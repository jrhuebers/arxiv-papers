#!/usr/bin/env python3
"""Verify one-directory-per-paper arXiv corpus artifacts."""
import argparse
from pathlib import Path
import re
import tarfile


def source_files(directory: Path, aid: str) -> list[Path]:
    excluded = {f"{aid}.pdf", f"{aid}.tex"}
    return sorted(path for path in directory.iterdir()
                  if path.is_file() and path.name.startswith(f"{aid}.") and path.name not in excluded)


def check_paper(directory: Path, aid: str | None = None, version: str | None = None,
                canonical_id: str | None = None) -> list[str]:
    aid = aid or directory.name
    canonical_id = canonical_id or aid
    errors = []
    pdf = directory / f"{aid}.pdf"
    tex = directory / f"{aid}.tex"
    marker = directory / "source-unavailable.txt"
    if not pdf.is_file() or pdf.stat().st_size < 10_000:
        errors.append("missing/invalid PDF")
    else:
        with pdf.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                errors.append("missing/invalid PDF")
    if marker.exists():
        record = marker.read_text(encoding="utf-8")
        expected = re.escape(version) if version else re.escape(aid) + r"v[0-9]+"
        if not re.search(rf"^arXiv {expected} \| source unavailable", record):
            errors.append("source-unavailable marker has missing/wrong version")
        if tex.exists():
            errors.append("source-unavailable marker contradicts TeX")
        return errors
    if not tex.is_file() or tex.stat().st_size < 200:
        errors.append("missing/tiny flattened TeX")
    else:
        text = tex.read_text(encoding="utf-8", errors="replace")
        if not re.search(r"\\document(?:class|style)(?:\[[^]]*\])?\s*\{", text):
            errors.append("missing documentclass/documentstyle")
        expected = re.escape(version) if version else re.escape(aid) + r"v[0-9]+"
        if not re.search(rf"^% arXiv {re.escape(canonical_id)} \(latest: {expected}\)", text):
            errors.append("missing/mismatched version header")
        if re.search(r"\\(?:input|include)\s*\{[^}]+\}", text):
            errors.append("unexpanded TeX input/include")
    originals = source_files(directory, aid)
    if len(originals) != 1 or originals[0].stat().st_size == 0:
        errors.append("missing or ambiguous original source")
    elif originals[0].name.endswith(".tar.gz"):
        try:
            with tarfile.open(originals[0], "r:gz") as archive:
                if not archive.getmembers():
                    errors.append("empty source archive")
        except tarfile.TarError:
            errors.append("invalid source archive")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    papers = sorted(path for path in args.directory.iterdir() if path.is_dir() and any(path.glob("*.pdf")))
    if not papers and any(args.directory.glob("*.pdf")):
        papers = [args.directory]
    if not papers:
        parser.error("no paper directories found")
    failed = False
    for paper in papers:
        aid = paper.name if paper != args.directory else next(p.stem for p in paper.glob("*.pdf"))
        errors = check_paper(paper, aid)
        print(f"{aid}: {'FAIL: ' + '; '.join(errors) if errors else 'OK'}")
        failed |= bool(errors)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
