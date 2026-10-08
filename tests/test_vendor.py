import hashlib
from pathlib import Path
import unittest

from arxiv_papers import flatten_tex


class VendorTests(unittest.TestCase):
    def test_pinned_release_checksum_and_license(self):
        vendor = Path(flatten_tex.__file__).with_name('_vendor')
        self.assertEqual(hashlib.sha256((vendor / 'latexpand').read_bytes()).hexdigest(),
                         'b66c5f753f0a006cd85b366835cc2e565152b235b9277e462ef49f5b5a97629f')
        self.assertIn('Redistribution and use', (vendor / 'LICENCE').read_text())
        self.assertIn('4a76237c217a532a77044c0346b1e06648814531', (vendor / 'README.md').read_text())
