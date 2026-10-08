"""Offline extraction, flattening and index edge-case checks."""
import gzip
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

from arxiv_papers.fetch_papers import extract_source
from arxiv_papers.figindex import build_index as build_figure_index, strip_tex_noise
from arxiv_papers.flatten_tex import collapse_blank_lines, find_main, flatten, strip_comments
from arxiv_papers.qa_corpus import check_paper
from arxiv_papers.refindex import build_index


class PipelineTests(unittest.TestCase):
    def test_caption_tex_normalization(self):
        self.assertEqual(strip_tex_noise(r"The verb `making' and \textbf{Top}."), "The verb “making” and Top.")

    def test_comments(self):
        text = "a% drop\n\\% keep\n\\\\% drop\n\\begin{verbatim}\nraw%keep\n\\end{verbatim}\nb%drop"
        self.assertEqual(strip_comments(text), "a\n\\% keep\n\\\\\n\\begin{verbatim}\nraw%keep\n\\end{verbatim}\nb")

    def test_collapse_blank_lines_preserves_verbatim(self):
        self.assertEqual(collapse_blank_lines("A\n\n\n\n\nB\n"), "A\n\nB\n")
        text = "before\n\n\n\\begin{verbatim}\n\n\n\nraw%line\n\\end{verbatim}\n\n\nend\n"
        expected = "before\n\n\\begin{verbatim}\n\n\n\nraw%line\n\\end{verbatim}\n\nend\n"
        self.assertEqual(collapse_blank_lines(text), expected)

    def test_main_largest(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "stub.tex").write_text(r"\documentclass{article}\input{real}")
            (root / "real.tex").write_text(r"\documentclass{article}" + "x" * 100)
            self.assertEqual(find_main(root).name, "real.tex")

    def test_flatten_multifile(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "sections").mkdir()
            (root / "main.tex").write_text("\\documentclass{article}\n\\begin{document}\n\\input{sections/body}\n\\end{document}\n")
            (root / "sections/body.tex").write_text("Included section " + "words " * 60 + "% discard\n\\% kept\n\\begin{verbatim}\nraw%keep\n\\end{verbatim}\n")
            out = root / "flat.tex"
            flatten(root, "1706.03762", "1706.03762v7", out)
            text = out.read_text()
            self.assertIn("Included section", text)
            self.assertNotIn("discard", text)
            self.assertIn(r"\% kept", text)
            self.assertIn("raw%keep", text)
            self.assertNotIn(r"\input{sections/body}", text)
            self.assertFalse(check_paper(root, "1706.03762") == [])  # PDF and source intentionally absent

    def test_unsafe_tar(self):
        for name in ("../outside.tex", "/tmp/outside.tex", "./safe/../../outside.tex"):
            with tempfile.TemporaryDirectory() as d:
                archive = io.BytesIO()
                with tarfile.open(fileobj=archive, mode="w:gz") as tf:
                    data = b"evil"
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    tf.addfile(info, io.BytesIO(data))
                with self.assertRaises(ValueError):
                    extract_source(archive.getvalue(), Path(d) / "src")

    def test_single_file_source(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "source"
            data = b"\\documentclass{article}\n\\begin{document}hi\\end{document}\n"
            self.assertEqual(extract_source(gzip.compress(data), root), "tex.gz")
            self.assertEqual((root / "main.tex").read_bytes(), data)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "source"
            self.assertEqual(extract_source(data, root), "tex")
            self.assertEqual((root / "main.tex").read_bytes(), data)


    def test_plain_source_figure_index_reports_actual_source_name(self):
        with tempfile.TemporaryDirectory() as d:
            paper = Path(d) / "1706.03762v1"
            paper.mkdir()
            tex = ("\\documentclass{article}\n\\begin{document}\n" + "text " * 60 +
                   "\\begin{figure}\\includegraphics{plot}\\caption{Caption}\\end{figure}\n\\end{document}\n")
            (paper / "1706.03762v1.tex").write_text(tex)
            (paper / "1706.03762v1.source.tex").write_text(tex)
            build_figure_index(paper)
            self.assertIn("- source: 1706.03762v1.source.tex ->", (paper / "FIGURES.md").read_text())


    def test_reference_index_falls_back_to_bib(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "1706.03762v1"
            root.mkdir()
            (root / "1706.03762v1.tex").write_text(r"\documentclass{article}\bibliography{refs}")
            (root / "bibliography").mkdir()
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w:gz") as tf:
                bib = b"@article{bibkey, author={A. Author}, title={Bib title}, year={2026}}\n"
                entry = tarfile.TarInfo("refs.bib")
                entry.size = len(bib)
                tf.addfile(entry, io.BytesIO(bib))
            (root / "1706.03762v1.tar.gz").write_bytes(archive.getvalue())
            build_index(str(root))
            references = (root / "REFERENCES.md").read_text()
            self.assertIn("`bibkey`", references)
            self.assertIn("Bib title", references)

    def test_missing_qa(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(check_paper(Path(d), "1706.03762"))


if __name__ == "__main__":
    unittest.main()
