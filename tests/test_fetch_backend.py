"""Offline version-resolution and publication tests."""
import gzip
import io
import json
import tarfile
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from arxiv_papers.fetch_papers import extract_source, fetch_one, metadata, parse_id


def atom(aid):
    return f'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <id>http://arxiv.org/abs/{aid}</id><title>A title</title>
    <author><name>A Author</name></author><summary>An abstract.</summary>
    <published>1999-01-02T00:00:00Z</published>
    <updated>2000-03-04T00:00:00Z</updated></entry></feed>'''.encode()


class FetchBackendTests(unittest.TestCase):
    def test_ids(self):
        self.assertEqual(parse_id('hep-th/9901001v12'), ('hep-th/9901001', 'v12'))
        self.assertEqual(parse_id('1706.03762'), ('1706.03762', None))
        for invalid in ('../1706.03762', '1706.03762v0', '1706.03762v', 'hep-th/9901001/extra'):
            with self.assertRaises(ValueError):
                parse_id(invalid)

    def test_exact_version_and_dates(self):
        with patch('arxiv_papers.fetch_papers.request', return_value=atom('hep-th/9901001v2')) as request:
            record = metadata('hep-th/9901001v2')
        self.assertIn('id_list=hep-th%2F9901001v2', request.call_args.args[0])
        self.assertEqual(record['version'], 'v2')
        self.assertEqual(record['first_submitted'], '1999-01-02')
        self.assertEqual(record['version_submitted'], '2000-03-04')
        self.assertEqual(record['tags'], [])

    def test_mismatch_is_not_fallback(self):
        with patch('arxiv_papers.fetch_papers.request', return_value=atom('1706.03762v3')) as request:
            with self.assertRaisesRegex(ValueError, 'inconsistent'):
                metadata('1706.03762v2')
        self.assertEqual(request.call_count, 1)

    def test_html_history_fallback(self):
        html = b'''<meta name="citation_title" content="Title">
        <meta name="citation_author" content="Author">
        <meta name="citation_abstract" content="Abstract">
        <div>[v1] Sat, 2 Jan 1999 00:00:00 UTC<br>
        [v2] Sat, 4 Mar 2000 00:00:00 UTC (this version)</div>'''
        error = urllib.error.URLError('offline')
        with patch('arxiv_papers.fetch_papers.request', side_effect=[error, html]):
            record = metadata('hep-th/9901001v2')
        self.assertEqual(record['first_submitted'], '1999-01-02')
        self.assertEqual(record['version_submitted'], '2000-03-04')
        with patch('arxiv_papers.fetch_papers.request', side_effect=[error, html]):
            with self.assertRaises(ValueError):
                metadata('hep-th/9901001v1')

    def test_realistic_selected_history_html(self):
        # arXiv links every history revision except the selected one. The
        # "this version" label is above the history, not inside its entry.
        for bare, selected, dates in (
            ('1706.03762', 'v2', ('2017-06-12', '2017-06-19')),
            ('hep-th/9901001', 'v1', ('1999-01-01', '1999-01-01')),
        ):
            old = bare.startswith('hep-th')
            history = (
                '<strong>[v1]</strong> Fri, 1 Jan 1999 01:01:10 UTC (15 KB)<br/>'
                f'<strong><a href="/abs/{bare}v2">[v2]</a></strong> Tue, 5 Jan 1999 21:36:42 UTC (15 KB)<br/>'
                f'<strong><a href="/abs/{bare}v3">[v3]</a></strong> Mon, 10 May 1999 04:45:54 UTC (15 KB)<br/>'
                if old else
                f'<strong><a href="/abs/{bare}v1">[v1]</a></strong> Mon, 12 Jun 2017 17:57:34 UTC (1,102 KB)<br/>'
                '<strong>[v2]</strong> Mon, 19 Jun 2017 16:49:45 UTC (1,125 KB)<br/>'
                f'<strong><a href="/abs/{bare}v3">[v3]</a></strong> Tue, 20 Jun 2017 05:20:02 UTC (1,125 KB)<br/>'
            )
            html = f'''<title>[{bare}{selected}] Title</title>
                <meta name="citation_title" content="Title">
                <meta name="citation_author" content="Author">
                <meta name="citation_arxiv_id" content="{bare}">
                <div class="dateline">[Submitted on a date (this version, {selected})]</div>
                <div class="submission-history"><h2>Submission history</h2>
                From: Author [<a href="/show-email/example">view email</a>]<br/>
                {history}</div>'''.encode()
            with patch('arxiv_papers.fetch_papers.request', side_effect=[urllib.error.URLError('offline'), html]):
                record = metadata(bare + selected)
            self.assertEqual(record['version'], selected)
            self.assertEqual((record['first_submitted'], record['version_submitted']), dates)
            with patch('arxiv_papers.fetch_papers.request', side_effect=[urllib.error.URLError('offline'), html]):
                with self.assertRaises(ValueError):
                    metadata(bare + 'v3')

    def test_blank_author_rejected(self):
        for author in (b'', b'   '):
            with patch('arxiv_papers.fetch_papers.request', return_value=atom('1706.03762v2').replace(b'A Author', author)):
                with self.assertRaises(ValueError):
                    metadata('1706.03762v2')

    def test_bounded_gzip_decompression(self):
        for content in (b'\\documentclass{article}' + b'x' * 2000, b'x' * 2000):
            with tempfile.TemporaryDirectory() as tmp, patch('arxiv_papers.fetch_papers.MAX_BYTES', 1000):
                with self.assertRaisesRegex(ValueError, 'decompressed source too large'):
                    extract_source(gzip.compress(content), Path(tmp) / 'source')

    def test_streamed_tar_member_limit(self):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w:gz') as tf:
            for n in range(4):
                member = tarfile.TarInfo(f'{n}.tex')
                member.size = 1
                tf.addfile(member, io.BytesIO(b'x'))
        with tempfile.TemporaryDirectory() as tmp, patch('arxiv_papers.fetch_papers.MAX_SOURCE_MEMBERS', 3), patch.object(tarfile.TarFile, 'getmembers', side_effect=AssertionError('must stream')):
            with self.assertRaisesRegex(ValueError, 'too many members'):
                extract_source(data.getvalue(), Path(tmp) / 'source')

    def test_streamed_tar_traversal_rejected(self):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w:gz') as tf:
            member = tarfile.TarInfo('../outside.tex')
            member.size = 1
            tf.addfile(member, io.BytesIO(b'x'))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'unsafe source member'):
                extract_source(data.getvalue(), Path(tmp) / 'source')
            self.assertFalse((Path(tmp) / 'outside.tex').exists())

    def test_pdf_only_pinned_and_encoded(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('arxiv_papers.fetch_papers.request', side_effect=[
                atom('hep-th/9901001v2'), b'%PDF-' + b'x' * 12000, b'%!PS source'
            ]) as request:
                result = fetch_one(Path(tmp), 'hep-th/9901001')
            self.assertEqual(result.name, 'hep-th_9901001v2')
            self.assertTrue((result / 'hep-th_9901001v2.pdf').is_file())
            self.assertTrue((result / 'abstract.md').is_file())
            self.assertEqual(json.loads((result / 'metadata.json').read_text())['arxiv_id'], 'hep-th/9901001')
            self.assertEqual(request.call_args_list[1].args[0], 'https://arxiv.org/pdf/hep-th/9901001v2')
            self.assertEqual(request.call_args_list[2].args[0], 'https://arxiv.org/e-print/hep-th/9901001v2')

    def test_failed_qa_does_not_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('arxiv_papers.fetch_papers.request', side_effect=[atom('1706.03762v2'), b'%PDF-tiny', b'']), self.assertRaisesRegex(RuntimeError, 'corpus QA'):
                fetch_one(Path(tmp), '1706.03762v2')
            self.assertEqual(list(Path(tmp).iterdir()), [])
