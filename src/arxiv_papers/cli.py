"""The single public CLI."""
import argparse
from contextlib import redirect_stdout
import json
from pathlib import Path
import shutil
import sys
import tempfile

from . import __version__
from .fetch_papers import fetch_one
from .library import Library, locked_library, normalize, parse_id, validate_metadata, write_metadata


def common(parser, *, top=False):
    parser.add_argument('--library', type=Path, default=Path('./arxiv-papers') if top else argparse.SUPPRESS,
                        help='library directory (default: ./arxiv-papers)')
    parser.add_argument('--json', action='store_true', default=False if top else argparse.SUPPRESS,
                        help='machine-readable JSON output')


OVERVIEW = '''\
QUICK START
  arxiv-papers add 2509.21097 1706.03762 --tag reading
  arxiv-papers list --tag reading
  arxiv-papers metadata 2509.21097 --json
  arxiv-papers --library /path/to/papers list

COMMAND REFERENCE
  list [FILTERS]                     List papers; no filters lists everything.
    --tag TAG                       Exact tag; repeat for OR matching.
    --match all                     Require every --tag (default: any).
    --untagged                      Only papers without tags; excludes --tag.
    --author QUERY                  Substring within an individual author name.
    --title QUERY                   All query words in the title, any order.
    --title-phrase PHRASE            Consecutive title words.
    --abstract QUERY                All query words in the abstract, any order.
    --abstract-phrase PHRASE         Consecutive abstract words.
    --content QUERY                 All query words in TeX-derived prose.
    --content-phrase PHRASE          Consecutive TeX-derived prose words.
    --fuzzy                         Typo tolerance for title/author words only.
  metadata ID [ID ...]               Complete metadata, including abstracts.
  add ID [ID ...] [--tag TAG ...]     Download/process new papers; skip installed.
  update ID [ID ...]                 Replace installed revision; preserve tags.
    --prune-macros {safe,off}        add/update: prune unused TeX macros (safe).
  tag add ID [ID ...] --tag TAG ...  Add tags, preserving existing tags.
  tag remove ID [...] --tag TAG ... Remove selected tags, not papers.
  remove ID [ID ...]                 PERMANENTLY DELETE papers and all assets.
  check                             Validate metadata, PDF, TeX, and sources.
  rebuild-indexes                   Regenerate INDEX.md, abstracts, figure and
                                    reference indexes from local files.

FILTER RULES
  Text ignores case and accents; punctuation/hyphens split words. Tags match
  exactly after normalization. Repeat text flags or combine different fields
  to require ALL conditions. Only --tag uses OR by default. Quote multiword
  queries. --fuzzy is approximate word matching, not semantic search; it does
  not affect tags, phrases, abstracts, or content. Content search is best-effort
  TeX prose, not PDF text, and excludes papers without usable LaTeX source.

FILTER EXAMPLES
  arxiv-papers list --tag GNNs --tag datasets
  arxiv-papers list --tag GNNs --tag datasets --match all
  arxiv-papers list --untagged
  arxiv-papers list --author Miolane --title "synthetic graph"
  arxiv-papers list --title-phrase "synthetic graph generation"
  arxiv-papers list --title "synthetc graph" --fuzzy
  arxiv-papers list --content "distribution shifts" --json

IDS AND VERSIONS
  Modern IDs (2509.21097), exact revisions (2509.21097v2), and old-style IDs
  (hep-th/9901001v3) are accepted. Only one revision per paper is installed.
  add: bare ID downloads latest; explicit revision downloads exactly that one.
       Installed papers are skipped; a different revision requires update.
  update: bare ID selects latest; explicit revision selects exactly that one,
          including older revisions. Paper must already be installed.
  metadata/tag/remove: bare ID resolves to installed revision; an explicit
                       revision must match it or the command fails.
  Updates preserve tags/custom metadata and publish after download validation.

LIBRARY AND OUTPUT
  --library PATH defaults to ./arxiv-papers relative to the working directory.
  add creates the library; other commands require it to exist. --library and
  --json may appear before or after the subcommand. --json emits structured
  stdout; download diagnostics go to stderr. Batch add/update retain successful
  papers if another ID fails. Exit codes: 0 success, 1 failure, 2 syntax error.
  Flat versioned directories hold metadata.json (source of truth), abstract.md,
  PDFs, flattened TeX, original sources, figures, and bibliography assets.
  INDEX.md lists titles, versioned IDs, and authors, not abstracts. You may edit
  metadata.json, then rebuild-indexes; do not edit downloaded/generated assets.
  Flattening always uses bundled latexpand v1.7.2; Perl on PATH is required.

MORE HELP
  arxiv-papers COMMAND --help        Detailed options and examples.
  arxiv-papers tag add --help        Nested command options.
  https://github.com/jrhuebers/arxiv-papers
'''


def parser():
    root = argparse.ArgumentParser(
        prog='arxiv-papers', description='Manage a flat, tagged arXiv paper library without a database.',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=OVERVIEW)
    common(root, top=True)
    root.add_argument('--version', action='version', version=__version__, help='show installed package version and exit')
    sub = root.add_subparsers(dest='command', required=True, title='commands', metavar='COMMAND')
    def command(name, summary, description, examples):
        p = sub.add_parser(name, help=summary, description=description,
                           formatter_class=argparse.RawDescriptionHelpFormatter,
                           epilog='Examples:\n' + examples)
        common(p)
        return p
    listing = command('list', 'list papers with tag, author, title, abstract, or content filters',
        'List installed papers. With no filters, list everything. All text matching ignores case and accents.\n'
        'Different fields and repeated text filters combine with AND. Tags combine with OR unless --match all.\n'
        'Quote multiword queries. Punctuation and hyphens are word boundaries. Tags require explicit --tag flags.',
        '  arxiv-papers list --tag GNNs --tag datasets --match all\n'
        '  arxiv-papers list --author Miolane --title "synthetic graph"\n'
        '  arxiv-papers list --title-phrase "synthetic graph generation"\n'
        '  arxiv-papers list --content "distribution shifts" --json\n'
        '  arxiv-papers list --untagged')
    listing.add_argument('--tag', action='append', default=[], metavar='TAG',
                         help='exact normalized tag; repeat for OR matching (or AND with --match all)')
    listing.add_argument('--match', choices=['any', 'all'], default='any', help='combine --tag filters: any = OR (default), all = AND; does not affect other fields')
    listing.add_argument('--untagged', action='store_true', help='only papers with no tags; cannot combine with --tag')
    filters = {
        'author': 'substring within an individual author name; repeat to require each query',
        'title': 'all query words in title, any order; repeat to require each query',
        'title-phrase': 'consecutive normalized words in title; repeat to require each phrase',
        'abstract': 'all query words in abstract, any order; repeat to require each query',
        'abstract-phrase': 'consecutive normalized words in abstract; repeat to require each phrase',
        'content': 'all query words in best-effort TeX prose; no PDF extraction; repeat for AND',
        'content-phrase': 'consecutive normalized words in TeX prose; excludes PDF-only papers; repeat for AND',
    }
    for field, help_text in filters.items():
        listing.add_argument('--' + field, action='append', default=[], metavar='PHRASE' if field.endswith('phrase') else 'QUERY', help=help_text)
    listing.add_argument('--fuzzy', action='store_true', help='approximate title/author words (at least 4 characters, similarity >= 0.8); not phrases, tags, abstracts, or content')
    descriptions = {
        'metadata': ('show complete metadata, including abstracts',
            'Show metadata.json records for the selected installed papers, including abstracts, authors, tags, and submission dates.\nBare IDs select the installed revision; explicit versions must match exactly.',
            '  arxiv-papers metadata 2509.21097 1706.03762 --json'),
        'add': ('download and process new papers; skip installed papers',
            'Download PDFs and original sources, flatten LaTeX, materialize assets, validate, and build indexes.\nBare IDs select latest; versioned IDs select exactly that revision. Creates the library if missing.\nInstalled papers are skipped without changing tags; a different revision requires update.\nBatch failures return nonzero but retain successful papers. Uses bundled latexpand; requires Perl.',
            '  arxiv-papers add 2509.21097 1706.03762 --tag reading\n  arxiv-papers add hep-th/9901001v3 --prune-macros off'),
        'update': ('replace an installed revision, preserving tags',
            'Replace an installed paper with latest (bare ID) or an exact requested revision (including older versions).\nPreserves tags/custom metadata, refreshes bibliographic fields, and validates before publication.\nPaper must already be installed; use add otherwise. Batch failures retain successful updates.',
            '  arxiv-papers update 2509.21097\n  arxiv-papers update 2509.21097v2'),
        'remove': ('PERMANENTLY delete papers and all their assets',
            'Permanently delete selected paper directories, including PDFs, sources, figures, and metadata.\nNO trash and NO confirmation prompt: use only when deletion is intended.\nAll IDs are validated before deletion. Bare IDs select installed revisions; explicit versions must match.',
            '  arxiv-papers remove 2509.21097 1706.03762'),
    }
    for name, (summary, description, examples) in descriptions.items():
        p = command(name, summary, description, examples)
        p.add_argument('ids', nargs='+', metavar='ID', help='one or more modern or old-style arXiv IDs, optionally ending in vN')
        if name in ['add', 'update']:
            p.add_argument('--prune-macros', choices=['safe', 'off'], default='safe', help='conservative unused-macro pruning (default: safe); original source is always preserved')
        if name == 'add':
            p.add_argument('--tag', action='append', default=[], metavar='TAG', help='initial tag for newly added papers; repeat for multiple tags; ignored for installed papers')
    tag = command('tag', 'add or remove tags on installed papers',
        'Manage tags without moving papers. Tags match case/accent-insensitively and retain display spelling.\nBare IDs select installed revisions; explicit versions must match. All IDs are validated before changes.',
        '  arxiv-papers tag add 2509.21097 1706.03762 --tag GNNs --tag reading\n'
        '  arxiv-papers tag remove 2509.21097 --tag reading')
    tag_sub = tag.add_subparsers(dest='operation', required=True, title='tag operations', metavar='OPERATION')
    for operation in ['add', 'remove']:
        p = tag_sub.add_parser(operation, help=operation + ' selected tags on installed papers',
            description=('Add tags without duplicates, preserving existing tags and their spelling.' if operation == 'add'
                         else 'Remove matching tags; absent tags are ignored. Does not delete papers.') +
                        '\nBare IDs select installed revisions; explicit versions must match. Tags ignore case and accents.',
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog=f'Example:\n  arxiv-papers tag {operation} 2509.21097 1706.03762 --tag reading --tag GNNs')
        common(p)
        p.add_argument('ids', nargs='+', metavar='ID', help='one or more installed arXiv IDs, optionally ending in exact installed vN')
        p.add_argument('--tag', action='append', required=True, metavar='TAG', help='tag to ' + operation + '; repeat for multiple tags (required)')
    command('check', 'validate all installed papers and report failures',
        'Validate metadata schema, unique installed versions, PDFs, flattened TeX, version headers, and original sources.\nPDF-only papers are checked against their source-unavailable marker. Does not download or repair files.\nReturns nonzero if validation fails.', '  arxiv-papers check --json')
    command('rebuild-indexes', 'regenerate library and paper-local Markdown indexes',
        'Regenerate INDEX.md, abstract.md, FIGURES.md, and REFERENCES.md from local metadata and sources.\nRun after editing metadata.json. Does not download papers or change tags/PDFs/TeX.\nINDEX.md lists titles, versioned IDs, and authors; each abstract lives in its paper directory.',
        '  arxiv-papers rebuild-indexes --library /path/to/papers')
    return root


def merged_tags(existing, additions):
    result = list(existing)
    known = {normalize(t) for t in result}
    for tag in additions:
        if not tag.strip():
            raise ValueError('tags cannot be empty')
        tag = tag.strip()
        if normalize(tag) not in known:
            result.append(tag)
            known.add(normalize(tag))
    return result


def emit(args, data):
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    elif args.command == 'metadata':
        for paper in data:
            print(json.dumps(paper, ensure_ascii=False, indent=2))
    elif args.command == 'list':
        for paper in data:
            print(f"{paper['arxiv_id']}{paper['version']} — {paper['title']} — {'; '.join(paper['authors'])}" +
                  (f" [{', '.join(paper['tags'])}]" if paper['tags'] else ''))
    else:
        for item in data:
            print(item['id'] + ': ' + (item.get('status') or ('; '.join(item['errors']) if item['errors'] else 'OK')))


def run(args):
    library = Library(args.library)
    with locked_library(args.library, create=args.command == 'add'):
        if args.command == 'list':
            if args.untagged and args.tag:
                raise ValueError('--untagged cannot be combined with tag filters')
            return [m for _, m in library.select(args)], 0
        if args.command == 'metadata':
            return [m for _, m in library.resolve(args.ids)], 0
        if args.command == 'check':
            results = library.check()
            return results, int(any(r['errors'] for r in results))
        if args.command == 'rebuild-indexes':
            library.rebuild(detailed=True)
            return [{'id': str(args.library), 'status': 'indexes rebuilt'}], 0
        if args.command == 'remove':
            papers = library.resolve(args.ids)  # validate all before deleting anything
            for directory, _ in papers:
                shutil.rmtree(directory)
            library.rebuild()
            return [{'id': m['arxiv_id'] + m['version'], 'status': 'deleted'} for _, m in papers], 0
        if args.command == 'tag':
            tags = merged_tags([], args.tag)
            papers = library.resolve(args.ids)
            for directory, metadata in papers:
                if args.operation == 'add':
                    metadata['tags'] = merged_tags(metadata['tags'], tags)
                else:
                    remove = {normalize(t) for t in tags}
                    metadata['tags'] = [t for t in metadata['tags'] if normalize(t) not in remove]
                write_metadata(directory, metadata)
            library.rebuild()
            return [{'id': m['arxiv_id'] + m['version'], 'status': 'tags updated', 'tags': m['tags']} for _, m in papers], 0
        # Add/update may succeed for some IDs and fail for others. Report each explicitly.
        added_tags = merged_tags([], getattr(args, 'tag', []))
        results, failed = [], False
        for value in args.ids:
            try:
                aid, version = parse_id(value)
                existing = next(((d, m) for d, m in library.papers() if m['arxiv_id'] == aid), None)
                if args.command == 'add' and existing:
                    if version and version != existing[1]['version']:
                        raise ValueError(f"installed {aid}{existing[1]['version']}; use update to replace it")
                    results.append({'id': value, 'status': 'already installed', 'installed_version': existing[1]['version']})
                    continue
                if args.command == 'update' and not existing:
                    raise ValueError('paper is not installed; use add')
                with tempfile.TemporaryDirectory(prefix='.fetch-', dir=args.library) as temporary:
                    with redirect_stdout(sys.stderr):
                        staged = fetch_one(Path(temporary), value, prune_macros=args.prune_macros)
                    data = json.loads((staged / 'metadata.json').read_text(encoding='utf-8'))
                    data['tags'] = list(existing[1]['tags']) if existing else added_tags
                    # Preserve locally edited/custom metadata; refresh fetched bibliographic fields.
                    if existing:
                        data = {**existing[1], **data}
                    validate_metadata(data, staged)
                    write_metadata(staged, data)
                    # Build paper-local indexes before replacing an installed version.
                    with redirect_stdout(sys.stderr):
                        from .figindex import build_index as figure_index
                        from .refindex import build_index as reference_index
                        figure_index(staged)
                        reference_index(staged)
                    destination = args.library / staged.name
                    backup = Path(temporary) / '.previous'
                    if existing:
                        existing[0].rename(backup)
                    try:
                        if destination.exists():
                            raise ValueError(f'destination already exists: {destination}')
                        staged.rename(destination)
                    except Exception:
                        if existing:
                            backup.rename(existing[0])
                        raise
                results.append({'id': data['arxiv_id'] + data['version'], 'status': 'updated' if existing else 'added'})
            except Exception as exc:
                failed = True
                results.append({'id': value, 'status': 'error', 'error': str(exc)})
                print(f'{value}: {exc}', file=sys.stderr)
        try:
            library.rebuild()
        except Exception as exc:
            failed = True
            results.append({'id': str(args.library), 'status': 'error', 'error': f'index rebuild failed: {exc}'})
            print(f'index rebuild failed: {exc}', file=sys.stderr)
        return results, int(failed)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        results, code = run(args)
        emit(args, results)
        return code
    except Exception as exc:
        print(f'arxiv-papers: {exc}', file=sys.stderr)
        if args.json:
            print(json.dumps({'error': str(exc)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
