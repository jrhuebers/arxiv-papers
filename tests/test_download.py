"""Downloader tests: all clocks, sleeps and networking are mocked."""
from email.message import Message
from email.utils import formatdate
import http.client
import io
import unittest
from unittest.mock import patch
import urllib.error

from arxiv_papers import download

URL = "https://example.invalid/paper"


class Response(io.BytesIO):
    def __init__(self, body=b"paper", length=None):
        super().__init__(body)
        self.headers = Message()
        if length is not None:
            self.headers["Content-Length"] = str(length)


def http_error(code, retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(URL, code, "test failure", headers, None)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        download._last_request = None
        self.now = 0.0
        self.starts = []
        self.sleeps = []
        self.responses = []
        self.stderr = io.StringIO()
        patches = [
            patch.object(download.time, "monotonic", side_effect=lambda: self.now),
            patch.object(download.time, "time", return_value=1_700_000_000.0),
            patch.object(download.time, "sleep", side_effect=self.sleep),
            patch.object(download.random, "uniform", return_value=0.0),
            patch.object(download.urllib.request, "urlopen", side_effect=self.urlopen),
            patch.object(download.sys, "stderr", self.stderr),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(setattr, download, "_last_request", None)

    def sleep(self, seconds):
        self.assertGreaterEqual(seconds, 0)
        self.sleeps.append(seconds)
        self.now += seconds

    def urlopen(self, req, timeout):
        self.assertEqual(req.full_url, URL)
        self.assertEqual(req.get_header("User-agent"), download.UA)
        self.assertEqual(timeout, 60)
        self.starts.append(self.now)
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def test_success_and_rate_limit_between_calls(self):
        self.responses = [Response(), Response()]
        self.assertEqual(download.request(URL), b"paper")
        self.assertEqual(download.request(URL), b"paper")
        self.assertEqual(self.starts, [0.0, 3.2])
        self.assertEqual(self.stderr.getvalue(), "")

    def test_all_transient_http_statuses(self):
        for code in sorted(download.RETRY_STATUSES):
            with self.subTest(code=code):
                download._last_request = None
                self.responses = [http_error(code), Response()]
                self.assertEqual(download.request(URL), b"paper")
                self.assertGreaterEqual(self.starts[-1] - self.starts[-2], 3.2 - 1e-9)
        self.assertIn(URL, self.stderr.getvalue())
        self.assertIn("attempt 1/5", self.stderr.getvalue())
        self.assertIn("retry in", self.stderr.getvalue())

    def test_http_exhaustion_preserves_error_and_exponential_delay(self):
        errors = [http_error(503) for _ in range(download.MAX_ATTEMPTS)]
        self.responses = errors.copy()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            download.request(URL)
        self.assertIs(caught.exception, errors[-1])
        self.assertEqual(len(self.starts), 5)
        self.assertEqual(self.sleeps, [2.0, 1.2000000000000002, 4.0, 8.0, 16.0])
        self.assertIn("giving up", self.stderr.getvalue())

    def test_permanent_errors_never_retry(self):
        for code in (400, 401, 403, 404, 406):
            with self.subTest(code=code):
                error = http_error(code)
                self.responses = [error]
                before = len(self.starts)
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    download.request(URL)
                self.assertIs(caught.exception, error)
                self.assertEqual(len(self.starts), before + 1)

    def test_network_and_interrupted_read_errors(self):
        failures = [urllib.error.URLError("DNS failure"), TimeoutError("timeout"),
                    ConnectionResetError("reset"), InterruptedError("interrupted"),
                    http.client.RemoteDisconnected("disconnect"),
                    http.client.IncompleteRead(b"partial", 12), EOFError("EOF")]
        for error in failures:
            with self.subTest(error=type(error).__name__):
                response = Response()
                response.read = lambda size: (_ for _ in ()).throw(error)
                self.responses = [response, Response(b"complete")]
                self.assertEqual(download.request(URL), b"complete")
                self.assertTrue(response.closed)

    def test_network_exhaustion_is_finite(self):
        error = urllib.error.URLError("offline")
        self.responses = [error] * download.MAX_ATTEMPTS
        with self.assertRaises(urllib.error.URLError) as caught:
            download.request(URL)
        self.assertIs(caught.exception, error)
        self.assertEqual(len(self.starts), download.MAX_ATTEMPTS)

    def test_short_body_retries_from_scratch(self):
        response = Response(b"partial", 12)
        self.responses = [response, Response(b"complete", 8)]
        self.assertEqual(download.request(URL), b"complete")
        self.assertTrue(response.closed)

    def test_invalid_and_oversized_content_do_not_retry(self):
        with patch.object(download, "MAX_BYTES", 8):
            for response in (Response(b"123456789"), Response(b"", 9),
                             Response(b"x", "bad"), Response(b"x", -1),
                             Response(b"xx", 1), Response(b"", "9" * 5000)):
                with self.subTest(headers=response.headers):
                    before = len(self.starts)
                    self.responses = [response]
                    with self.assertRaises(ValueError):
                        download.request(URL)
                    self.assertEqual(len(self.starts), before + 1)
                    self.assertTrue(response.closed)

    def test_exact_limit_and_empty_body(self):
        with patch.object(download, "MAX_BYTES", 8), patch.object(download, "CHUNK_BYTES", 3):
            self.responses = [Response(b"12345678", 8), Response(b"", 0)]
            self.assertEqual(download.request(URL), b"12345678")
            self.assertEqual(download.request(URL), b"")

    def test_retry_after_seconds_date_invalid_past_and_bounded(self):
        cases = [("20", 20), (formatdate(1_700_000_030, usegmt=True), 30),
                 (formatdate(1_699_999_999, usegmt=True), 2),
                 ("nonsense", 2), ("-1", 2), ("NaN", 2)]
        for header, expected in cases:
            with self.subTest(header=header):
                download._last_request = None
                self.sleeps.clear()
                self.responses = [http_error(429, header), Response()]
                self.assertEqual(download.request(URL), b"paper")
                self.assertEqual(self.sleeps[0], expected)

    def test_long_retry_after_gives_up_without_retrying_early(self):
        for header in ("999999999999999999999999", formatdate(1_700_001_000, usegmt=True)):
            with self.subTest(header=header):
                download._last_request = None
                self.sleeps.clear()
                error = http_error(429, header)
                self.responses = [error]
                before = len(self.starts)
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    download.request(URL)
                self.assertIs(caught.exception, error)
                self.assertEqual(len(self.starts), before + 1)
                self.assertEqual(self.sleeps, [])
                self.assertIn("Retry-After exceeds", self.stderr.getvalue())

    def test_jitter_and_backoff_cap(self):
        with patch.object(download.random, "uniform", side_effect=lambda low, high: high), \
                patch.object(download, "BACKOFF_BASE", 40):
            self.responses = [http_error(500), http_error(500), Response()]
            self.assertEqual(download.request(URL), b"paper")
            self.assertEqual(self.sleeps, [60, 60])


if __name__ == "__main__":
    unittest.main()
