"""Offline CLI/library contracts; all fixtures live in temporary directories."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from arxiv_papers import cli
from arxiv_papers.library import Library


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'library'
        self.root.mkdir()
        fetch_patch = patch.object(cli, 'fetch_one', side_effect=AssertionError('unexpected fetch'))
        self.fetch = fetch_patch.start()
        self.addCleanup(fetch_patch.stop)
        self.first = self.paper('1706.03762', title='Café neural attention networks',
                                authors=['José García', 'Ada Lovelace'], tags=['Vision', 'Café'])
        self.second = self.paper('2305.17589', title='Attention for neural systems',
                                 authors=['Grace Hopper'], tags=['vision'])
        self.third = self.paper('2401.00001', title='Unrelated topic', tags=[])

    def paper(self, aid, version='v1', title='A title', authors=None, tags=None, root=None):
        directory = (root or self.root) / (aid.replace('/', '_') + version)
        directory.mkdir()
        data = dict(arxiv_id=aid, version=version, title=title,
                    authors=authors or ['A Author'], abstract='Efficient learning for café models.',
                    tags=tags or [], first_submitted='2017-06-01', version_submitted='2017-06-02')
        (directory / 'metadata.json').write_text(json.dumps(data), encoding='utf-8')
        return directory

    def invoke(self, *args, expected=0):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(['--library', str(self.root), '--json', *args])
        self.assertEqual(code, expected, err.getvalue())
        return json.loads(out.getvalue()), err.getvalue()

    def ids(self, *args):
        data, _ = self.invoke('list', *args)
        return [m['arxiv_id'] for m in data]

    def metadata(self, directory):
        return json.loads((directory / 'metadata.json').read_text())

    def snapshot(self, directory):
        return {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob('*') if p.is_file()}

    def fake_fetch(self, root, value, *, prune_macros):
        aid, version = cli.parse_id(value)
        return self.paper(aid, version or 'v2', title='Fresh title', root=root)

    def test_list_all_and_missing_library(self):
        self.assertEqual(self.ids(), ['1706.03762', '2305.17589', '2401.00001'])
        self.root = self.root / 'missing'
        data, _ = self.invoke('list', expected=1)
        self.assertIn('does not exist', data['error'])
        self.assertFalse(self.root.exists())

    def test_tag_or_and_case_and_accents(self):
        self.assertEqual(self.ids('CAFE', 'VISION'), ['1706.03762', '2305.17589'])
        self.assertEqual(self.ids('--tag', 'cafe', '--tag', 'ViSiOn', '--match', 'all'), ['1706.03762'])
        self.assertEqual(self.ids('absent'), [])
        self.assertEqual(self.ids('cafe', 'absent', '--match', 'all'), [])

    def test_untagged_and_conflicting_filters(self):
        self.assertEqual(self.ids('--untagged'), ['2401.00001'])
        for arguments in [('vision',), ('--tag', 'vision')]:
            with self.subTest(arguments=arguments):
                data, _ = self.invoke('list', '--untagged', *arguments, expected=1)
                self.assertIn('cannot be combined', data['error'])

    def test_title_words_not_substrings_and_order_independent(self):
        self.assertEqual(self.ids('--title', 'NETWORKS cafe'), ['1706.03762'])
        self.assertEqual(self.ids('--title', 'net'), [])
        self.assertEqual(self.ids('--title', 'neural', '--title', 'systems'), ['2305.17589'])

    def test_title_phrase_requires_contiguous_words(self):
        self.assertEqual(self.ids('--title-phrase', 'CAFE neural'), ['1706.03762'])
        self.assertEqual(self.ids('--title-phrase', 'neural networks'), [])
        self.assertEqual(self.ids('--title-phrase', 'attention neural'), [])

    def test_fuzzy_is_opt_in_for_title_and_author(self):
        self.assertEqual(self.ids('--title', 'netwroks'), [])
        self.assertEqual(self.ids('--title', 'netwroks', '--fuzzy'), ['1706.03762'])
        self.assertEqual(self.ids('--author', 'Garciaa'), [])
        self.assertEqual(self.ids('--author', 'Garciaa', '--fuzzy'), ['1706.03762'])

    def test_author_substring_case_accents_and_repeated_filters(self):
        self.assertEqual(self.ids('--author', 'OSE GARC'), ['1706.03762'])
        self.assertEqual(self.ids('--author', 'garcia', '--author', 'lovelace'), ['1706.03762'])
        self.assertEqual(self.ids('--author', 'garcia', '--author', 'hopper'), [])

    def test_abstract_words_phrases_and_no_fuzzy(self):
        self.assertEqual(len(self.ids('--abstract', 'cafe efficient')), 3)
        self.assertEqual(len(self.ids('--abstract-phrase', 'learning for cafe')), 3)
        self.assertEqual(self.ids('--abstract-phrase', 'efficient cafe'), [])
        self.assertEqual(self.ids('--abstract', 'efficent', '--fuzzy'), [])

    def test_fulltex_is_canonical_only_and_removes_comments_commands(self):
        (self.first / (self.first.name + '.tex')).write_text(
            r'\section{Résumé} Deep \textbf{learning} works.' + '\n% secretword\n' +
            r'\cite{citationword} \label{labelword}')
        (self.second / (self.second.name + '.source.tex')).write_text('Deep learning secretword')
        self.assertEqual(self.ids('--content', 'resume learning'), ['1706.03762'])
        self.assertEqual(self.ids('--content-phrase', 'deep learning'), ['1706.03762'])
        for query in ('secretword', 'citationword', 'labelword', 'section'):
            self.assertEqual(self.ids('--content', query), [])
        self.assertEqual(self.ids('--content', 'learnign', '--fuzzy'), [])

    def test_empty_word_queries_are_errors(self):
        self.invoke('list', '--title', '!!!', expected=1)

    def test_metadata_bare_exact_and_duplicate_ids(self):
        data, _ = self.invoke('metadata', '1706.03762', '1706.03762v1')
        self.assertEqual(data, [self.metadata(self.first)])
        self.invoke('metadata', '1706.03762v2', expected=1)
        self.invoke('metadata', '9999.99999', expected=1)

    def test_old_style_identifier(self):
        old = self.paper('hep-th/9901001', version='v12')
        data, _ = self.invoke('metadata', 'hep-th/9901001v12')
        self.assertEqual(data, [self.metadata(old)])

    def test_invalid_metadata_schema_and_directory_mismatch(self):
        original = self.metadata(self.first)
        for changes in ({'version': 'v2'}, {'tags': 'vision'}, {'authors': [42]},
                        {'first_submitted': 'not-a-date'}, {'title': None}):
            with self.subTest(changes=changes):
                (self.first / 'metadata.json').write_text(json.dumps({**original, **changes}))
                self.invoke('list', expected=1)
        original.pop('abstract')
        (self.first / 'metadata.json').write_text(json.dumps(original))
        self.invoke('list', expected=1)

    def test_duplicate_installed_versions_are_rejected(self):
        self.paper('1706.03762', version='v2')
        data, _ = self.invoke('list', expected=1)
        self.assertIn('multiple versions', data['error'])

    def test_tag_add_deduplicates_normalized_names_and_remove(self):
        self.invoke('tag', 'add', '1706.03762', '--tag', 'CAFE', '--tag', ' New ')
        self.assertEqual(self.metadata(self.first)['tags'], ['Vision', 'Café', 'New'])
        self.invoke('tag', 'remove', '1706.03762v1', '--tag', 'VISION', '--tag', 'cafe')
        self.assertEqual(self.metadata(self.first)['tags'], ['New'])
        self.assertTrue((self.root / 'INDEX.md').is_file())

    def test_tag_invalid_batch_does_not_mutate(self):
        before = self.snapshot(self.first)
        self.invoke('tag', 'add', '1706.03762', '9999.99999', '--tag', 'new', expected=1)
        self.assertEqual(before, self.snapshot(self.first))
        self.invoke('tag', 'add', '1706.03762', '--tag', ' ', expected=1)
        self.assertEqual(before, self.snapshot(self.first))

    def test_remove_validates_entire_batch_before_deletion(self):
        for invalid in ('9999.99999', '1706.03762v2', '../outside'):
            self.invoke('remove', '1706.03762', invalid, expected=1)
            self.assertTrue(self.first.is_dir())
        result, _ = self.invoke('remove', '1706.03762', '1706.03762v1')
        self.assertEqual(len(result), 1)
        self.assertFalse(self.first.exists())
        self.assertTrue(self.second.exists())
        self.assertNotIn('1706.03762', (self.root / 'INDEX.md').read_text())

    def test_remove_rejects_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside)
            (target / 'keep').write_text('safe')
            (self.root / '9999.99999v1').symlink_to(target, target_is_directory=True)
            self.invoke('remove', '1706.03762', expected=1)
            self.assertTrue(self.first.exists())
            self.assertEqual((target / 'keep').read_text(), 'safe')

    def test_add_mocked_fetch_tags_and_options(self):
        self.fetch.side_effect = self.fake_fetch
        result, _ = self.invoke('add', '2501.00001v3', '--tag', ' New ', '--tag', 'new', '--prune-macros', 'off')
        self.assertEqual(result, [{'id': '2501.00001v3', 'status': 'added'}])
        self.assertEqual(self.metadata(self.root / '2501.00001v3')['tags'], ['New'])
        self.assertEqual(self.fetch.call_args.kwargs, {'prune_macros': 'off'})
        self.assertFalse(any(p.name.startswith('.fetch-') for p in self.root.iterdir()))

    def test_add_creates_library(self):
        self.root = self.root / 'new-library'
        self.fetch.side_effect = self.fake_fetch
        self.invoke('add', '2501.00001')
        self.assertTrue((self.root / '2501.00001v2/metadata.json').is_file())

    def test_add_already_installed_and_version_conflict_do_not_fetch(self):
        result, _ = self.invoke('add', '1706.03762')
        self.assertEqual(result[0]['status'], 'already installed')
        self.invoke('add', '1706.03762v2', expected=1)
        self.fetch.assert_not_called()

    def test_add_mixed_success_and_fetch_failure(self):
        def fetch(root, value, **kwargs):
            if value == '2501.00002':
                raise RuntimeError('mock download failed')
            return self.fake_fetch(root, value, **kwargs)
        self.fetch.side_effect = fetch
        result, err = self.invoke('add', '2501.00001', '2501.00002', expected=1)
        self.assertEqual([r['status'] for r in result], ['added', 'error'])
        self.assertIn('mock download failed', err)
        self.assertTrue((self.root / '2501.00001v2').exists())

    def test_malformed_id_batch_still_processes_valid_ids(self):
        self.fetch.side_effect = self.fake_fetch
        result, _ = self.invoke('add', '../evil', '2501.00003', 'bad-id', '2501.00004', expected=1)
        self.assertEqual([r['status'] for r in result], ['error', 'added', 'error', 'added'])
        self.assertEqual([r['id'] for r in result],
                         ['../evil', '2501.00003v2', 'bad-id', '2501.00004v2'])
        self.assertEqual([call.args[1] for call in self.fetch.call_args_list],
                         ['2501.00003', '2501.00004'])
        for aid in ('2501.00003', '2501.00004'):
            self.assertTrue((self.root / (aid + 'v2') / 'metadata.json').is_file())

    def test_update_rejects_downloaded_invalid_metadata_before_publication(self):
        before = self.snapshot(self.first)
        for changes in ({'title': None}, {'authors': [42]}, {'version': 'v3'},
                        {'first_submitted': None}, {'version_submitted': '2024-02-30'}):
            with self.subTest(changes=changes):
                def invalid_fetch(root, value, **kwargs):
                    staged = self.fake_fetch(root, value, **kwargs)
                    data = self.metadata(staged)
                    data.update(changes)
                    (staged / 'metadata.json').write_text(json.dumps(data))
                    return staged
                self.fetch.side_effect = invalid_fetch
                with patch.object(Path, 'rename', autospec=True) as rename:
                    result, _ = self.invoke('update', '1706.03762', expected=1)
                    rename.assert_not_called()
                self.assertEqual(result[0]['status'], 'error')
                self.assertTrue(result[0]['error'])
                after = self.snapshot(self.first)
                self.assertEqual(before, {key: after[key] for key in before})
                self.assertFalse((self.root / '1706.03762v2').exists())
                self.assertFalse(any(p.name.startswith('.fetch-') for p in self.root.iterdir()))

    def test_rebuild_failure_keeps_per_id_results_and_separate_index_error(self):
        for command, ids, statuses in (
                ('add', ('2501.00005', 'bad-id'), ['added', 'error']),
                ('update', ('1706.03762', 'bad-id'), ['updated', 'error'])):
            with self.subTest(command=command):
                self.fetch.side_effect = self.fake_fetch
                with patch.object(Library, 'rebuild', side_effect=OSError('mock index failure')) as rebuild:
                    result, err = self.invoke(command, *ids, expected=1)
                rebuild.assert_called_once()
                self.assertIsInstance(result, list)
                self.assertEqual([r['status'] for r in result], statuses + ['error'])
                self.assertEqual(result[0]['id'], ids[0] + 'v2')
                self.assertEqual(result[1]['id'], 'bad-id')
                self.assertEqual(result[2]['id'], str(self.root))
                self.assertIn('index rebuild failed: mock index failure', result[2]['error'])
                self.assertIn('index rebuild failed', err)
                self.assertTrue((self.root / (ids[0] + 'v2') / 'metadata.json').is_file())

    def test_metadata_dates_reject_null_noncanonical_and_impossible_values(self):
        original = self.metadata(self.first)
        for field in ('first_submitted', 'version_submitted'):
            for value in (None, 20240101, '', '20240101', '2024-1-01', '2024-02-30'):
                with self.subTest(field=field, value=value):
                    (self.first / 'metadata.json').write_text(json.dumps({**original, field: value}))
                    result, _ = self.invoke('metadata', '1706.03762', expected=1)
                    self.assertIn(field, result['error'])
        (self.first / 'metadata.json').write_text(json.dumps({**original,
                                                            'first_submitted': '2024-02-29'}))
        self.invoke('metadata', '1706.03762')

    def test_update_preserves_tags_custom_metadata_and_replaces_version(self):
        data = self.metadata(self.first)
        data['notes'] = 'local notes'
        (self.first / 'metadata.json').write_text(json.dumps(data))
        self.fetch.side_effect = self.fake_fetch
        result, _ = self.invoke('update', '1706.03762')
        self.assertEqual(result[0]['status'], 'updated')
        self.assertFalse(self.first.exists())
        updated = self.metadata(self.root / '1706.03762v2')
        self.assertEqual(updated['tags'], data['tags'])
        self.assertEqual(updated['notes'], 'local notes')
        self.assertEqual(updated['title'], 'Fresh title')

    def test_update_uninstalled_and_fetch_failure_preserve_original(self):
        self.invoke('update', '2501.00001', expected=1)
        self.fetch.assert_not_called()
        before = self.snapshot(self.first)
        self.fetch.side_effect = RuntimeError('offline')
        self.invoke('update', '1706.03762', expected=1)
        self.assertEqual(before, self.snapshot(self.first))

    def test_update_install_rename_failure_rolls_back(self):
        before = self.snapshot(self.first)
        self.fetch.side_effect = self.fake_fetch
        rename = Path.rename
        def fail_install(path, destination):
            if path.name == '1706.03762v2' and path.parent.name.startswith('.fetch-'):
                raise OSError('mock publication failure')
            return rename(path, destination)
        with patch.object(Path, 'rename', fail_install):
            self.invoke('update', '1706.03762', expected=1)
        # Rebuild legitimately regenerates abstract.md; original files remain byte-identical.
        after = self.snapshot(self.first)
        self.assertEqual(before, {key: after[key] for key in before})
        self.assertFalse((self.root / '1706.03762v2').exists())
        self.assertFalse(any(p.name.startswith('.fetch-') for p in self.root.iterdir()))

    def test_json_options_after_subcommands_and_human_output(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(['list', '--library', str(self.root), '--json'])
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out.getvalue())), 3)
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(['--library', str(self.root), 'list', '--untagged'])
        self.assertEqual(code, 0)
        self.assertIn('2401.00001v1 — Unrelated topic', out.getvalue())

    def test_check_status_and_rebuild_indexes(self):
        with patch.object(Library, 'check', return_value=[{'id': '1706.03762v1', 'errors': ['missing PDF']}]):
            result, _ = self.invoke('check', expected=1)
        self.assertEqual(result[0]['errors'], ['missing PDF'])
        self.invoke('rebuild-indexes')
        self.assertIn('Café neural attention networks', (self.root / 'INDEX.md').read_text())
        self.assertTrue((self.first / 'abstract.md').is_file())


if __name__ == '__main__':
    unittest.main()
