# arxiv-papers

A single CLI for a filesystem-backed arXiv paper library, for humans and agents. Papers live in flat, versioned directories; tags provide subdivisions. There is no database or server.

## Install

Python 3.10+ on Linux/macOS is required. Install [`latexpand`](https://ctan.org/pkg/latexpand) (usually through TeX Live) and ensure it is on `PATH`; it is used to flatten author LaTeX sources. The Python package has no third-party runtime dependencies.

```bash
pip install git+https://github.com/jrhuebers/arxiv-papers.git
# Or from a clone:
pip install -e .
arxiv-papers --help
```

The library defaults to `./arxiv-papers/`, relative to your current working directory. Override it with `--library PATH` before or after the subcommand. `add` creates the library; other commands fail if it does not exist. Run the installed CLI from your research project's directory, not necessarily from the source repository.

## Commands

```bash
arxiv-papers add 2509.21097 1706.03762 --tag reading
arxiv-papers add hep-th/9901001v3
arxiv-papers list
arxiv-papers list GNNs datasets                  # either tag
arxiv-papers list --tag GNNs --tag datasets --match all
arxiv-papers list --untagged
arxiv-papers list --author Miolane --title "synthetic graph"
arxiv-papers list --title-phrase "synthetic graph generation"
arxiv-papers list --title "synthetc graph" --fuzzy
arxiv-papers list --abstract "distribution shifts"
arxiv-papers list --content "inductive generalization"
arxiv-papers metadata 2509.21097 1706.03762 --json
arxiv-papers tag add 2509.21097 1706.03762 --tag GNNs --tag plan-6
arxiv-papers tag remove 2509.21097 --tag reading
arxiv-papers update 2509.21097                    # latest revision
arxiv-papers update 2509.21097v2                  # exact revision
arxiv-papers check
arxiv-papers rebuild-indexes
arxiv-papers remove 2509.21097                    # PERMANENT deletion
```

All commands support `--json`. Download diagnostics go to stderr; stdout remains machine-readable. Batch add/update reports each result and exits nonzero if any paper fails; successful papers remain installed. Other mutation commands validate every supplied ID before making changes. Exit status is zero on success, one on operational failure, and two on invalid CLI syntax.

### Matching

Text matching ignores case and Unicode accents. Title, abstract, and content queries require every word, in any order; punctuation and hyphens are word boundaries. `--title-phrase`, `--abstract-phrase`, and `--content-phrase` require consecutive normalized words. Repeated text filters and different fields combine with AND. Author queries are substrings within individual author names. Tags match exactly after case/accent normalization, preserve their first display spelling, and combine with OR unless `--match all` is given. `--untagged` cannot be combined with tag filters.

`--fuzzy` applies only to ordinary title and author filters, not phrases, tags, abstracts, or content. It uses approximate word similarity (minimum four characters, similarity at least 0.8); this is typo tolerance, not semantic search. There is no relevance ranking.

Content search scans a best-effort plain-text representation of flattened TeX, removing comments and common commands while retaining prose arguments. It is not a TeX interpreter: macros/math can limit matching, and preamble text can match. Papers without usable LaTeX source remain searchable through metadata but are excluded from content searches. No PDF text extraction or embeddings are used.

### Versions

One revision per canonical paper ID is allowed. Bare IDs resolve to the installed revision in metadata, tag, and remove commands. An explicit revision must match the installed revision, otherwise the command fails.

`add` downloads the latest revision, or exactly the requested revision. If already installed, it skips without updating or changing tags; an explicit different revision instructs you to use `update`. `update` requires an installed paper, preserves its tags/custom metadata fields, refreshes bibliographic fields, and replaces it only after the new download passes validation. Updates can select an older revision. Downloads are pinned to the revision resolved from metadata.

Old-style IDs remain canonical IDs; they do not map to modern IDs. Directory/file names replace their slash with an underscore: `hep-th/9901001v3` becomes `hep-th_9901001v3/`.

## Library layout

```text
arxiv-papers/
  .library.lock
  INDEX.md
  2509.21097v2/
    metadata.json
    abstract.md
    2509.21097v2.pdf
    2509.21097v2.tex
    2509.21097v2.tar.gz
    FIGURES.md
    REFERENCES.md
    figures/
    bibliography/
```

Original sources may instead be `.source.tex` or `.tex.gz`. If LaTeX source is unavailable, `source-unavailable.txt` explains the PDF-only paper. Image and bibliography paths retain their original source-relative structure. Macro pruning is conservative and enabled by default; pass `--prune-macros off` to add/update to disable it. Original sources are preserved regardless.

`INDEX.md` lists titles, versioned IDs, and author lists, with directory links. Abstracts are in individual `abstract.md` files instead.

Example `metadata.json` (the source of truth):

```json
{
  "arxiv_id": "2509.21097",
  "version": "v2",
  "title": "GraphUniverse: Synthetic Graph Generation for Evaluating Inductive Generalization",
  "authors": ["Louis Van Langendonck", "Guillermo Bernárdez", "Nina Miolane", "Pere Barlet-Ros"],
  "abstract": "The complete abstract is stored here.",
  "tags": ["GNNs", "datasets", "plan-6", "report-8"],
  "first_submitted": "2025-09-25",
  "version_submitted": "2026-02-28"
}
```

You may edit metadata directly, then run `rebuild-indexes` to refresh derived Markdown. Do not change the ID/version independently of directory names. Do not manually edit generated abstracts/indexes or downloaded assets. CLI operations serialize through a library-local advisory lock; external editors do not participate in that lock. Replacement rolls back on ordinary publication errors, but is not a full crash-recovery transaction: keep backups of important libraries. There is no migration command for older paper-fetching corpora.

## Development

```bash
pip install -e .
python3 -m unittest discover -s tests -v
```

Tests run offline. Optional corpus regression tests can use `PAPER_FETCHING_TEST_CORPUS=/path/to/papers`; regenerated files are written only to temporary directories. CI installs `latexpand` for flattening tests.

The public entry point is `arxiv-papers`; processing modules are internal implementation details adapted from the author's paper-fetching skill. There is no Redis dependency and no separate public script workflow.
