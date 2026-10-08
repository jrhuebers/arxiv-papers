"""Bounded, retrying HTTP downloads (standard library only).

Import ``request`` into fetch_papers so its existing request mocks and HTTPError
fallbacks continue to work. Rate limiting is process-local and sequential, as is
the fetching CLI. Content-format validation remains the caller's responsibility.
"""
from email.utils import parsedate_to_datetime
import http.client
import random
import sys
import time
import urllib.error
import urllib.request

UA = "arxiv-papers/0.1 (paper library; contact: https://github.com/jrhuebers/arxiv-papers)"
MAX_BYTES = 100_000_000
MIN_INTERVAL = 3.2
TIMEOUT = 60
MAX_ATTEMPTS = 5
BACKOFF_BASE = 2.0
MAX_DELAY = 60.0
CHUNK_BYTES = 64 * 1024
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_last_request = None


def _retry_after(exc: BaseException) -> float:
    if not isinstance(exc, urllib.error.HTTPError) or exc.headers is None:
        return 0.0
    value = exc.headers.get("Retry-After", "").strip()
    try:
        if value.isascii() and value.isdigit():
            # Avoid parsing arbitrarily large integer strings from the server.
            return float("inf") if len(value) > 8 else float(value)
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            return 0.0
        return max(0.0, date.timestamp() - time.time())
    except (ValueError, TypeError, OverflowError):
        return 0.0


def _read(response, url: str) -> bytes:
    raw_length = response.headers.get("Content-Length")
    length = None
    if raw_length is not None:
        value = raw_length.strip()
        if not value.isascii() or not value.isdigit():
            raise ValueError(f"invalid Content-Length: {url}")
        if len(value.lstrip("0")) > len(str(MAX_BYTES)):
            raise ValueError(f"download exceeds {MAX_BYTES} bytes: {url}")
        length = int(value.lstrip("0") or "0")
        if length > MAX_BYTES:
            raise ValueError(f"download exceeds {MAX_BYTES} bytes: {url}")
    chunks = []
    total = 0
    while True:
        chunk = response.read(min(CHUNK_BYTES, MAX_BYTES + 1 - total))
        if not isinstance(chunk, bytes):
            raise ValueError(f"response is not bytes: {url}")
        total += len(chunk)
        if total > MAX_BYTES:
            raise ValueError(f"download exceeds {MAX_BYTES} bytes: {url}")
        if not chunk:
            break
        chunks.append(chunk)
    if length is not None and total < length:
        # read(size) can silently return EOF on a truncated HTTP body.
        raise http.client.IncompleteRead(b"", length - total)
    if length is not None and total > length:
        raise ValueError(f"body exceeds Content-Length: {url}")
    return b"".join(chunks)


def request(url: str) -> bytes:
    """Return bytes, retry transient failures, and preserve final exceptions.

    At most five attempts; per-attempt socket timeout is 60 seconds. Backoff
    delays are capped at 60 seconds. A longer Retry-After stops retrying rather
    than contacting the server prematurely. This is not a total wall
    clock deadline (urllib's timeout applies to individual socket operations).
    """
    global _last_request
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if _last_request is not None:
            delay = MIN_INTERVAL - (time.monotonic() - _last_request)
            if delay > 0:
                time.sleep(delay)
        _last_request = time.monotonic()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
                return _read(response, url)
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                InterruptedError, EOFError, http.client.IncompleteRead) as exc:
            transient = not isinstance(exc, urllib.error.HTTPError) or exc.code in RETRY_STATUSES
            if not transient or attempt == MAX_ATTEMPTS:
                print(f"download attempt {attempt}/{MAX_ATTEMPTS} {url}: {exc}; giving up", file=sys.stderr)
                raise
            retry_after = _retry_after(exc)
            if retry_after > MAX_DELAY:
                print(f"download attempt {attempt}/{MAX_ATTEMPTS} {url}: Retry-After exceeds {MAX_DELAY:g}s; giving up", file=sys.stderr)
                raise
            base = min(MAX_DELAY, BACKOFF_BASE * 2 ** (attempt - 1))
            delay = min(MAX_DELAY, max(base + random.uniform(0.0, base), retry_after))
            print(f"download attempt {attempt}/{MAX_ATTEMPTS} {url}: {exc}; retry in {delay:.2f}s", file=sys.stderr)
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()
            time.sleep(delay)
