"""Optional offline regression: re-flatten originals into temporary directories.

PYTHONPATH=src PAPER_FETCHING_TEST_CORPUS=/path/to/corpus python3 -m unittest discover -s tests -v
The supplied corpus is read-only; tests never regenerate or overwrite its files.
"""
import hashlib
import os
from pathlib import Path
import re
import tempfile
import unittest

from arxiv_papers.assets import copy_assets
from arxiv_papers.fetch_papers import extract_source
from arxiv_papers.flatten_tex import collapse_blank_lines, flatten
from arxiv_papers.prune_macros import analyze, external_macro_names, prune_unused_macros, tokenize

CORPUS = os.environ.get("PAPER_FETCHING_TEST_CORPUS")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@unittest.skipUnless(CORPUS, "set PAPER_FETCHING_TEST_CORPUS for real-paper regression")
class CorpusPruningTests(unittest.TestCase):
    def test_all_papers_from_original_archives(self):
        corpus = Path(CORPUS)
        papers = sorted(corpus.glob("*/*.tar.gz"))
        self.assertGreaterEqual(len(papers), 2)
        original_files = sorted(corpus.rglob("*"))
        before_hashes = {p: digest(p) for p in original_files if p.is_file()}
        total_removed = 0
        with tempfile.TemporaryDirectory(prefix="paper-pruning-tests-") as tmp:
            tmp = Path(tmp)
            for archive in papers:
                paper_id = archive.parent.name
                aid = re.sub(r"v\d+$", "", paper_id)
                with self.subTest(paper=paper_id):
                    if paper_id != aid:
                        version = paper_id
                    else:
                        # Also accept a read-only legacy corpus for regression runs.
                        header = (archive.parent / f"{aid}.tex").read_text().splitlines()[0]
                        version = re.search(r"latest: ([^)]+)", header)[1]
                    work = tmp / paper_id
                    work.mkdir()
                    root = work / "source"
                    extract_source(archive.read_bytes(), root)
                    off = work / "off.tex"
                    safe = work / "safe.tex"
                    asset_map = copy_assets(root, work)
                    flatten(root, aid, version, off, prune_macros="off", asset_map=asset_map)
                    flatten(root, aid, version, safe, asset_map=asset_map)
                    for destination in asset_map.values():
                        self.assertTrue((work / destination).is_file())
                    # Canonical direct image references must point to copied files,
                    # including names shared by several source subdirectories.
                    targets = re.findall(r"\\includegraphics\s*\*?\s*(?:\[[^]]*\]\s*)?\{([^}]+)\}", safe.read_text())
                    for target in targets:
                        if target in asset_map.values():
                            self.assertTrue((work / target).is_file())
                    if aid == "2305.17589":
                        # Regression observed in the real GRIT source: latexpand
                        # incorrectly prepended Sections/ to this Figures/ asset.
                        self.assertIn("figures/Figures/fluorescein_1.pdf", targets)
                        self.assertNotIn("Sections/Figures/fluorescein_1.pdf", targets)
                    original = "\n".join(off.read_text().splitlines()[2:])
                    cleaned = "\n".join(safe.read_text().splitlines()[2:])
                    protected = external_macro_names(root)
                    self.assertEqual(cleaned, collapse_blank_lines(prune_unused_macros(original, protected_names=protected)))
                    self.assertEqual(prune_unused_macros(cleaned, protected_names=protected), cleaned)
                    body = original.index(r"\begin{document}")
                    self.assertEqual(original[body:], cleaned[cleaned.index(r"\begin{document}"):])
                    try:
                        tokens, declarations, live = analyze(original, protected)
                    except ValueError as exc:
                        self.assertEqual(original, cleaned)
                        print(f"{aid}: preserved unchanged ({exc})")
                        continue
                    removed = [d for d in declarations if d.name not in live]
                    # Independent check: no removed name occurs in any retained TeX token.
                    remaining_names = {t.value for t in tokenize(cleaned)}
                    self.assertFalse(remaining_names & {d.name for d in removed})
                    self.assertTrue(all(d.removable for d in removed))
                    self.assertTrue(all(tokens[d.first].start < body for d in removed))
                    total_removed += len(removed)
                    print(f"{aid}: {len(removed)}/{len(declarations)} declarations removed; "
                          f"{len(original) - len(cleaned)} characters saved")
        self.assertGreater(total_removed, 0)
        self.assertEqual(before_hashes, {p: digest(p) for p in original_files if p.is_file()})
        self.assertEqual(original_files, sorted(corpus.rglob("*")))


if __name__ == "__main__":
    unittest.main()
