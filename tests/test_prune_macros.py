"""Offline dead-macro parser, safety and full-file integration tests."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from arxiv_papers.flatten_tex import flatten, strip_comments
from arxiv_papers.prune_macros import analyze, external_macro_names, prune_unused_macros, tokenize


def paper(preamble, body="text"):
    return "\\documentclass{article}\n" + preamble + "\n\\begin{document}\n" + body + "\n\\end{document}\n"


class PruneMacroTests(unittest.TestCase):
    def test_basic_forms_and_control_symbols(self):
        text = paper(r"\def\dead#1{#1}\def\1{one}\newcommand*{\unused}[1]{#1}"
                     r"\DeclareMathOperator*{\argmin}{argmin}\newcommand\used{yes}", r"\used")
        result = prune_unused_macros(text)
        for name in (r"\dead", r"\1", r"\unused", r"\argmin"):
            self.assertNotIn(name, result)
        self.assertIn(r"\newcommand\used{yes}", result)

    def test_dependencies_defaults_and_unused_cycles(self):
        preamble = (r"\newcommand\leaf{value}\newcommand\helper{\leaf}"
                    r"\newcommand\used[1][\helper]{#1}"
                    r"\newcommand\deada{\deadb}\newcommand\deadb{\deada}")
        result = prune_unused_macros(paper(preamble, r"\used"))
        for name in (r"\leaf", r"\helper", r"\used"):
            self.assertIn(name, result)
        self.assertNotIn(r"\newcommand\deada", result)
        self.assertNotIn(r"\newcommand\deadb", result)

    def test_duplicates_are_not_uses_and_live_duplicates_stay(self):
        dead = paper(r"\def\unused{a}\def\unused{b}")
        self.assertNotIn(r"\unused", prune_unused_macros(dead))
        live = paper(r"\def\used{a}\def\used{b}", r"\used")
        self.assertEqual(prune_unused_macros(live), live)

    def test_protected_declarations_retain_dependencies(self):
        text = paper(r"\newcommand\helper{hi}\renewcommand\title{\helper}"
                     r"\newcommand\aliashelper{hi}\let\alias=\aliashelper"
                     r"\def\UrlFont{\helper}\def\thefoo{foo}\def\internal@foo{foo}")
        self.assertEqual(prune_unused_macros(text), text)

    def test_scopes_conditionals_environments_and_nested_definitions(self):
        preamble = (r"{\def\local{a}}\begingroup\def\localtwo{b}\endgroup"
                    r"\bgroup\def\localthree{c}\egroup"
                    r"\iftrue\def\conditional{d}\fi"
                    r"\begin{foo}\newcommand\inside{e}\end{foo}"
                    r"\AtBeginDocument{\def\hook{f}}"
                    r"\newcommand\setup{\def\temporary{g}\temporary}")
        text = paper(preamble, r"\setup\newcommand\bodymacro{h}")
        self.assertEqual(prune_unused_macros(text), text)

    def test_unsupported_definitions_are_roots(self):
        text = paper(r"\newcommand\helper{h}\edef\unused{\helper}\gdef\another{\helper}")
        self.assertEqual(prune_unused_macros(text), text)

    def test_prefixes_and_literal_command_tokens(self):
        for prefix in (r"\global", r"\long", r"\outer", r"\protected", r"\expandafter",
                       r"\global\long", r"\string", r"\noexpand", r"\meaning", r"\show"):
            text = paper(prefix + " % ignored\n" + r"\def\unused{X}\count0=1")
            self.assertEqual(prune_unused_macros(text), text, prefix)

    def test_dynamic_names_and_tokenization_changes_disable_pruning(self):
        for syntax in (r"\csname foo\string b ar\endcsname", r"\@nameuse{unused}",
                       r"\catcode`\@=11", r"\ExplSyntaxOn", r"\scantokens{stuff}",
                       r"\afterassignment\foo", r"\lstinline|code|", r"\mintinline{tex}|code|"):
            text = paper(r"\newcommand\unused{value}", syntax)
            self.assertEqual(prune_unused_macros(text), text)

    def test_literal_csname_reference(self):
        text = paper(r"\newcommand\used{v}\newcommand\unused{u}", r"\csname used\endcsname")
        result = prune_unused_macros(text)
        self.assertIn(r"\newcommand\used{v}", result)
        self.assertNotIn(r"\unused", result)

    def test_inline_and_environment_verbatim_are_opaque(self):
        for code in (r"\verb|\newcommand\fake{a % }|", r"\verb*|% { \unused|",
                     "\\begin{verbatim}\n% } \\def\\fake{foo}\n\\end{verbatim}",
                     "\\begin{lstlisting}\n\\def\\fake{foo} % {\n\\end{lstlisting}",
                     "\\begin{minted}{tex}\n\\newcommand\\fake{foo} % {\n\\end{minted}"):
            text = paper(r"\newcommand\unused{value}", code)
            result = prune_unused_macros(text)
            self.assertIn(code, result)
            self.assertNotIn(r"\newcommand\unused", result)

    def test_comments_escapes_multiline_and_optional_braces(self):
        text = paper("\\newcommand% comment\n{\\used}% comment\n[1][{a]b}]{\\{#1\\}}\n"
                     "\\def\\unused#1;{\\% #1}\n", r"\used")
        result = prune_unused_macros(text)
        self.assertNotIn(r"\unused", result)
        self.assertIn("[1][{a]b}]{\\{#1\\}}", result)

    def test_deferred_group_commands_do_not_disable_pruning(self):
        text = paper(r"\newcommand\dead{d}\providecommand\doi{\begingroup\Url}")
        result = prune_unused_macros(text)
        self.assertNotIn(r"\dead", result)
        self.assertIn(r"\providecommand\doi{\begingroup\Url}", result)

    def test_at_catcode_ambiguity_preserves_possible_calls(self):
        text = paper(r"\newcommand\foo{Text}\newcommand\internal@foo{Text}", r"\foo@literal")
        self.assertEqual(prune_unused_macros(text), text)
        text = paper(r"\newcommand\foo{Text}")
        self.assertEqual(prune_unused_macros(text, protected_names={r"\foo@literal"}), text)

    def test_name_boundaries(self):
        text = paper(r"\newcommand\foo{a}\newcommand\foobar{b}", r"\foobar")
        result = prune_unused_macros(text)
        self.assertNotIn(r"\newcommand\foo{", result)
        self.assertIn(r"\newcommand\foobar{b}", result)

    def test_uncertain_syntax_leaves_input_unchanged(self):
        for text in (r"\newcommand{\bad", r"\def\bad#1}", r"\newcommand{\bad\name}{foo}",
                     r"\newcommand\dead{good} {broken", r"\verb|unterminated"):
            self.assertEqual(prune_unused_macros(text), text)

    def test_deletion_does_not_join_neighboring_tokens(self):
        text = paper(r"left\newcommand\dead{x}word")
        self.assertIn("left\nword", prune_unused_macros(text))

    def test_literal_bracket_in_optional_default_does_not_swallow_document(self):
        text = paper(r"\newcommand{\unused}[1][[]{#1}", "]{Visible}")
        result = prune_unused_macros(text)
        self.assertNotIn(r"\unused", result)
        self.assertIn(r"\begin{document}", result)
        self.assertIn("]{Visible}", result)

    def test_alltt_and_tex_escape_references_are_not_lost(self):
        text = paper(r"\usepackage{alltt}\newcommand\keep{Text}",
                     r"\begin{alltt}\keep\end{alltt}")
        self.assertEqual(prune_unused_macros(text), text)
        text = paper(r"\newcommand\keep{Text}\newcommand\dead{Unused}",
                     r"\begin{lstlisting}[escapeinside={(*}{*)}](*\keep*)\end{lstlisting}")
        result = prune_unused_macros(text)
        self.assertIn(r"\newcommand\keep{Text}", result)
        self.assertNotIn(r"\dead", result)
        for environment in ("alltt", "lstlisting", "minted"):
            for whitespace in (" ", "\n"):
                text = paper(r"\newcommand\keep{Text}",
                             "\\begin" + whitespace + "{" + environment + "}\\keep\\end{" + environment + "}")
                self.assertIn(r"\newcommand\keep{Text}", prune_unused_macros(text))

    @unittest.skipUnless(shutil.which("latexpand"), "latexpand not installed")
    def test_unsupported_inline_code_survives_full_cleanup(self):
        for code in (r"\lstinline|100% literal| tail", r"\mintinline{tex}{100% literal} tail"):
            self.assertEqual(strip_comments(code), code)
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "source"
                root.mkdir()
                (root / "main.tex").write_text(paper("", code + " text" * 60))
                out = Path(tmp) / "out.tex"
                flatten(root, "1706.03762", "1706.03762v1", out)
                self.assertIn(code, out.read_text())

    def test_aliased_prefixes_and_conditionals_disable_pruning(self):
        for aliases, use in ((r"\let\myglobal\global", r"\myglobal\def\unused{X}\message{hi}"),
                             (r"\let\cond\iftrue", r"\cond\def\unused{X}\fi"),
                             (r"\let\a\global\let\b\a", r"\b\def\unused{X}"),
                             (r"\def\myglobal{\global}", r"\myglobal\def\unused{X}")):
            text = paper(aliases + use)
            self.assertEqual(prune_unused_macros(text), text)

    def test_off_and_idempotence(self):
        text = paper(r"\def\dead{x}\newcommand\used{hi}", r"\used")
        self.assertEqual(prune_unused_macros(text, "off"), text)
        once = prune_unused_macros(text)
        self.assertEqual(prune_unused_macros(once), once)
        with self.assertRaises(ValueError):
            prune_unused_macros(text, "bogus")

    def test_external_hook_and_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "local.sty").write_text(r"\newcommand\render{\customhook}")
            text = paper(r"\newcommand\helper{h}\newcommand\customhook{\helper}\newcommand\dead{d}")
            result = prune_unused_macros(text, protected_names=external_macro_names(root))
            self.assertIn(r"\customhook", result)
            self.assertIn(r"\helper", result)
            self.assertNotIn(r"\dead", result)

    def test_external_indirect_names_are_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            style = root / "local.sty"
            style.write_text(r"\csname helper\endcsname\csname foo#1\endcsname\csname #1mark\endcsname")
            text = paper(r"\newcommand\helper{h}\newcommand\foobar{f}\newcommand\xmark{x}\newcommand\dead{d}")
            result = prune_unused_macros(text, protected_names=external_macro_names(root))
            for name in (r"\helper", r"\foobar", r"\xmark"):
                self.assertIn(name, result)
            self.assertNotIn(r"\dead", result)
            style.write_text(r"\csname\computed\endcsname")
            self.assertEqual(prune_unused_macros(text, protected_names=external_macro_names(root)), text)

    @unittest.skipUnless(shutil.which("latexpand"), "latexpand not installed")
    def test_final_pass_sees_uses_across_inputs_and_keeps_originals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            root.mkdir()
            main = root / "main.tex"
            main.write_text("\\documentclass{article}\n\\input{macros}\n\\begin{document}\n\\input{body}\n\\end{document}\n")
            macros = root / "macros.tex"
            macros.write_text(r"\newcommand\used{hi}\newcommand\unused{bye}")
            (root / "body.tex").write_text(r"\used \verb|100% literal| " + "text " * 60)
            before = {p: p.read_bytes() for p in root.iterdir()}
            out = Path(tmp) / "flattened.tex"
            flatten(root, "1706.03762", "1706.03762v1", out)
            self.assertIn(r"\newcommand\used{hi}", out.read_text())
            self.assertNotIn(r"\unused", out.read_text())
            self.assertIn(r"\verb|100% literal|", out.read_text())
            self.assertEqual(before, {p: p.read_bytes() for p in root.iterdir()})
            off = Path(tmp) / "off.tex"
            flatten(root, "1706.03762", "1706.03762v1", off, prune_macros="off")
            self.assertIn(r"\unused", off.read_text())

    @unittest.skipUnless(shutil.which("pdflatex") and importlib.util.find_spec("fitz"), "TeX/PDF tools unavailable")
    def test_compiled_rendering_is_unchanged(self):
        text = paper(r"\newcommand\helper{Hello}\newcommand\used{\helper}"
                     r"\newcommand\dead[1]{#1}\def\unused{\missing}"
                     r"\renewcommand\figurename{Diagram}",
                     r"\used world.\begin{figure}\caption{A caption}\end{figure}")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, source in (("before", text), ("after", prune_unused_macros(text))):
                (root / f"{name}.tex").write_text(source)
                subprocess.run(["pdflatex", "-interaction=batchmode", "-halt-on-error", f"{name}.tex"],
                               cwd=root, capture_output=True, timeout=60, check=True)
            import fitz
            with fitz.open(root / "before.pdf") as before, fitz.open(root / "after.pdf") as after:
                self.assertEqual([page.get_text() for page in before], [page.get_text() for page in after])
                self.assertEqual([page.get_pixmap().samples for page in before],
                                 [page.get_pixmap().samples for page in after])


if __name__ == "__main__":
    unittest.main()
