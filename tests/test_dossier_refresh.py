"""Dossier refresh must never eat the research — and must fail safe.

Guards the fix for: build_dossier skips names that already have a file, so a
name reappearing on a later shortlist kept its first-generation data forever (on
2026-08-23 MAN still showed its 08-04 price of $54.80 against a live $61.45).
The available workaround, force=True, is destructive — it resets research and the
Verdict to PENDING.

refresh_dossier_data is the highest-risk function in this module (it rewrites
files containing irreplaceable research), so it is tested directly rather than
only through its helper: DOSSIER_DIR is redirected to a temp dir and
build_dossier is stubbed, so nothing here touches the network or real dossiers.
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.dossier as dz
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
FRESH_DATA_HALF = DATA_HALF.replace("$108.17", "$141.11").replace("08-23", "08-27")

PENDING_RESEARCH_HALF = (
    f"{RESEARCH_MARKER} — filled Sunday via web research. -->\n\n"
    "## What they do\n_pending research_\n\n"
    f"## Verdict\n_{PENDING_MARKER} — not yet reviewed (ADMIT / WATCH / PASS)._\n"
)


class _DossierTempDir(unittest.TestCase):
    """Redirects DOSSIER_DIR at a temp dir and stubs the (network-bound) builder."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_dir = dz.DOSSIER_DIR
        self._orig_build = dz.build_dossier
        dz.DOSSIER_DIR = self.tmp
        self.build_calls = []

        def fake_build(item, force=False):
            path = os.path.join(dz.DOSSIER_DIR, f"{item['ticker']}.md")
            if os.path.exists(path) and not force:
                return None
            self.build_calls.append((item["ticker"], force))
            with open(path, "w") as f:
                f.write(FRESH_DATA_HALF + PENDING_RESEARCH_HALF)
            return path

        dz.build_dossier = fake_build

    def tearDown(self):
        dz.DOSSIER_DIR = self._orig_dir
        dz.build_dossier = self._orig_build
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, ticker, text):
        p = os.path.join(self.tmp, f"{ticker}.md")
        with open(p, "w") as f:
            f.write(text)
        return p

    def read(self, ticker):
        with open(os.path.join(self.tmp, f"{ticker}.md")) as f:
            return f.read()


class RefreshDossierData(_DossierTempDir):

    def test_updates_data_and_keeps_research_byte_for_byte(self):
        self.write("HALO", DATA_HALF + RESEARCH_HALF)
        dz.refresh_dossier_data({"ticker": "HALO"})
        out = self.read("HALO")
        _, research = _split_dossier(out)
        self.assertEqual(research, RESEARCH_HALF)       # the whole point
        self.assertIn("$141.11", out)
        self.assertNotIn("$108.17", out)
        self.assertNotIn(PENDING_MARKER, out)

    def test_verdict_survives(self):
        self.write("HALO", DATA_HALF + RESEARCH_HALF)
        dz.refresh_dossier_data({"ticker": "HALO"})
        self.assertEqual(dz.read_verdict("HALO")["status"], "ADMIT")
        self.assertFalse(dz.needs_research("HALO"))

    def test_refuses_when_no_marker_and_leaves_file_untouched(self):
        """A file with no seam must be declined, not guessed at."""
        original = "# hand-written notes\nirreplaceable text\n"
        self.write("MANUAL", original)
        result = dz.refresh_dossier_data({"ticker": "MANUAL"})
        self.assertIsNone(result)
        self.assertEqual(self.read("MANUAL"), original)
        self.assertEqual(self.build_calls, [])          # never even attempted

    def test_restores_original_when_build_raises(self):
        original = DATA_HALF + RESEARCH_HALF
        self.write("HALO", original)

        def boom(item, force=False):
            raise RuntimeError("network died mid-refresh")
        dz.build_dossier = boom

        with self.assertRaises(RuntimeError):
            dz.refresh_dossier_data({"ticker": "HALO"})
        self.assertEqual(self.read("HALO"), original)   # never half-written

    def test_restores_even_if_build_already_overwrote_the_file(self):
        """The dangerous window: builder succeeded (research now PENDING) and
        THEN something failed. Original must still come back."""
        original = DATA_HALF + RESEARCH_HALF
        self.write("HALO", original)
        real_fake = dz.build_dossier

        def build_then_fail(item, force=False):
            real_fake(item, force=force)                # clobbers with PENDING
            raise RuntimeError("failed after write")
        dz.build_dossier = build_then_fail

        with self.assertRaises(RuntimeError):
            dz.refresh_dossier_data({"ticker": "HALO"})
        self.assertEqual(self.read("HALO"), original)
        self.assertNotIn(PENDING_MARKER, self.read("HALO"))

    def test_builds_when_no_dossier_exists(self):
        result = dz.refresh_dossier_data({"ticker": "NEWCO"})
        self.assertIsNotNone(result)
        self.assertTrue(dz.needs_research("NEWCO"))
        self.assertEqual(self.build_calls, [("NEWCO", False)])

    def test_returns_none_without_a_ticker(self):
        self.assertIsNone(dz.refresh_dossier_data({"name": "no ticker"}))


class GenerateDossiers(_DossierTempDir):

    def test_mixed_shortlist_builds_new_and_refreshes_existing(self):
        self.write("HALO", DATA_HALF + RESEARCH_HALF)
        out = dz.generate_dossiers([{"ticker": "HALO"}, {"ticker": "NEWCO"}])
        self.assertEqual(out["built"], ["NEWCO"])
        self.assertEqual(out["refreshed"], ["HALO"])
        self.assertEqual(out["errored"], [])
        # the researched one kept its research; the new one is PENDING
        self.assertIn("ADMIT to growth_universe", self.read("HALO"))
        self.assertTrue(dz.needs_research("NEWCO"))

    def test_refresh_data_false_restores_old_skip_behaviour(self):
        original = DATA_HALF + RESEARCH_HALF
        self.write("HALO", original)
        out = dz.generate_dossiers([{"ticker": "HALO"}], refresh_data=False)
        self.assertEqual(out["skipped"], ["HALO"])
        self.assertEqual(self.read("HALO"), original)   # stale, but untouched

    def test_force_is_destructive_by_design(self):
        """Documents the sharp edge: force=True is a full reset and WILL discard
        research. Only safe when every name is still unresearched."""
        self.write("HALO", DATA_HALF + RESEARCH_HALF)
        dz.generate_dossiers([{"ticker": "HALO"}], force=True)
        self.assertTrue(dz.needs_research("HALO"))
        self.assertNotIn("ADMIT to growth_universe", self.read("HALO"))

    def test_one_failure_does_not_abort_the_batch(self):
        self.write("BAD", "# no marker here\n")
        real_fake = dz.build_dossier

        def selective(item, force=False):
            if item["ticker"] == "BOOM":
                raise RuntimeError("nope")
            return real_fake(item, force=force)
        dz.build_dossier = selective

        out = dz.generate_dossiers([{"ticker": "BOOM"}, {"ticker": "GOOD"}])
        self.assertEqual(out["errored"], ["BOOM"])
        self.assertEqual(out["built"], ["GOOD"])


class VerdictParsing(_DossierTempDir):
    """read_verdict/needs_research gate the whole Sunday research workflow —
    a misparse either re-researches a done name or graduates an unreviewed one."""

    def _verdict(self, verdict_line):
        self.write("T", DATA_HALF + RESEARCH_MARKER + " -->\n\n## Verdict\n"
                   + verdict_line + "\n")
        return dz.read_verdict("T")["status"]

    def test_each_status(self):
        cases = {
            "ADMIT to growth_universe — strong": "ADMIT",
            "WATCH (re-check on next shortlist)": "WATCH",
            "PASS (one-time revenue)": "PASS",
            f"_{PENDING_MARKER} — not yet reviewed._": "PENDING",
            "Something unparseable": "NONE",
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(self._verdict(line), expected)

    def test_italic_underscores_are_stripped(self):
        self.assertEqual(self._verdict("_ADMIT to growth_universe_"), "ADMIT")

    def test_blank_lines_before_verdict_text_are_skipped(self):
        self.write("T", DATA_HALF + RESEARCH_MARKER + " -->\n\n## Verdict\n\n\n"
                   "WATCH (thin)\n")
        self.assertEqual(dz.read_verdict("T")["status"], "WATCH")

    def test_missing_file_is_none_not_an_error(self):
        self.assertEqual(dz.read_verdict("NOPE")["status"], "NONE")
        self.assertFalse(dz.needs_research("NOPE"))

    def test_needs_research_tracks_the_sentinel(self):
        self.write("P", DATA_HALF + PENDING_RESEARCH_HALF)
        self.assertTrue(dz.needs_research("P"))
        self.write("D", DATA_HALF + RESEARCH_HALF)
        self.assertFalse(dz.needs_research("D"))


class SplitDossier(unittest.TestCase):

    def test_splits_on_the_marker(self):
        data, research = _split_dossier(DATA_HALF + RESEARCH_HALF)
        self.assertEqual(data, DATA_HALF)
        self.assertEqual(research, RESEARCH_HALF)

    def test_halves_rejoin_losslessly(self):
        original = DATA_HALF + RESEARCH_HALF
        data, research = _split_dossier(original)
        self.assertEqual(data + research, original)

    def test_missing_marker_yields_none_so_caller_can_refuse(self):
        data, research = _split_dossier("# hand-written\nirreplaceable\n")
        self.assertIsNone(research)
        self.assertEqual(data, "# hand-written\nirreplaceable\n")

    def test_marker_constant_matches_the_live_template(self):
        """If the template comment is reworded without updating the constant,
        every refresh would silently start refusing."""
        import inspect
        self.assertIn(RESEARCH_MARKER, inspect.getsource(dz.build_dossier))


if __name__ == "__main__":
    unittest.main()
