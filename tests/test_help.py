"""Help is the self-contained interface documentation for humans and agents."""
from contextlib import redirect_stdout
import io
import unittest

from arxiv_papers.cli import main, parser


class HelpTests(unittest.TestCase):
    def help_text(self, *command):
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as caught:
            main([*command, '--help'])
        self.assertEqual(caught.exception.code, 0)
        return output.getvalue()

    def test_top_level_covers_workflow_and_semantics(self):
        text = self.help_text()
        for section in ['QUICK START', 'COMMAND REFERENCE', 'FILTER RULES',
                        'FILTER EXAMPLES', 'IDS AND VERSIONS', 'LIBRARY AND OUTPUT', 'MORE HELP']:
            self.assertIn(section, text)
        for detail in ['--tag', '--match all', '--untagged', '--author', '--title-phrase',
                       '--abstract-phrase', '--content-phrase', '--fuzzy', '--prune-macros',
                       'tag add', 'tag remove', 'PERMANENTLY DELETE', 'hep-th/9901001v3',
                       'Exit codes', 'bundled latexpand', 'Perl', 'source of truth']:
            self.assertIn(detail, text)

    def test_every_subcommand_has_examples_and_explanations(self):
        for command in [('list',), ('metadata',), ('add',), ('update',), ('remove',),
                        ('tag',), ('tag', 'add'), ('tag', 'remove'), ('check',), ('rebuild-indexes',)]:
            with self.subTest(command=command):
                text = self.help_text(*command)
                self.assertIn('Example', text)
                self.assertIn('--library', text)
                self.assertIn('--json', text)
                self.assertIn('arxiv-papers', text)

    def test_every_option_has_help(self):
        def walk(p):
            for action in p._actions:
                if action.option_strings:
                    self.assertTrue(action.help, action.option_strings)
                if hasattr(action, 'choices') and isinstance(action.choices, dict):
                    for child in action.choices.values():
                        walk(child)
        walk(parser())
