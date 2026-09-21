"""Offline tests for the `--mode validate --refetch` selection and comparison.

The live gate (re-probe 20 rows, expect zero mismatches) can only run against
the real mirror. What can go wrong silently *here* is the sampler picking rows
that exercise nothing, or the date mask hiding a real difference — a run that
reports "0 mismatches" over a vacuous sample looks identical to a clean one.
"""
import argparse
import os
import shutil
import tempfile
import unittest
import warnings

from fixture_support import refresh

MARKER = refresh.MARKER


def row_md(enrichment):
    return ("<!-- notion db row | id: x | db: y (Z) -->\n# Row\n\n"
            "| Property | Value |\n|---|---|\n\n" + MARKER + enrichment)


class MaskStamps(unittest.TestCase):
    def test_retention_stamp_is_masked(self):
        a = "- _Someone (2026-01-01):_ hi _[resolved/deleted ≤2026-08-01]_"
        b = "- _Someone (2026-01-01):_ hi _[resolved/deleted ≤2026-08-06]_"
        self.assertEqual(refresh.mask_stamps(a), refresh.mask_stamps(b))

    def test_real_difference_survives_the_mask(self):
        a = "- _Someone (2026-01-01):_ hi _[resolved/deleted ≤2026-08-01]_"
        b = "- _Someone (2026-01-01):_ bye _[resolved/deleted ≤2026-08-01]_"
        self.assertNotEqual(refresh.mask_stamps(a), refresh.mask_stamps(b))

    def test_a_dropped_comment_is_not_masked_away(self):
        """The mask removes the annotation, never the bullet it annotates."""
        disk = "\n\n## Comments\n\n- _A (2026-01-01):_ kept _[resolved/deleted ≤2026-08-01]_\n"
        self.assertIn("kept", refresh.mask_stamps(disk))
        self.assertNotEqual(refresh.mask_stamps(disk), refresh.mask_stamps("\n\n## Comments\n\n"))

    def test_a_probe_failure_annotation_is_masked(self):
        """A re-probe renders fresh enrichment, which carries no annotation,
        while disk carries one on every row whose last probe was capped (28 of
        them today). Unmasked, each reads as a MISMATCH whose entire diff is the
        annotation line — degrading the gate three units tell the operator to
        run at integration."""
        fresh = "\n\n## Body\n\n  a body line\n"
        disk = refresh.annotate_probe_failure(fresh, "2026-08-06")
        self.assertNotEqual(fresh, disk)
        self.assertEqual(refresh.mask_stamps(fresh), refresh.mask_stamps(disk))

    def test_masking_an_annotation_does_not_swallow_the_body(self):
        """The mask drops the annotation line, not the enrichment carrying it."""
        disk = refresh.annotate_probe_failure("\n\n## Body\n\n  a body line\n", "2026-08-06")
        self.assertIn("a body line", refresh.mask_stamps(disk))


class Candidates(unittest.TestCase):
    def setUp(self):
        # existing_enrichment reads with a bare open(); that is production
        # behaviour, not something this test is measuring
        warnings.filterwarnings("ignore", category=ResourceWarning)
        self.tmp = tempfile.mkdtemp()
        self._dbs = refresh.DBS
        refresh.DBS = self.tmp
        self.addCleanup(setattr, refresh, "DBS", self._dbs)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def db(self, name, rows):
        d = os.path.join(self.tmp, name)
        os.makedirs(d)
        for rid, enrich in rows:
            with open(os.path.join(d, f"Row {rid}.md"), "w") as f:
                f.write(row_md(enrich))
        return d

    def args(self, **kw):
        return argparse.Namespace(**{"dbs": "", "refetch_sample": 20, **kw})

    def test_comment_bearing_rows_come_first(self):
        self.db("Tasks " + "a" * 32, [
            ("b" * 32, "\n\n## Body\n\nplain body\n"),
            ("c" * 32, "\n\n## Comments\n\n- _A (2026-01-01):_ hi\n"),
        ])
        commented, bodies = refresh.refetch_candidates(self.args(), 20)
        self.assertEqual(["c" * 32], [c[2] for c in commented])
        self.assertEqual(["b" * 32], [b[2] for b in bodies])

    def test_unenriched_rows_are_skipped(self):
        self.db("Feed " + "a" * 32, [("b" * 32, "\n"), ("c" * 32, "\n\n")])
        commented, bodies = refresh.refetch_candidates(self.args(), 20)
        self.assertEqual(([], []), (commented, bodies))

    def test_dbs_filter_selects_one_database(self):
        self.db("Tasks " + "a" * 32, [("b" * 32, "\n\n## Comments\n\n- _A (2026-01-01):_ hi\n")])
        self.db("People " + "d" * 32, [("e" * 32, "\n\n## Comments\n\n- _A (2026-01-01):_ yo\n")])
        commented, _ = refresh.refetch_candidates(self.args(dbs="Tasks"), 20)
        self.assertEqual(["b" * 32], [c[2] for c in commented])

    def test_scanning_stops_once_the_sample_is_filled(self):
        """A bare --refetch must not walk all 74k mirrored rows to sample 20."""
        for i in range(3):
            self.db(f"{i}db " + chr(97 + i) * 32,
                    [(f"{i}{'f' * 31}", "\n\n## Comments\n\n- _A (2026-01-01):_ hi\n")])
        commented, _ = refresh.refetch_candidates(self.args(), 1)
        self.assertEqual(1, len(commented))


if __name__ == "__main__":
    unittest.main()
