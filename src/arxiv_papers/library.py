"""Library storage, matching, and generated indexes (no database)."""
from contextlib import contextmanager, redirect_stdout
import fcntl
import io
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata
from difflib import SequenceMatcher

from .flatten_tex import strip_comments
from .figindex import build_index as figure_index
from .refindex import build_index as reference_index
from .qa_corpus import check_paper

ID_RE = re.compile(r"(?P<id>\d{4}\.\d{4,5}|[a-zA-Z][a-zA-Z0-9.-]*/\d{7})(?P<version>v[1-9]\d*)?\Z")


def parse_id(value):
    match = ID_RE.fullmatch(value)
    if not match:
        raise ValueError(f"invalid arXiv ID: {value}")
    return match['id'], match['version']


def directory_name(aid, version):
    parse_id(aid + version)
    return aid.replace('/', '_') + version


def normalize(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', value.casefold())
                   if not unicodedata.combining(c))


def words(value):
    return re.findall(r'\w+', normalize(value), re.UNICODE)


def matches(value, query, *, phrase=False, fuzzy=False):
    target, wanted = words(value), words(query)
    if not wanted:
        raise ValueError('search queries must contain at least one word')
    if phrase:
        return any(target[i:i + len(wanted)] == wanted for i in range(len(target)))
    def word_match(word):
        return word in target or (fuzzy and len(word) >= 4 and any(
            len(candidate) >= 4 and SequenceMatcher(None, word, candidate).ratio() >= 0.8
            for candidate in target))
    return all(word_match(word) for word in wanted)


def plain_tex(text):
    """Best-effort searchable prose, not a TeX interpreter."""
    text = strip_comments(text)
    text = re.sub(r"\\['`\^\"~=.]\s*\{?([A-Za-z])\}?", r'\1', text)
    text = re.sub(r'\\(?:label|cite\w*|ref|eqref|includegraphics)(?:\[[^\]]*\])?\s*\{[^}]*\}', ' ', text)
    text = re.sub(r'\\[A-Za-z@]+\*?(?:\[[^\]]*\])?', ' ', text)
    text = re.sub(r'\\([^A-Za-z])', r'\1', text)
    return re.sub(r'[{}$]', '', text)


def atomic_write(path, text):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_metadata(directory, metadata):
    atomic_write(directory / 'metadata.json', json.dumps(metadata, ensure_ascii=False, indent=2) + '\n')


def validate_metadata(metadata, directory):
    required = ('arxiv_id', 'version', 'title', 'authors', 'abstract', 'tags',
                'first_submitted', 'version_submitted')
    if not isinstance(metadata, dict) or any(key not in metadata for key in required):
        raise ValueError(f'{directory}: missing required metadata fields')
    if not all(isinstance(metadata[k], str) for k in ('arxiv_id', 'version', 'title', 'abstract')):
        raise ValueError(f'{directory}: invalid metadata strings')
    aid, version = parse_id(metadata['arxiv_id'] + metadata['version'])
    if aid != metadata['arxiv_id'] or version != metadata['version'] or directory.name != directory_name(aid, version):
        raise ValueError(f'{directory}: ID/version does not match directory')
    for key in ('authors', 'tags'):
        if not isinstance(metadata[key], list) or any(not isinstance(x, str) or not x.strip() for x in metadata[key]):
            raise ValueError(f'{directory}: {key} must be a list of nonempty strings')
    from datetime import date
    for key in ('first_submitted', 'version_submitted'):
        value = metadata[key]
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError(f'{directory}: {key} must be YYYY-MM-DD')
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError(f'{directory}: invalid {key}') from None


@contextmanager
def locked_library(root, *, create=False):
    if create:
        root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise ValueError(f'library does not exist: {root}; use add to create it')
    with (root / '.library.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


class Library:
    def __init__(self, root):
        self.root = root

    def papers(self):
        result, seen = [], set()
        for directory in sorted(self.root.iterdir()):
            if directory.name.startswith('.') or not directory.is_dir():
                continue
            if directory.is_symlink():
                raise ValueError(f'symlink paper directories are not supported: {directory}')
            path = directory / 'metadata.json'
            if not path.is_file() or path.is_symlink():
                raise ValueError(f'{directory}: missing or unsafe metadata.json')
            data = json.loads(path.read_text(encoding='utf-8'))
            validate_metadata(data, directory)
            if data['arxiv_id'] in seen:
                raise ValueError(f"multiple versions installed: {data['arxiv_id']}")
            seen.add(data['arxiv_id'])
            result.append((directory, data))
        return result

    def resolve(self, ids):
        papers = {m['arxiv_id']: (d, m) for d, m in self.papers()}
        resolved = []
        for value in ids:
            aid, version = parse_id(value)
            if aid not in papers:
                raise ValueError(f'paper is not installed: {value}')
            directory, metadata = papers[aid]
            if version and metadata['version'] != version:
                raise ValueError(f"{value} is not installed; installed: {aid}{metadata['version']}")
            if not any(d == directory for d, _ in resolved):
                resolved.append((directory, metadata))
        return resolved

    def select(self, args):
        result = []
        for directory, metadata in self.papers():
            tags = {normalize(tag) for tag in metadata['tags']}
            queries = [normalize(t) for t in args.tag]
            if args.untagged and tags:
                continue
            if queries and not (all(q in tags for q in queries) if args.match == 'all' else any(q in tags for q in queries)):
                continue
            if not all(any(matches(a, q, fuzzy=True) if args.fuzzy else normalize(q) in normalize(a)
                           for a in metadata['authors']) for q in args.author):
                continue
            if not all(matches(metadata['title'], q, fuzzy=args.fuzzy) for q in args.title):
                continue
            if not all(matches(metadata['title'], q, phrase=True) for q in args.title_phrase):
                continue
            if not all(matches(metadata['abstract'], q) for q in args.abstract):
                continue
            if not all(matches(metadata['abstract'], q, phrase=True) for q in args.abstract_phrase):
                continue
            if args.content or args.content_phrase:
                tex = directory / (directory.name + '.tex')
                if not tex.is_file():
                    continue
                text = plain_tex(tex.read_text(encoding='utf-8', errors='replace'))
                if not all(matches(text, q) for q in args.content):
                    continue
                if not all(matches(text, q, phrase=True) for q in args.content_phrase):
                    continue
            result.append((directory, metadata))
        return result

    def rebuild(self, *, detailed=False):
        papers = self.papers()
        lines = ['# arXiv paper library', '']
        for directory, metadata in papers:
            title = metadata['title'].replace('\n', ' ').replace('[', r'\[').replace(']', r'\]')
            authors = '; '.join(metadata['authors']).replace('\n', ' ')
            lines.append(f"- [{title}]({directory.name}/) — {metadata['arxiv_id']}{metadata['version']} — {authors}")
            atomic_write(directory / 'abstract.md', '# ' + metadata['title'].replace('\n', ' ') + '\n\n' + metadata['abstract'] + '\n')
            if detailed:
                with redirect_stdout(io.StringIO()):
                    figure_index(directory)
                    reference_index(directory)
        atomic_write(self.root / 'INDEX.md', '\n'.join(lines) + '\n')

    def check(self):
        return [{'id': m['arxiv_id'] + m['version'], 'errors': check_paper(
                    d, d.name, m['arxiv_id'] + m['version'], canonical_id=m['arxiv_id'])}
                for d, m in self.papers()]
