#!/usr/bin/env python3
"""Fetch version-pinned arXiv papers into self-contained directories."""
from contextlib import contextmanager
from datetime import date, datetime
import gzip
import http.client
from html.parser import HTMLParser
import io
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .assets import copy_assets
from .download import MAX_BYTES, UA, request
from .flatten_tex import flatten
from .qa_corpus import check_paper

ID = re.compile(r"(?P<bare>(?:[0-9]{4}\.[0-9]{4,5}|[a-zA-Z][a-zA-Z0-9.-]*/[0-9]{7}))(?P<version>v[1-9][0-9]*)?$")
ATOM = "{http://www.w3.org/2005/Atom}"
MAX_SOURCE_MEMBERS = 10_000


def parse_id(aid: str) -> tuple[str, str | None]:
    match = ID.fullmatch(aid)
    if not match:
        raise ValueError(f"invalid arXiv ID: {aid}")
    return match["bare"], match["version"]


@contextmanager
def fetching_stage(aid: str, stage: str):
    """Keep failures attributable to a paper and pipeline stage."""
    try:
        yield
    except Exception as exc:
        raise RuntimeError(f"{stage} failed for {aid}: {exc}") from exc


class AbsMetadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values: dict[str, list[str]] = {}
        self.text: list[str] = []
        self.history_depth = 0
        self.history_strong = False
        self.in_anchor = False
        self.selected_versions: list[str] = []

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "div":
            if self.history_depth:
                self.history_depth += 1
            elif "submission-history" in fields.get("class", "").split():
                self.history_depth = 1
        if tag == "a":
            self.in_anchor = True
        if tag == "strong" and self.history_depth:
            self.history_strong = True
        if tag == "meta":
            name = fields.get("name", fields.get("property", ""))
            self.values.setdefault(name, []).append(fields.get("content", ""))

    def handle_endtag(self, tag):
        if tag == "a":
            self.in_anchor = False
        if tag == "strong":
            self.history_strong = False
        if tag == "div" and self.history_depth:
            self.history_depth -= 1

    def handle_data(self, data):
        self.text.append(data)
        if self.history_strong and not self.in_anchor:
            match = re.fullmatch(r"\s*\[(v[1-9][0-9]*)\]\s*", data)
            if match:
                self.selected_versions.append(match[1])


def _iso_date(value: str) -> str:
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%a, %d %b %Y", "%d %b %Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError as exc:
        raise ValueError(f"missing/invalid submission date: {value!r}") from exc


def _html_metadata(aid: str) -> dict:
    bare, requested = parse_id(aid)
    parser = AbsMetadata()
    parser.feed(request(f"https://arxiv.org/abs/{aid}").decode("utf-8", errors="replace"))
    text = " ".join(" ".join(parser.text).split())
    history = list(re.finditer(
        r"\[v([1-9][0-9]*)\]\s*((?:[A-Za-z]{3},\s*)?[0-9]{1,2}\s+[A-Za-z]{3}\s+[0-9]{4})(.*?)(?=\[v[0-9]+\]|$)", text))
    versions = {"v" + m[1]: _iso_date(m[2]) for m in history}
    selected = parser.selected_versions or ["v" + m[1] for m in history if "this version" in m[3]]
    def first(name):
        return parser.values.get(name, [""])[0]
    # A selected history entry or versioned citation identifier is evidence of
    # the page's actual revision. Never silently accept a latest-version page.
    cited = first("citation_arxiv_id")
    cited_match = ID.fullmatch(cited)
    resolved = (cited_match["version"] if cited_match and cited_match["bare"] == bare else None)
    if not resolved and len(selected) == 1:
        resolved = selected[0]
    if not resolved and not requested and versions:
        resolved = max(versions, key=lambda v: int(v[1:]))
    if not resolved or (requested and resolved != requested):
        raise ValueError(f"missing or inconsistent arXiv version metadata for {aid}")
    first_date = versions.get("v1") or _iso_date(first("citation_date"))
    revision_date = versions.get(resolved)
    if not revision_date:
        revision_date = first_date if resolved == "v1" else _iso_date(first("citation_online_date"))
    return dict(arxiv_id=bare, version=resolved,
                title=" ".join(first("citation_title").split()),
                authors=parser.values.get("citation_author", []),
                abstract=" ".join(first("citation_abstract").split()), tags=[],
                first_submitted=first_date, version_submitted=revision_date)


def metadata(aid: str) -> dict:
    """Resolve metadata, honoring an explicitly requested revision exactly."""
    bare, requested = parse_id(aid)
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode({"id_list": aid, "max_results": 1})
    try:
        feed = ET.fromstring(request(url))
        entry = feed.find(f"{ATOM}entry")
        if entry is None:
            raise ValueError(f"arXiv ID not found: {aid}")
        identifier = entry.findtext(f"{ATOM}id", "").split("/abs/", 1)[-1]
        resolved_bare, resolved = parse_id(identifier)
        if resolved_bare != bare or not resolved or (requested and resolved != requested):
            raise ValueError(f"missing or inconsistent arXiv version metadata for {aid}: {identifier}")
        result = dict(arxiv_id=bare, version=resolved,
                      title=" ".join(entry.findtext(f"{ATOM}title", "").split()),
                      authors=[" ".join(a.findtext(f"{ATOM}name", "").split()) for a in entry.findall(f"{ATOM}author")],
                      abstract=" ".join(entry.findtext(f"{ATOM}summary", "").split()), tags=[],
                      first_submitted=_iso_date(entry.findtext(f"{ATOM}published", "")),
                      version_submitted=_iso_date(entry.findtext(f"{ATOM}updated", "")))
    except (urllib.error.URLError, OSError, EOFError, http.client.IncompleteRead, ET.ParseError) as exc:
        if isinstance(exc, urllib.error.HTTPError) and exc.code not in (406, 408, 429, 500, 502, 503, 504):
            raise
        result = _html_metadata(aid)
    if not result["title"] or not result["authors"] or any(not author.strip() for author in result["authors"]):
        raise ValueError(f"missing or inconsistent arXiv metadata for {aid}")
    return result


def extract_source(data: bytes, root: Path) -> str:
    """Extract bounded regular members, rejecting links and traversals.

    Bound decompression before parsing tar headers (including extended headers),
    then iterate members rather than materializing an entire member list.
    """
    root.mkdir()
    if len(data) > MAX_BYTES:
        raise ValueError("source download too large")
    if data.startswith(b"\x1f\x8b"):
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
            text = stream.read(MAX_BYTES + 1)
        if len(text) > MAX_BYTES:
            raise ValueError("decompressed source too large")
        try:
            archive = tarfile.open(fileobj=io.BytesIO(text), mode="r|")
        except tarfile.ReadError:
            if not re.search(rb"\\document(?:class|style)\b", text):
                raise ValueError("gzip source is not LaTeX")
            (root / "main.tex").write_bytes(text)
            return "tex.gz"
        with archive as tf:
            total = 0
            for count, member in enumerate(tf, 1):
                if count > MAX_SOURCE_MEMBERS:
                    raise ValueError("source archive has too many members")
                name = PurePosixPath(member.name)
                if (name.is_absolute() or ".." in name.parts or not member.name or
                        not (member.isdir() or member.isfile())):
                    raise ValueError(f"unsafe source member: {member.name}")
                if member.isfile():
                    total += member.size
                    if total > MAX_BYTES or member.size < 0:
                        raise ValueError("source archive too large")
                    destination = root.joinpath(*name.parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with tf.extractfile(member) as source, destination.open("wb") as output:
                        shutil.copyfileobj(source, output, length=64 * 1024)
            return "tar.gz"
    if re.search(rb"\\document(?:class|style)\b", data[:200_000]):
        (root / "main.tex").write_bytes(data)
        return "tex"
    raise ValueError("arXiv source is neither tar.gz nor LaTeX (PDF/PS or error page?)")


def fetch_one(outdir: Path, aid: str, *, prune_macros: str = "safe") -> Path:
    """Fetch and atomically publish a revision; return its published directory.

    Old-style slashes are encoded as underscores in directory/file names.
    QA receives the encoded stem, full revision, and bare ``canonical_id``.
    """
    bare, requested = parse_id(aid)
    if prune_macros not in ("safe", "off"):
        raise ValueError("prune_macros must be 'safe' or 'off'")
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    with fetching_stage(aid, "metadata"):
        record = metadata(aid)
        if record["arxiv_id"] != bare or (requested and record["version"] != requested):
            raise ValueError(f"metadata does not match requested ID {aid}")
        full_id = bare + record["version"]
        if parse_id(full_id) != (bare, record["version"]):
            raise ValueError("invalid resolved version")
    stem = full_id.replace("/", "_")
    paper_dir = outdir / stem
    if paper_dir.exists():
        raise FileExistsError(f"paper directory already exists: {paper_dir}")
    with fetching_stage(aid, "PDF download"):
        pdf = request(f"https://arxiv.org/pdf/{full_id}")
        if not pdf.startswith(b"%PDF-"):
            raise ValueError("PDF endpoint did not return a PDF")
    with fetching_stage(aid, "source download"):
        try:
            source = request(f"https://arxiv.org/e-print/{full_id}")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            source = b""
    with tempfile.TemporaryDirectory(prefix=".paper-fetch-", dir=outdir) as tmp:
        stage = Path(tmp) / stem
        stage.mkdir()
        (stage / f"{stem}.pdf").write_bytes(pdf)
        (stage / "metadata.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (stage / "abstract.md").write_text(
            f"# {record['title']}\n\n{' ; '.join(record['authors'])}\n\n"
            f"arXiv: [{full_id}](https://arxiv.org/abs/{full_id})\n\n{record['abstract']}\n", encoding="utf-8")
        if not source or source.startswith((b"%PDF-", b"%!PS")):
            (stage / "source-unavailable.txt").write_text(
                f"arXiv {full_id} | source unavailable (404/PDF/PS); PDF only\n", encoding="utf-8")
        else:
            extracted = stage / "extracted"
            with fetching_stage(aid, "source extraction"):
                kind = extract_source(source, extracted)
            original_name = f"{stem}.tar.gz" if kind == "tar.gz" else (f"{stem}.source.tex" if kind == "tex" else f"{stem}.{kind}")
            (stage / original_name).write_bytes(source)
            with fetching_stage(aid, "asset materialization"):
                asset_map = copy_assets(extracted, stage)
            with fetching_stage(aid, "TeX flattening/asset remapping"):
                flatten(extracted, bare, full_id, stage / f"{stem}.tex", prune_macros=prune_macros, asset_map=asset_map)
            shutil.rmtree(extracted)
        with fetching_stage(aid, "corpus QA"):
            errors = check_paper(stage, stem, version=full_id, canonical_id=bare)
            if errors:
                raise ValueError("; ".join(errors))
        with fetching_stage(aid, "publication"):
            if paper_dir.exists():
                raise FileExistsError(f"paper directory already exists: {paper_dir}")
            stage.replace(paper_dir)
    return paper_dir
