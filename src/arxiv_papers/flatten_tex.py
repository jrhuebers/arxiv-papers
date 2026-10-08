#!/usr/bin/env python3
"""Flatten an extracted arXiv source tree using latexpand, with a traceable header."""
import argparse
from datetime import date
from pathlib import Path
import re
import shutil
import subprocess

from .assets import rewrite_asset_paths
from .prune_macros import external_macro_names, prune_unused_macros, tokenize

VERBATIM = {"verbatim", "verbatim*", "Verbatim", "lstlisting", "minted", "alltt"}
TOKEN = re.compile(r"\\(?:begin|end)\{([A-Za-z*]+)\}|%")
DOC = re.compile(r"^[^%\n]*\\document(?:class|style)(?:\[[^]]*\])?\s*\{", re.M)


def strip_comments(text: str) -> str:
    """Strip ordinary comments; preserve escaped percent and block/inline code."""
    try:
        tokens = tokenize(text)
    except ValueError:
        return text  # Uncertain verbatim syntax must not lose source contents.
    if any(t.value in (r"\lstinline", r"\mintinline") for t in tokens):
        return text  # Their configurable delimiters need package-specific parsing.
    out = []
    pos = 0
    for token in tokens:
        # The tokenizer omits comments and whitespace, but preserves escaped
        # percent and verbatim as tokens. Only token gaps can contain comments.
        out.append(re.sub(r"%[^\n]*", "", text[pos:token.start]))
        out.append(text[token.start:token.end])
        pos = token.end
    out.append(re.sub(r"%[^\n]*", "", text[pos:]))
    return "".join(out)


def collapse_blank_lines(text: str) -> str:
    """Reduce outside-code runs of blank lines to one; preserve verbatim contents."""
    out = []
    verbatim = None
    blank_lines = 0
    for line in text.splitlines(keepends=True):
        was_verbatim = verbatim is not None
        for match in TOKEN.finditer(line):
            token = match.group()
            if token.startswith(r"\begin{") and match.group(1) in VERBATIM and verbatim is None:
                verbatim = match.group(1)
            elif token.startswith(r"\end{") and match.group(1) == verbatim:
                verbatim = None
        if was_verbatim or verbatim is not None:
            blank_lines = 0
            out.append(line)
        elif not line.strip():
            blank_lines += 1
            if blank_lines <= 1:
                out.append(line)
        else:
            blank_lines = 0
            out.append(line)
    return "".join(out)


def find_main(root: Path) -> Path:
    candidates = []
    for path in root.rglob("*.tex"):
        if path.is_symlink():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if DOC.search(text):
            candidates.append((path.stat().st_size, path))
    if not candidates:
        raise ValueError("no LaTeX main file containing \\documentclass or \\documentstyle")
    return max(candidates)[1]


def flatten(root: Path, aid: str, version: str, output: Path, *, prune_macros: str = "safe",
            asset_map: dict[str, str] | None = None) -> Path:
    tool = shutil.which("perl")
    if not tool:
        raise RuntimeError("Perl is required to run bundled latexpand; install perl with your OS package manager")
    vendor = Path(__file__).with_name("_vendor") / "latexpand"
    if not vendor.is_file():
        raise RuntimeError("bundled latexpand is missing; reinstall arxiv-papers")
    main = find_main(root)
    # --empty-comments destroys literal % inside verbatim; keep comments until our verbatim-aware pass.
    # Most archives keep the main file beside its inputs. Some (e.g. ECHO) place
    # the main .tex one directory below the source root while inputs remain root-relative.
    attempts = [(main.parent, main.name)]
    if main.parent != root:
        attempts.append((root, main.relative_to(root).as_posix()))
    errors = []
    clean = ""
    for cwd, tex_path in attempts:
        run = subprocess.run([tool, str(vendor), "--keep-comments", tex_path], cwd=cwd, capture_output=True, timeout=120, check=False)
        warnings = run.stderr.decode(errors="replace")
        if run.returncode or not run.stdout:
            errors.append(warnings[:1000] or f"latexpand exited {run.returncode} with empty output")
            continue
        candidate = collapse_blank_lines(strip_comments(run.stdout.decode("utf-8", errors="replace")))
        # Some latexpand versions warn and exit successfully while leaving unresolved inputs.
        if re.search(r"\\(?:input|include)\s*\{[^}]+\}", candidate):
            errors.append(warnings[:1000] or f"unexpanded input/include when running from {cwd}")
            continue
        if re.search(r"(?:not found|cannot open|no such file|can't open|could not find file)", warnings, re.I):
            errors.append(warnings[:1000])
            continue
        clean = candidate
        break
    else:
        raise RuntimeError("latexpand failed to flatten source: " + "; ".join(errors))
    if not DOC.search(clean) or len(clean) < 200:
        raise ValueError("flattened LaTeX is empty or missing documentclass")
    # Source-relative assets were materialized before flattening. Repair import
    # prefixes and map literal references before the final dead-macro pass.
    if asset_map is not None:
        clean = rewrite_asset_paths(clean, asset_map, cwd=cwd.relative_to(root).as_posix())
    # Final cleanup: search the complete single-file source, never individual inputs.
    clean = prune_unused_macros(clean, prune_macros, external_macro_names(root))
    clean = collapse_blank_lines(clean)
    pruning = "+safe-macro-pruning" if prune_macros == "safe" else ""
    assets = "+asset-paths-remapped" if asset_map is not None else ""
    header = (f"% arXiv {aid} (latest: {version}) | flattened+comments-stripped+blank-lines-collapsed{assets}{pruning} by latexpand --keep-comments + flatten_tex.py\n"
              f"% main: {main.relative_to(root)} | fetched: {date.today().isoformat()}\n")
    output.write_text(header + clean, encoding="utf-8")
    return main


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("source", type=Path)
    p.add_argument("id")
    p.add_argument("version")
    p.add_argument("output", type=Path)
    p.add_argument("--prune-macros", choices=("safe", "off"), default="safe")
    args = p.parse_args()
    print(f"main: {flatten(args.source.resolve(), args.id, args.version, args.output, prune_macros=args.prune_macros)}")


if __name__ == "__main__":
    main()
