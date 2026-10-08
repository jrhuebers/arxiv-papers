"""Offline extraction, flattening and index edge-case checks."""
import gzip
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import arxiv_papers.flatten_tex as flatten_module
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

    @unittest.skipUnless(shutil.which("perl"), "perl unavailable")
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

    @unittest.skipUnless(shutil.which("perl"), "perl unavailable")
    def test_flatten_uses_bundled_script_not_system_latexpand(self):
        perl = shutil.which("perl")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tools = root / "bin"
            tools.mkdir()
            (tools / "perl").symlink_to(perl)
            sentinel = root / "system-latexpand-used"
            fake = tools / "latexpand"
            fake.write_text('#!/bin/sh\nprintf used > "' + str(sentinel) + '"\nexit 99\n')
            fake.chmod(0o755)
            source = root / "source"
            source.mkdir()
            (source / "main.tex").write_text(
                "\\documentclass{article}\n\\begin{document}\n\\input{body}\n\\end{document}\n")
            (source / "body.tex").write_text("Bundled expansion " + "words " * 60)
            output = root / "flat.tex"
            with patch.dict(os.environ, {"PATH": str(tools)}), patch.object(
                    flatten_module.subprocess, "run", wraps=subprocess.run) as run:
                flatten(source, "1706.03762", "1706.03762v1", output)
            self.assertFalse(sentinel.exists(), "system latexpand must never run")
            self.assertIn("Bundled expansion", output.read_text())
            self.assertNotIn(r"\input{body}", output.read_text())
            vendor = Path(flatten_module.__file__).with_name("_vendor") / "latexpand"
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0],
                             [str(tools / "perl"), str(vendor), "--keep-comments", "main.tex"])

    def test_flatten_missing_perl_is_explicit_error(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "main.tex").write_text(r"\documentclass{article}" + "words " * 60)
            tools = root / "bin"
            tools.mkdir()
            sentinel = root / "system-latexpand-used"
            fake = tools / "latexpand"
            fake.write_text('#!/bin/sh\nprintf used > "' + str(sentinel) + '"\nexit 99\n')
            fake.chmod(0o755)
            output = root / "flat.tex"
            with patch.dict(os.environ, {"PATH": str(tools)}), patch.object(
                    flatten_module.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "(?i)perl"):
                    flatten(root, "1706.03762", "1706.03762v1", output)
            run.assert_not_called()
            self.assertFalse(sentinel.exists())
            self.assertFalse(output.exists())

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
