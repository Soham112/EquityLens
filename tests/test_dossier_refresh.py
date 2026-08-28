"""Dossier data refresh must never eat the research.

Guards the fix for: build_dossier skips names that already have a file, so a
name reappearing on a later shortlist kept its ORIGINAL data forever (on
2026-08-23 MAN still showed its 08-04 price of $54.80 against a live $61.45).
The obvious fix — force=True — is destructive, resetting research to PENDING.

Only the pure splice logic is tested here; refresh_dossier_data itself pulls
live prices, so exercising it would hit the network and depend on data/.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.dossier import PENDING_MARKER, RESEARCH_MARKER, _split_dossier

DATA_HALF = (
    "# Halozyme (HALO) — Discovery Dossier\n"
    "_Generated 2026-08-23 · RS pct 97_\n\n"
    "## Snapshot\n- **Price:** $108.17\n\n"
)
RESEARCH_HALF = (
    f"{RESEARCH_MARKER} — filled Sunday via web research. -->\n\n"
    "## What they do\nRoyalty licensing of ENHANZE.\n\n"
    "## Verdict\nADMIT to growth_universe — 48% revenue growth.\n"
)


class SplitDossier(unittest.TestCase):

    def test_splits_on_the_marker(self):
        data, research = _split_dossier(DATA_HALF + RESEARCH_HALF)
        self.assertEqual(data, DATA_HALF)
        self.assertEqual(research, RESEARCH_HALF)

    def test_halves_rejoin_losslessly(self):
        """The refresh grafts fresh_data + old_research; that must round-trip."""
        original = DATA_HALF + RESEARCH_HALF
        data, research = _split_dossier(original)
        self.assertEqual(data + research, original)

    def test_research_survives_a_data_swap(self):
        """The actual guarantee: new numbers, byte-identical research."""
        fresh_data = DATA_HALF.replace("$108.17", "$141.11").replace("08-23", "08-27")
        _, old_research = _split_dossier(DATA_HALF + RESEARCH_HALF)
        spliced = fresh_data + old_research
        _, new_research = _split_dossier(spliced)
        self.assertEqual(new_research, RESEARCH_HALF)
        self.assertIn("$141.11", spliced)
        self.assertNotIn("$108.17", spliced)
        self.assertIn("ADMIT to growth_universe", spliced)
        self.assertNotIn(PENDING_MARKER, spliced)

    def test_missing_marker_yields_none_so_caller_can_refuse(self):
        """A file with no seam must be detectable, not silently split — that is
        what lets refresh_dossier_data decline instead of overwriting."""
        data, research = _split_dossier("# hand-written notes\nirreplaceable\n")
        self.assertIsNone(research)
        self.assertEqual(data, "# hand-written notes\nirreplaceable\n")

    def test_marker_constant_matches_the_template(self):
        """If the template's comment is reworded without updating the constant,
        every refresh would silently start refusing."""
        self.assertTrue(RESEARCH_HALF.startswith(RESEARCH_MARKER))


if __name__ == "__main__":
    unittest.main()
