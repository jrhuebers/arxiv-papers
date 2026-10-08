#!/usr/bin/env python3
"""Build one citation-key/reference index inside each paper directory."""
import re
import sys
import tarfile
from datetime import date
from pathlib import Path

from .qa_corpus import source_files

BIBITEM = re.compile(r"\\bibitem(?:\[[^]]*\])?\{([^}]+)\}")
BIBENTRY = re.compile(r"@[A-Za-z]+\s*\{\s*([^,\s]+)\s*,")


def balanced_entry(text, start):
    opening = text.find("{", start)
    if opening < 0:
        return text[start:].strip()
    depth = 0
    escaped = False
    for position in range(opening, len(text)):
        char = text[position]
        if char == "{" and not escaped:
            depth += 1
        elif char == "}" and not escaped:
            depth -= 1
            if depth == 0:
                return text[start:position + 1].strip()
        escaped = char == "\\" and not escaped
        if char != "\\":
            escaped = False
    return text[start:].strip()


def parse_bbl(text):
    matches = list(BIBITEM.finditer(text))
    entries = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else text.find(r"\end{thebibliography}", match.end())
        if end < 0:
            end = len(text)
        entries.append((match.group(1).strip(), text[match.start():end].strip()))
    return entries


def parse_bib(text):
    return [(match.group(1).strip(), balanced_entry(text, match.start())) for match in BIBENTRY.finditer(text)]


def selected_bbls(tex, names):
    requested = []
    for group in re.findall(r"\\bibliography\{([^}]+)\}", tex):
        requested.extend(part.strip().removesuffix(".bbl") for part in group.split(","))
    exact = [name for name in names if Path(name).stem in requested]
    if exact:
        return exact
    if len(names) == 1:
        return names
    main = [name for name in names if Path(name).stem == "main"]
    return main or names


def read_archive_sources(paper, aid, tex):
    source = source_files(paper, aid)
    archive = source[0] if len(source) == 1 and source[0].name.endswith(".tar.gz") else None
    if archive is None:
        return [], []
    entries = []
    available = []
    with tarfile.open(archive, "r:gz") as source:
        members = {member.name: member for member in source.getmembers() if member.isfile()}
        bbls = sorted(name for name in members if name.endswith(".bbl"))
        bibs = sorted(name for name in members if name.endswith(".bib"))
        available = bbls + bibs
        for name in selected_bbls(tex, bbls):
            content = source.extractfile(members[name]).read().decode("utf-8", errors="replace")
            entries.extend((key, entry, f"{archive.name} -> {name}") for key, entry in parse_bbl(content))
        if not entries:
            for name in bibs:
                content = source.extractfile(members[name]).read().decode("utf-8", errors="replace")
                entries.extend((key, entry, f"{archive.name} -> {name}") for key, entry in parse_bib(content))
    return entries, available


def read_materialized_sources(paper, aid, tex):
    bibliography = paper / "bibliography"
    entries = []
    available = []
    if bibliography.is_dir():
        bbls = sorted(path for path in bibliography.rglob("*.bbl"))
        bibs = sorted(path for path in bibliography.rglob("*.bib"))
        available = [path.relative_to(paper).as_posix() for path in bbls + bibs]
        requested = []
        for group in re.findall(r"\\bibliography\{([^}]+)\}", tex):
            requested.extend(part.strip().removesuffix(".bbl") for part in group.split(","))
        selected = [path for path in bbls if path.stem in requested] or (bbls if len(bbls) == 1 else [path for path in bbls if path.stem == "main"])
        for path in selected:
            entries.extend((key, entry, path.relative_to(paper).as_posix()) for key, entry in parse_bbl(path.read_text(encoding="utf-8", errors="replace")))
        if not entries:
            for path in bibs:
                entries.extend((key, entry, path.relative_to(paper).as_posix()) for key, entry in parse_bib(path.read_text(encoding="utf-8", errors="replace")))
    return entries, available


def build_index(paper):
    paper = Path(paper)
    aid = paper.name
    tex_path = next(iter(sorted(paper.glob("*.tex"))), None)
    if tex_path is None:
        print(f"{paper}: no tex file, skipped")
        return
    tex = tex_path.read_text(encoding="utf-8", errors="replace")
    entries, available = read_materialized_sources(paper, aid, tex)
    if not entries:
        entries, available = read_archive_sources(paper, aid, tex)
    if not entries:
        entries = [(key, entry, tex_path.name) for key, entry in parse_bbl(tex)]
    lines = [f"# References — {aid}", "", f"Generated {date.today()} from {tex_path.name} and the paper-local bibliography assets.", ""]
    if available:
        lines.append("- bibliography files: " + "; ".join(available) + "")
        lines.append("")
    seen = set()
    for key, entry, source in entries:
        if key in seen:
            continue
        seen.add(key)
        lines.extend([f"## `{key}`", f"- source: {source}", "- entry:", "```latex", entry, "```", ""])
    if not entries:
        lines.append("_(no bibliography entries found; inspect the flattened TeX or paper archive)_")
    (paper / "REFERENCES.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"{paper}: {len(seen)} references -> {paper / 'REFERENCES.md'}")


def paper_dirs(paths):
    for raw in paths:
        root = Path(raw)
        if any(root.glob("*.tex")):
            yield root
        else:
            yield from sorted(path for path in root.iterdir() if path.is_dir() and any(path.glob("*.tex")))


def main():
    dirs = sys.argv[1:]
    if not dirs:
        raise SystemExit("usage: refindex.py <papers_dir> [papers_dir2 ...]")
    for paper in paper_dirs(dirs):
        build_index(paper)


if __name__ == "__main__":
    main()
