"""Asset remapping uses only temporary materializations, never corpus mutations."""
import importlib.util
from contextlib import redirect_stderr
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from arxiv_papers.assets import copy_assets, rewrite_asset_paths
from arxiv_papers.figindex import build_index, locate_image
from arxiv_papers.flatten_tex import flatten


def paper(body, preamble=""):
    return "\\documentclass{article}\n" + preamble + "\n\\begin{document}\n" + body + " text" * 60 + "\n\\end{document}\n"


class AssetTests(unittest.TestCase):
    def test_materialization_returns_explicit_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "src", Path(tmp) / "out"
            (root / "Images").mkdir(parents=True)
            (root / "Images/plot.png").write_bytes(b"image")
            (root / "refs.bib").write_text("bib")
            (root / "plain.sty").write_text("style")
            mapping = copy_assets(root, out)
            self.assertEqual(mapping, {"Images/plot.png": "figures/Images/plot.png", "refs.bib": "bibliography/refs.bib"})
            self.assertEqual((out / mapping["Images/plot.png"]).read_bytes(), b"image")
            self.assertFalse((out / "plain.sty").exists())

    def test_literal_starred_optional_and_bibliography_paths(self):
        mapping = {"Figures/pic.pdf": "figures/Figures/pic.pdf", "refs.bib": "bibliography/refs.bib", "local.bst": "bibliography/local.bst"}
        text = paper(r"\includegraphics* [width={2cm}] {Figures/pic}"
                     r"\bibliography{refs}\addbibresource[location=local]{refs.bib}\bibliographystyle{local}")
        result = rewrite_asset_paths(text, mapping)
        self.assertIn(r"\includegraphics* [width={2cm}] {figures/Figures/pic.pdf}", result)
        self.assertIn(r"\bibliography{bibliography/refs}", result)
        self.assertIn(r"\addbibresource[location=local]{bibliography/refs.bib}", result)
        self.assertIn(r"\bibliographystyle{bibliography/local}", result)

    def test_import_prefix_repair_and_nested_main(self):
        mapping = {"Figures/pic.pdf": "figures/Figures/pic.pdf"}
        result = rewrite_asset_paths(paper(r"\includegraphics{Sections/Figures/pic.pdf}"), mapping)
        self.assertIn(r"\includegraphics{figures/Figures/pic.pdf}", result)
        result = rewrite_asset_paths(paper(r"\includegraphics{../Figures/pic.pdf}"), mapping, cwd="Main")
        self.assertIn(r"\includegraphics{figures/Figures/pic.pdf}", result)

    def test_source_path_wins_over_colliding_canonical_spelling(self):
        mapping = {"pic.pdf": "figures/pic.pdf", "figures/pic.pdf": "figures/figures/pic.pdf"}
        result = rewrite_asset_paths(paper(r"\includegraphics{figures/pic.pdf}"), mapping)
        self.assertIn(r"\includegraphics{figures/figures/pic.pdf}", result)

    def test_graphicspath_and_extension_precedence(self):
        mapping = {"Images/pic.pdf": "figures/Images/pic.pdf", "Images/pic.png": "figures/Images/pic.png"}
        text = paper(r"\includegraphics{pic}", r"\graphicspath{{Images/}}\DeclareGraphicsExtensions{.png,.pdf}")
        result = rewrite_asset_paths(text, mapping)
        self.assertIn(r"\graphicspath{{figures/}{figures/Images/}}", result)
        self.assertIn(r"\includegraphics{figures/Images/pic.png}", result)
        mapping = {"figures/pic.png": "figures/figures/pic.png", "elsewhere/pic.png": "figures/elsewhere/pic.png"}
        result = rewrite_asset_paths(paper(r"\includegraphics{pic}", r"\graphicspath{{figures/}}"), mapping)
        self.assertIn(r"\includegraphics{figures/figures/pic.png}", result)

    def test_extensions_precede_directories_and_root_is_only_a_fallback(self):
        mapping = {"pic.pdf": "figures/pic.pdf", "Images/pic.png": "figures/Images/pic.png"}
        text = paper(r"\includegraphics{pic}", r"\graphicspath{{Images/}}\DeclareGraphicsExtensions{.png,.pdf}")
        self.assertIn(r"\includegraphics{figures/Images/pic.png}", rewrite_asset_paths(text, mapping))
        mapping = {"pic.pdf": "figures/pic.pdf", "src/Images/pic.pdf": "figures/src/Images/pic.pdf"}
        text = paper(r"\includegraphics{pic.pdf}", r"\graphicspath{{Images/}}")
        self.assertIn(r"\includegraphics{figures/src/Images/pic.pdf}", rewrite_asset_paths(text, mapping, cwd="src"))

    def test_import_repair_uses_graphicspath_before_basename_fallback(self):
        mapping = {"Figures/pic.pdf": "figures/Figures/pic.pdf", "Other/pic.pdf": "figures/Other/pic.pdf"}
        text = paper(r"\includegraphics{Sections/pic.pdf}", r"\graphicspath{{Figures/}}")
        self.assertIn(r"\includegraphics{figures/Figures/pic.pdf}", rewrite_asset_paths(text, mapping))

    def test_unsupported_inline_code_is_not_rewritten(self):
        for code in (r"\lstinline|\includegraphics{pic.pdf}|", r"\mintinline{tex}|\includegraphics{pic.pdf}|"):
            text = paper(code)
            with redirect_stderr(io.StringIO()):
                self.assertEqual(rewrite_asset_paths(text, {"pic.pdf": "figures/pic.pdf"}), text)

    def test_unsafe_paths_are_not_repaired_by_basename(self):
        for path in ("/outside/pic.pdf", "../../outside/pic.pdf"):
            text = paper(r"\includegraphics{" + path + "}")
            with redirect_stderr(io.StringIO()):
                result = rewrite_asset_paths(text, {"pic.pdf": "figures/pic.pdf"})
            self.assertIn(r"\includegraphics{" + path + "}", result)

    def test_dynamic_graphicspath_preserves_unknown_reference(self):
        mapping = {"a/pic.pdf": "figures/a/pic.pdf", "b/pic.pdf": "figures/b/pic.pdf"}
        text = paper(r"\includegraphics{pic.pdf}", r"\def\mypath{b/}\graphicspath{{\mypath}}")
        result = rewrite_asset_paths(text, mapping)
        self.assertIn(r"\includegraphics{pic.pdf}", result)
        self.assertIn(r"{figures/\mypath}", result)

    def test_macro_supplied_graphicspath_list_is_not_overwritten(self):
        mapping = {"a/pic.pdf": "figures/a/pic.pdf", "b/pic.pdf": "figures/b/pic.pdf"}
        text = paper(r"\includegraphics{pic.pdf}", r"\def\paths{{b/}}\graphicspath{\paths}")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(rewrite_asset_paths(text, mapping), text)

    def test_graphicspath_scopes_are_restored(self):
        mapping = {"a/pic.png": "figures/a/pic.png", "b/pic.png": "figures/b/pic.png"}
        for opening, closing in (("{", "}"), (r"\begingroup", r"\endgroup"),
                                 (r"\begin{figure}", r"\end{figure}")):
            text = paper(opening + r"\graphicspath{{b/}}\includegraphics{pic}" + closing + r"\includegraphics{pic}", r"\graphicspath{{a/}}")
            result = rewrite_asset_paths(text, mapping)
            self.assertIn(r"\includegraphics{figures/b/pic.png}", result)
            self.assertTrue(result.index(r"\includegraphics{figures/b/pic.png}") < result.index(r"\includegraphics{figures/a/pic.png}"))

    def test_ambiguity_is_an_error_not_arbitrary_basename_choice(self):
        mapping = {"a/pic.png": "figures/a/pic.png", "b/pic.png": "figures/b/pic.png"}
        with self.assertRaisesRegex(ValueError, "ambiguous asset reference"):
            rewrite_asset_paths(paper(r"\includegraphics{pic}"), mapping)

    def test_code_dynamic_paths_and_missing_files_stay_unchanged(self):
        mapping = {"pic.png": "figures/pic.png"}
        code = r"\verb|\includegraphics{pic.png}|"
        body = code + r"\includegraphics{\imagepath/pic}\includegraphics{#1}\includegraphics{missing.pdf}"
        errors = io.StringIO()
        with redirect_stderr(errors):
            result = rewrite_asset_paths(paper(body), mapping)
        self.assertIn(body, result)
        self.assertIn("missing.pdf", errors.getvalue())
        self.assertIn("leaving it unchanged", errors.getvalue())

    def test_quoted_spaces_and_multiple_bibliographies(self):
        mapping = {"my plot.png": "figures/my plot.png", "one.bib": "bibliography/one.bib", "two.bib": "bibliography/two.bib"}
        result = rewrite_asset_paths(paper(r'\includegraphics{"my plot.png"}\bibliography{one, two}'), mapping)
        self.assertIn(r"\includegraphics{figures/my plot.png}", result)
        self.assertIn(r"\bibliography{bibliography/one,bibliography/two}", result)

    def test_index_resolves_exact_canonical_paths_with_duplicate_basenames(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "1706.03762v1"
            for directory in (root / "figures/a", root / "figures/b"):
                directory.mkdir(parents=True)
                (directory / "pic.png").write_bytes(b"image")
            self.assertEqual(locate_image(root, "figures/a/pic.png"), "figures/a/pic.png")
            self.assertIsNone(locate_image(root, "../../outside.png"))
            (root / "1706.03762v1.source.tex").write_text("original source without figures")
            (root / "1706.03762v1.tex").write_text(paper(r"\begin{figure}\includegraphics*{figures/b/pic.png}\caption{Caption}\end{figure}"))
            build_index(root)
            self.assertIn("- image: figures/b/pic.png", (root / "FIGURES.md").read_text())


    @unittest.skipUnless(shutil.which("pdflatex") and shutil.which("latexpand") and importlib.util.find_spec("pymupdf"), "rendering tools unavailable")
    def test_relocated_graphics_and_wrapper_render_identically(self):
        import pymupdf
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            root, out = tmp / "src", tmp / "out"
            (root / "Figures").mkdir(parents=True)
            out.mkdir()
            image = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 12, 12), False)
            image.clear_with(128)
            (root / "Figures/my pic.png").write_bytes(image.tobytes("png"))
            preamble = r"\usepackage{graphicx}\newcommand\img[1]{\includegraphics[width=1cm]{#1}}\graphicspath{{Figures/}}"
            (root / "main.tex").write_text(paper(r"\includegraphics[width=1cm]{my pic.png}\img{Figures/my pic.png}", preamble))
            mapping = copy_assets(root, out)
            flatten(root, "1706.03762", "1706.03762v1", out / "flat.tex", asset_map=mapping)
            for name, cwd, source in (("original", root, "main.tex"), ("mapped", out, "flat.tex")):
                destination = tmp / name
                destination.mkdir()
                subprocess.run(["pdflatex", "-interaction=batchmode", "-halt-on-error", "-jobname=paper", f"-output-directory={destination}", source],
                               cwd=cwd, check=True, capture_output=True, timeout=60)
            with pymupdf.open(tmp / "original/paper.pdf") as before, pymupdf.open(tmp / "mapped/paper.pdf") as after:
                self.assertEqual([p.get_text() for p in before], [p.get_text() for p in after])
                self.assertEqual([p.get_pixmap().samples for p in before], [p.get_pixmap().samples for p in after])

    @unittest.skipUnless(shutil.which("latexpand"), "latexpand unavailable")
    def test_subimport_repair_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, out = Path(tmp) / "src", Path(tmp) / "out"
            (root / "Sections").mkdir(parents=True)
            (root / "Figures").mkdir()
            (root / "Figures/pic.png").write_bytes(b"image")
            (root / "main.tex").write_text(paper(r"\subimport{Sections/}{body}", r"\usepackage{import,graphicx}"))
            (root / "Sections/body.tex").write_text(r"\includegraphics{Figures/pic.png}")
            out.mkdir()
            mapping = copy_assets(root, out)
            flatten(root, "1706.03762", "1706.03762v1", out / "flat.tex", asset_map=mapping)
            self.assertIn(r"\includegraphics{figures/Figures/pic.png}", (out / "flat.tex").read_text())


if __name__ == "__main__":
    unittest.main()
