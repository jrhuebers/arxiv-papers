"""Adapt subfiles-package sources for the unmodified, bundled latexpand.

latexpand understands imports, but silently leaves \\subfile commands intact.
Normalize those commands in a disposable source tree, retaining import-relative
lookup and removing standalone subfile preambles only, never author files.
"""
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile

from .assets import literal_path
from .prune_macros import group_end, tokenize

SUBFILES = {r"\subfile", r"\subfileinclude"}
INCLUSIONS = SUBFILES | {r"\input", r"\include", r"\import", r"\subimport"}


def has_unexpanded_inclusions(text: str) -> bool:
    """Detect unresolved source calls, not examples in comments/verbatim."""
    try:
        tokens = tokenize(text)
    except ValueError:
        # Retain the previous conservative check when tokenization is uncertain.
        return bool(re.search(r"\\(?:input|include|subfile|subfileinclude|import|subimport)\s*\{[^}]+\}|"
                              r"\\documentclass(?:\[[^]]*\])?\s*\{subfiles\}", text))
    for i, token in enumerate(tokens):
        if token.value in SUBFILES and (i + 1 == len(tokens) or tokens[i + 1].value != "}"):
            return True  # Unsupported unbraced/dynamic calls must not pass either.
        if token.value in INCLUSIONS and i + 1 < len(tokens) and tokens[i + 1].value == "{":
            return True
        if token.value == r"\documentclass":
            try:
                if document_class(text[token.start:]) == "subfiles":
                    return True  # A child wrapper could not be normalized safely.
            except ValueError:
                pass
    return False


def document_class(text: str) -> str | None:
    """Read the literal class name without matching comments or verbatim."""
    # Stop at the header: ignored material after a child's end{document} may
    # contain incomplete drafts or verbatim and must not affect class detection.
    for match in re.finditer(r"\\documentclass\s*(?:\[[^]]*\]\s*)?\{[^{}]*\}", text):
        try:
            tokens = tokenize(text[:match.end()])
        except ValueError:
            continue  # A candidate inside an unfinished verbatim/code token.
        for i, token in enumerate(tokens):
            if token.value != r"\documentclass":
                continue
            j = i + 1
            if j < len(tokens) and tokens[j].value == "[":
                j = group_end(tokens, j, "[")
            after = group_end(tokens, j)
            return "".join(t.value for t in tokens[j + 1:after - 1])
    return None


def subfile_body(text: str) -> str:
    """Discard the standalone preamble/tail of a class{subfiles} document."""
    for match in re.finditer(r"\\end\s*\{\s*document\s*\}", text):
        try:
            tokens = tokenize(text[:match.end()])
        except ValueError:
            continue
        markers = []
        depth = 0
        for i, token in enumerate(tokens):
            if token.value == "{":
                depth += 1
            elif token.value == "}":
                depth -= 1
            elif depth == 0 and token.value in (r"\begin", r"\end"):
                if i + 1 >= len(tokens) or tokens[i + 1].value != "{":
                    continue
                after = group_end(tokens, i + 1)
                if "".join(t.value for t in tokens[i + 2:after - 1]) == "document":
                    markers.append((token.value, token.start, tokens[after - 1].end))
        if [marker[0] for marker in markers] == [r"\begin", r"\end"]:
            body = text[markers[0][2]:markers[1][1]]
            # LaTeX's begin ignores the immediate line-ending/indentation. Do
            # not turn that wrapper newline into a blank paragraph on import.
            return re.sub(r"\A[ \t]*(?:\r?\n[ \t]*)?", "", body)
    raise ValueError("subfiles document must have one begin/end{document} pair")


def normalize_subfiles(text: str, *, strip_document: bool = True,
                       companion_prefix: str = "") -> str:
    """Translate literal subfile calls to grouped imports for latexpand."""
    if r"\subfile" not in text and not (r"\documentclass" in text and "subfiles" in text):
        return text
    if strip_document and document_class(text) == "subfiles":
        text = subfile_body(text)
    tokens = tokenize(text)
    if any(t.value in SUBFILES for t in tokens) and any(
            t.value in (r"\lstinline", r"\mintinline") for t in tokens):
        raise ValueError("cannot safely normalize subfiles with unsupported inline-code syntax")
    changes = []
    for i, token in enumerate(tokens):
        if token.value not in SUBFILES:
            continue
        after = group_end(tokens, i + 1)
        raw = text[tokens[i + 1].end:tokens[after - 1].start]
        target = literal_path(raw)
        if target is None or PurePosixPath(target).is_absolute():
            raise ValueError(f"unsupported subfile path: {raw!r}")
        path = PurePosixPath(target)
        directory = path.parent.as_posix().rstrip("/") + "/"
        # Separate calls onto lines: latexpand expands only one directive per
        # line. Grouping matches subfiles' local definitions; include adds pages.
        name = path.name
        if companion_prefix:
            name = companion_prefix + name + (".tex" if not path.suffix else "")
        lines = [r"\begingroup", f"\\subimport{{{directory}}}{{{name}}}", r"\endgroup"]
        if token.value == r"\subfileinclude":
            lines = [r"\clearpage"] + lines + [r"\clearpage"]
        end = tokens[after - 1].end
        # Reuse existing boundaries: an extra blank line would insert a TeX
        # paragraph and can break a subfile used inside display mathematics.
        before = text[text.rfind("\n", 0, token.start) + 1:token.start]
        next_newline = text.find("\n", end)
        after_line = text[end:next_newline if next_newline >= 0 else len(text)]
        leading = "" if not before.strip() else "\n"
        trailing = "" if not after_line.strip() else "\n"
        if changes and not text[changes[-1][1]:token.start].strip() and changes[-1][2].endswith("\n"):
            leading = ""  # Adjacent inline calls share their generated boundary.
        replacement = leading + "\n".join(lines) + trailing
        changes.append((token.start, end, replacement))
    for start, end, replacement in reversed(changes):
        text = text[:start] + replacement + text[end:]
    return text


@contextmanager
def normalized_source_tree(root: Path):
    """Yield a source tree suitable for latexpand, copying only when needed."""
    with tempfile.TemporaryDirectory(prefix="arxiv-subfiles-") as tmp:
        prefix = Path(tmp).name + "-"
        originals = {}
        changes = {}
        for path in root.rglob("*.tex"):
            if path.is_symlink():
                continue
            relative = path.relative_to(root)
            text = path.read_text(encoding="utf-8", errors="replace")
            originals[relative] = text
            try:
                normalized = normalize_subfiles(text, strip_document=False, companion_prefix=prefix)
            except ValueError:
                # An unused draft must not break the selected main. If reached,
                # its unchanged subfile calls/wrappers fail final validation.
                normalized = text
            if normalized != text:
                changes[relative] = normalized
        if not changes:
            yield root
            return
        source = Path(tmp) / "source"
        shutil.copytree(root, source)
        for relative, normalized in changes.items():
            (source / relative).write_text(normalized, encoding="utf-8")
        # Body-only companions preserve ordinary input semantics: only calls
        # made through subfile load a body instead of the original document.
        for relative, text in originals.items():
            try:
                body = normalize_subfiles(text, companion_prefix=prefix)
            except ValueError:
                body = text  # Fail only if this unnormalized child is reached.
            companion = relative.with_name(prefix + relative.name)
            (source / companion).write_text(body, encoding="utf-8")
        yield source
