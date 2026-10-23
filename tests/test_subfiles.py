"""Offline regressions for the subfile-only manuscript 2506.02070v3."""
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from arxiv_papers.assets import copy_assets
from arxiv_papers.flatten_tex import find_main, flatten
from arxiv_papers.qa_corpus import check_paper
from arxiv_papers.subfiles import has_unexpanded_inclusions, normalize_subfiles, normalized_source_tree


class SubfileNormalizationTests(unittest.TestCase):
    def test_comments_and_verbatim_are_not_subfile_calls(self):
        text = ("% \\subfile{missing}\n"
                "\\begin{verbatim}\n\\subfile{missing}\n\\end{verbatim}\n"
                "\\verb|\\subfile{missing}|\n")
        self.assertEqual(normalize_subfiles(text), text)
        self.assertFalse(has_unexpanded_inclusions(text))

    def test_normal_source_with_uncertain_verbatim_is_untouched(self):
        text = "\\documentclass{article}\n\\begin{verbatim}\nunterminated"
        self.assertEqual(normalize_subfiles(text), text)

    def test_dynamic_subfile_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported subfile path"):
            normalize_subfiles(r"\subfile{\chaptername}")

    def test_unbraced_dynamic_subfile_is_still_unexpanded(self):
        self.assertTrue(has_unexpanded_inclusions(r"\subfile\chaptername"))
        self.assertTrue(has_unexpanded_inclusions(r"\subfile"))

    def test_subfile_wrappers_require_document_body(self):
        with self.assertRaisesRegex(ValueError, "begin/end"):
            normalize_subfiles(r"\documentclass[../main.tex]{subfiles}missing body")

    def test_wrapper_delimiters_in_macro_comments_and_code_are_ignored(self):
        text = ("\\documentclass[../main.tex]{subfiles}\n"
                "\\newcommand{\\fake}{\\begin{document}fake\\end{document}}\n"
                "% \\begin{document}\n"
                "\\begin{document}\nActual body.\n"
                "\\verb|\\end{document}|\n\\end{document}\nIgnored tail.\n")
        body = normalize_subfiles(text)
        self.assertIn("Actual body.", body)
        self.assertIn(r"\verb|\end{document}|", body)
        self.assertNotIn("fake", body)
        self.assertNotIn("Ignored tail", body)

    def test_malformed_ignored_tail_is_not_tokenized(self):
        text = ("\\documentclass[main.tex]{subfiles}\n\\begin{document}\n"
                "Valid body.\n\\end{document}\n\\begin{verbatim}\nIncomplete unused tail.")
        self.assertEqual(normalize_subfiles(text), "Valid body.\n")

    def test_main_excludes_larger_standalone_subfile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.tex").write_text(r"\documentclass{article}\subfile{chapter}")
            (root / "chapter.tex").write_text(
                r"\documentclass[main.tex]{subfiles}" + "chapter text " * 100)
            self.assertEqual(find_main(root), root / "main.tex")

    def test_qa_rejects_unexpanded_subfiles_and_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for command in ("subfile", "subfileinclude", "import", "subimport"):
                (root / "1706.03762v1.tex").write_text(
                    "% arXiv 1706.03762 (latest: 1706.03762v1)\n"
                    "\\documentclass{article}\n" + "words " * 60 +
                    f"\\{command}{{missing}}{{chapter}}\n")
                errors = check_paper(root, "1706.03762v1", version="1706.03762v1",
                                     canonical_id="1706.03762")
                self.assertTrue(any("unexpanded" in error for error in errors), (command, errors))


@unittest.skipUnless(shutil.which("perl"), "perl unavailable")
class SubfilePipelineTests(unittest.TestCase):
    def test_all_twelve_fragment_chapters_assets_and_macro_liveness(self):
        # Same structure as 2506.02070v3: main has only front matter and twelve
        # subfile calls, some with .tex and some without, plus root-relative art.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            root.mkdir()
            (root / "subfiles").mkdir()
            (root / "figures").mkdir()
            (root / "figures/plot.png").write_bytes(b"fixture image")
            (root / "math_commands.tex").write_text(
                "\\newcommand{\\usedinchapter}{Live chapter macro}\n"
                "\\newcommand{\\unusedmacro}{Unused macro}\n")
            calls = "\n".join(f"\\subfile{{subfiles/part_{i:02d}" +
                              (".tex" if i % 2 else "") + "}" for i in range(12))
            (root / "main.tex").write_text(
                "\\documentclass{article}\n\\usepackage{subfiles}\n"
                "\\input{math_commands}\n\\begin{document}\nFront matter.\n" +
                calls + "\n\\end{document}\n")
            for i in range(12):
                (root / f"subfiles/part_{i:02d}.tex").write_text(
                    f"\\section{{Chapter {i}}}\n" + f"Unique chapter {i} prose. " * 20 +
                    "\n\\usedinchapter\n"
                    "\\begin{figure}\\includegraphics{figures/plot.png}"
                    f"\\caption{{Chapter {i} illustration}}\\end{{figure}}\n")
            original = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            paper = Path(tmp) / "paper"
            paper.mkdir()
            mapping = copy_assets(root, paper)
            bodies = []
            for mode in ("safe", "off"):
                output = paper / f"flat-{mode}.tex"
                main = flatten(root, "2506.02070", "2506.02070v3", output,
                               prune_macros=mode, asset_map=mapping)
                self.assertEqual(main, root / "main.tex")
                text = output.read_text()
                for i in range(12):
                    self.assertIn(f"\\section{{Chapter {i}}}", text)
                    self.assertIn(f"Unique chapter {i} prose.", text)
                self.assertEqual(text.count(r"\includegraphics{figures/figures/plot.png}"), 12)
                self.assertFalse(has_unexpanded_inclusions(text))
                self.assertIn(r"\newcommand{\usedinchapter}", text)
                self.assertEqual(r"\newcommand{\unusedmacro}" in text, mode == "off")
                bodies.append(text.split(r"\begin{document}", 1)[1])
            self.assertEqual(bodies[0], bodies[1], "pruning must not change chapter content")
            self.assertEqual(original, {p.relative_to(root): p.read_bytes()
                                        for p in root.rglob("*") if p.is_file()})

    def test_standalone_nested_subfiles_and_local_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            (root / "chapters/nested").mkdir(parents=True)
            (root / "main.tex").write_text(
                "\\documentclass{article}\n\\usepackage{subfiles}\n\\begin{document}\n"
                "Before\\subfile{chapters/one}Between\\subfile{chapters/two}After\n"
                "\\end{document}\n")
            (root / "chapters/one.tex").write_text(
                "\\documentclass[../main.tex]{subfiles}\n\\input{ignored-missing-preamble}\n"
                "\\begin{document}\n" + "First chapter body. " * 30 +
                "\n\\input{local}\n\\subfile{nested/inner}\n\\end{document}\nIgnored tail.")
            (root / "chapters/two.tex").write_text("Second chapter body. " * 30)
            (root / "chapters/local.tex").write_text("Correct child-local input.")
            (root / "local.tex").write_text("Wrong root input.")
            (root / "chapters/nested/inner.tex").write_text(
                "\\documentclass[../../main.tex]{subfiles}\n\\begin{document}\n"
                "Nested child body.\n\\end{document}")
            output = Path(tmp) / "flat.tex"
            flatten(root, "2506.02070", "2506.02070v3", output)
            text = output.read_text()
            for phrase in ("Before", "Between", "After", "First chapter body.",
                           "Second chapter body.", "Correct child-local input.", "Nested child body."):
                self.assertIn(phrase, text)
            for phrase in ("ignored-missing-preamble", "Ignored tail", "Wrong root input"):
                self.assertNotIn(phrase, text)
            self.assertEqual(text.count(r"\documentclass"), 1)
            self.assertEqual(text.count(r"\begin{document}"), 1)
            self.assertEqual(text.count(r"\end{document}"), 1)
            self.assertFalse(has_unexpanded_inclusions(text))

    def test_subfileinclude_preserves_page_boundaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            root.mkdir()
            (root / "main.tex").write_text(
                "\\documentclass{article}\n\\begin{document}\n"
                "\\subfileinclude{chapter}\n\\end{document}")
            (root / "chapter.tex").write_text("Included chapter. " * 30)
            output = Path(tmp) / "flat.tex"
            flatten(root, "2506.02070", "2506.02070v3", output)
            text = output.read_text()
            self.assertEqual(text.count(r"\clearpage"), 2)
            self.assertLess(text.index(r"\clearpage"), text.index("Included chapter."))
            self.assertGreater(text.rindex(r"\clearpage"), text.index("Included chapter."))

    def test_missing_subfile_fails_without_changing_source_or_writing_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            root.mkdir()
            main = root / "main.tex"
            text = "\\documentclass{article}\n" + "words " * 60 + "\n\\subfile{missing}\n"
            main.write_text(text)
            output = Path(tmp) / "flat.tex"
            with self.assertRaisesRegex(RuntimeError, "flatten"):
                flatten(root, "2506.02070", "2506.02070v3", output)
            self.assertEqual(main.read_text(), text)
            self.assertFalse(output.exists())

    def test_display_math_subfile_does_not_introduce_paragraphs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            root.mkdir()
            (root / "wrapped.tex").write_text(
                "\\documentclass[main.tex]{subfiles}\n\\begin{document}\nx=1\n\\end{document}\n")
            for calls in ("\\subfile{equation}", "  \\subfile{equation}  ",
                          "\\subfile{equation}\\subfile{equation}", "\\subfile{wrapped}"):
                (root / "main.tex").write_text(
                    "\\documentclass{article}\n\\begin{document}\n" + "words " * 60 +
                    "\n\\[\n" + calls + "\n\\]\n\\end{document}\n")
                (root / "equation.tex").write_text("x=1\n")
                output = Path(tmp) / "flat.tex"
                flatten(root, "2506.02070", "2506.02070v3", output)
                math = output.read_text().split(r"\[", 1)[1].split(r"\]", 1)[0]
                self.assertIn("x=1", math)
                self.assertNotIn("\n\n", math)
                self.assertFalse(any(not line.strip() for line in math.strip().splitlines()))

    def test_unused_malformed_sources_do_not_break_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            root.mkdir()
            (root / "main.tex").write_text(
                "\\documentclass{article}\n\\begin{document}\n" + "Main prose. " * 60 +
                "\n\\end{document}\n")
            (root / "backup.tex").write_text(
                "\\documentclass[main.tex]{subfiles}\nIncomplete old draft.")
            (root / "dynamic.tex").write_text(r"\subfile{\chaptername}")
            output = Path(tmp) / "flat.tex"
            flatten(root, "2506.02070", "2506.02070v3", output)
            self.assertIn("Main prose.", output.read_text())
            self.assertNotIn("Incomplete old draft", output.read_text())
            # Also exercise unused drafts when a valid, reachable subfile forces
            # normalization into a temporary tree.
            main = root / "main.tex"
            main.write_text(main.read_text().replace(r"\end{document}",
                            "\\subfile{valid}\n\\end{document}"))
            (root / "valid.tex").write_text("Valid child prose.")
            flatten(root, "2506.02070", "2506.02070v3", output)
            self.assertIn("Valid child prose.", output.read_text())

    def test_referenced_malformed_wrapper_still_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            root.mkdir()
            (root / "main.tex").write_text(
                "\\documentclass{article}\n" + "words " * 60 + "\n\\subfile{bad}\n")
            (root / "bad.tex").write_text(
                "\\documentclass[main.tex]{subfiles}\nMissing document body.")
            output = Path(tmp) / "flat.tex"
            with self.assertRaisesRegex(RuntimeError, "unexpanded source inclusion"):
                flatten(root, "2506.02070", "2506.02070v3", output)
            self.assertFalse(output.exists())

    def test_body_only_companions_leave_ordinary_inputs_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.tex").write_text(r"\subfile{child}")
            child = ("\\documentclass[main.tex]{subfiles}\n\\begin{document}\n"
                     "Child body.\n\\end{document}\n")
            (root / "child.tex").write_text(child)
            with normalized_source_tree(root) as prepared:
                self.assertEqual((prepared / "child.tex").read_text(), child)
                companions = list(prepared.glob("arxiv-subfiles-*-child.tex"))
                self.assertEqual(len(companions), 1)
                self.assertIn("Child body.", companions[0].read_text())
                self.assertNotIn(r"\documentclass", companions[0].read_text())

    def test_unexpanded_subfile_cannot_pass_successful_vendor_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.tex").write_text(r"\documentclass{article}" + "words " * 60)
            result = type("Run", (), {"returncode": 0, "stderr": b"", "stdout":
                          (r"\documentclass{article}" + "words " * 60 + r"\subfile{missing}").encode()})()
            with patch("arxiv_papers.flatten_tex.subprocess.run", return_value=result):
                with self.assertRaisesRegex(RuntimeError, "unexpanded source inclusion"):
                    flatten(root, "2506.02070", "2506.02070v3", root / "flat.tex")


if __name__ == "__main__":
    unittest.main()
