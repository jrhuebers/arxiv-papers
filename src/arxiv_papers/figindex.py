#!/usr/bin/env python3
"""Build one searchable figure index inside each paper directory."""
import glob
import os
import re
import sys
from datetime import date
from pathlib import Path

from .qa_corpus import source_files


def brace_match(text, start):
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "\\":
            continue
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def strip_tex_noise(text):
    text = re.sub(r"\\label\{[^}]*\}", "", text)
    text = re.sub(r"\\(?:textbf|textit|emph|textrm|texttt)\{([^{}]*)\}", r"\1", text)
    text = text.replace("``", "“").replace("''", "”")
    text = re.sub(r"`([^`\n]+)'", r"“\1”", text)
    text = text.replace(r"\textasciitilde", "~").replace(r"\%", "%").replace(r"\&", "&")
    return re.sub(r"\s+", " ", text.replace("\n", " ")).strip()


def parse_figures(tex):
    figures = []
    environment = re.compile(r"\\begin\{(figure\*?)\}(.*?)\\end\{\1\}", re.S)
    for match in environment.finditer(tex):
        body = match.group(2)
        images = [target.strip() for target in re.findall(r"\\includegraphics\s*\*?\s*(?:\[[^]]*\]\s*)?\{([^}]*)\}", body)]
        caption_match = re.search(r"\\caption(?:\[[^]]*\])?\{", body)
        caption = ""
        if caption_match:
            end = brace_match(body, caption_match.end() - 1)
            if end != -1:
                caption = strip_tex_noise(body[caption_match.end():end])
        label_match = re.search(r"\\label\{([^}]*)\}", body)
        figures.append({"images": images, "caption": caption, "label": label_match.group(1) if label_match else ""})
    return figures


def paper_dirs(paths):
    for raw in paths:
        root = Path(raw)
        if any(root.glob("*.tex")):
            yield root
        else:
            yield from sorted(path for path in root.iterdir() if path.is_dir() and any(path.glob("*.tex")))


def locate_image(paper, target, *, canonical=False):
    clean = target.strip()
    if Path(clean).is_absolute() or ".." in Path(clean).parts:
        return None
    clean = clean.removeprefix("./")
    # New flattened references already point into the materialized figures tree.
    if canonical and clean.startswith("figures/") and (paper / clean).is_file():
        return clean
    direct = paper / "figures" / clean
    if direct.is_file():
        return direct.relative_to(paper).as_posix()
    for extension in (".pdf", ".png", ".jpg", ".jpeg", ".eps", ".svg"):
        candidate = paper / "figures" / f"{clean}{extension}"
        if candidate.is_file():
            return candidate.relative_to(paper).as_posix()
    if clean.startswith("figures/") and (paper / clean).is_file():
        return clean
    matches = list((paper / "figures").rglob(Path(clean).name)) if (paper / "figures").is_dir() else []
    return matches[0].relative_to(paper).as_posix() if len(matches) == 1 else None


def build_index(paper):
    aid = paper.name
    tex_path = paper / f"{aid}.tex"
    if not tex_path.is_file():
        tex_path = next(iter(sorted(paper.glob("*.tex"))), None)
    if tex_path is None:
        print(f"{paper}: no tex file, skipped")
        return
    tex = tex_path.read_text(encoding="utf-8", errors="replace")
    figures = parse_figures(tex)
    remapped = "+asset-paths-remapped" in tex.splitlines()[0] if tex.splitlines() else False
    source = source_files(paper, aid)
    source_name = source[0].name if len(source) == 1 else f"{aid}.tar.gz"
    lines = [f"# Figures — {aid}", "", f"Generated {date.today()} from {tex_path.name}. Figure numbers are source order, not necessarily PDF numbering.", ""]
    no_caption = no_image = 0
    for number, figure in enumerate(figures, 1):
        if not figure["caption"]:
            no_caption += 1
        if not figure["images"]:
            no_image += 1
        label = f" ({figure['label']})" if figure["label"] else ""
        lines.append(f"## Figure {number}{label}")
        if figure["images"]:
            for target in figure["images"]:
                materialized = locate_image(paper, target, canonical=remapped)
                source_target = target.removeprefix("figures/") if remapped and target.startswith("figures/") else target
                lines.append(f"- image: {materialized or '_(not materialized; embedded or unavailable)_'}")
                lines.append(f"- source: {source_name} -> {source_target}")
        else:
            lines.append("- image: _(embedded in TeX or unavailable)_")
        lines.append(f"- caption: {figure['caption'] or '_(no caption)_'}")
        lines.append("")
    if not figures:
        lines.append("_(no figure environments found)_")
    (paper / "FIGURES.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"{paper}: {len(figures)} figures -> {paper / 'FIGURES.md'}")
    print(f"   (no caption: {no_caption}, no includegraphics: {no_image})")


def main():
    dirs = sys.argv[1:]
    if not dirs:
        raise SystemExit("usage: figindex.py <papers_dir> [papers_dir2 ...]")
    for paper in paper_dirs(dirs):
        build_index(paper)


if __name__ == "__main__":
    main()
