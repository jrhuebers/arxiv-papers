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


def parser():
    root = argparse.ArgumentParser(prog='arxiv-papers', description='Manage a flat, tagged arXiv paper library.')
    common(root, top=True)
    root.add_argument('--version', action='version', version=__version__)
    sub = root.add_subparsers(dest='command', required=True)
    listing = sub.add_parser('list', help='list papers with optional filters')
    common(listing)
    listing.add_argument('--tag', action='append', default=[])
    listing.add_argument('--match', choices=['any', 'all'], default='any', help='combine tag filters')
    listing.add_argument('--untagged', action='store_true')
    for field in ['author', 'title', 'title-phrase', 'abstract', 'abstract-phrase', 'content', 'content-phrase']:
        listing.add_argument('--' + field, action='append', default=[])
    listing.add_argument('--fuzzy', action='store_true', help='approximate title words and author words only')
    for command in ['metadata', 'add', 'update', 'remove']:
        p = sub.add_parser(command)
        common(p)
        p.add_argument('ids', nargs='+')
        if command in ['add', 'update']:
            p.add_argument('--prune-macros', choices=['safe', 'off'], default='safe')
        if command == 'add':
            p.add_argument('--tag', action='append', default=[])
    tag = sub.add_parser('tag', help='add or remove tags')
    common(tag)
    tag_sub = tag.add_subparsers(dest='operation', required=True)
    for operation in ['add', 'remove']:
        p = tag_sub.add_parser(operation)
        common(p)
        p.add_argument('ids', nargs='+')
        p.add_argument('--tag', action='append', required=True)
    for command in ['check', 'rebuild-indexes']:
        p = sub.add_parser(command)
        common(p)
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
