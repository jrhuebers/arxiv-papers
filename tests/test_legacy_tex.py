"""Old-style IDs often come with LaTeX 2.09 documentstyle sources."""
import gzip
from pathlib import Path
import shutil
import tempfile
import unittest

from arxiv_papers.fetch_papers import extract_source
from arxiv_papers.flatten_tex import find_main, flatten
from arxiv_papers.qa_corpus import check_paper


class LegacyTeXTests(unittest.TestCase):
    def test_plain_and_gzip_documentstyle_sources(self):
        text = br'\documentstyle[12pt]{article}' + b'\n'
        for data, kind in [(text, 'tex'), (gzip.compress(text), 'tex.gz')]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / 'source'
                self.assertEqual(extract_source(data, root), kind)
                self.assertEqual(find_main(root), root / 'main.tex')

    @unittest.skipUnless(shutil.which('perl'), 'perl required')
    def test_flatten_and_validate_legacy_paper(self):
        text = (r'\documentstyle[12pt]{article}' + '\n' +
                r'\begin{document}' + '\n' + 'Legacy text. ' * 40 + '\n' + r'\end{document}')
        with tempfile.TemporaryDirectory() as tmp:
            paper = Path(tmp) / 'hep-th_9901001v1'
            paper.mkdir()
            root = Path(tmp) / 'source'
            extract_source(text.encode(), root)
            (paper / (paper.name + '.pdf')).write_bytes(b'%PDF-' + b'0' * 10000)
            (paper / (paper.name + '.source.tex')).write_text(text)
            flatten(root, 'hep-th/9901001', 'hep-th/9901001v1', paper / (paper.name + '.tex'))
            self.assertEqual(check_paper(paper, paper.name, 'hep-th/9901001v1', canonical_id='hep-th/9901001'), [])
