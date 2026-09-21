"""Probe-failure annotation: a row that carries enrichment we did not just
re-fetch says so on its face.

No network: `refresh.probe_row` is stubbed on every path through
`upsert_row_md`, so nothing here can reach Notion.
"""
import datetime as dt
import os
import shutil
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import refresh  # noqa: E402  (_tools is not a package; discover's top dir is tests/)


def today():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def page(rid, title="Row One"):
    return {"id": refresh.dashed(rid),
            "properties": {"Name": {"type": "title", "title": [{"plain_text": title}]}},
            "last_edited_time": "2026-08-01T00:00:00.000Z"}


BODY_AND_COMMENTS = ("\n\n## Body\n\n  stored body line\n\n"
                     "## Comments\n\n- _Someone (2026-07-01):_ stored comment\n")
COMMENTS_ONLY = "\n\n## Comments\n\n- _Someone (2026-07-01):_ stored comment\n"
FRESH = "\n\n## Body\n\n  fresh body line\n"


class AnnotationBase(unittest.TestCase):
    """One temp DB directory, one row, `probe_row` replaced by a stub."""

    RID = "aa" * 16
    DB_ID = "bb" * 16
    DB_TITLE = "Test DB"
    COLS = ["Name"]

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="a2-annotation-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.real_probe = refresh.probe_row
        self.addCleanup(setattr, refresh, "probe_row", self.real_probe)
        self.state = {"queue": [], "comment_rows": {}}
        self.report = refresh.new_report("test")
        self.args = types.SimpleNamespace(dry_run=False)
        self.page = page(self.RID)

    def stub_probe(self, result=None, raises=None):
        def _stub(api, users, page_id, dest_dir, report, old_comments_body="",
                  max_blocks=800, block_comment_cap=25, discovered=None):
            if raises is not None:
                raise raises
            return result
        refresh.probe_row = _stub

    def seed(self, enrichment):
        """Write the row's .md as it would stand after an earlier run."""
        txt = refresh.render_row_md(self.page, self.DB_ID, self.DB_TITLE, self.COLS,
                                    None, enrichment)
        with open(self.path(), "w") as f:
            f.write(txt)
        return txt

    def path(self):
        return os.path.join(self.dir, f"Row One {self.RID}.md")

    def run_upsert(self, probe=True):
        refresh.upsert_row_md(None, None, self.page, self.DB_ID, self.DB_TITLE,
                              self.COLS, self.dir, probe, self.state, self.report,
                              self.args)
        with open(self.path()) as f:
            return f.read()

    def annotations(self, txt):
        return refresh.PROBE_FAIL_RE.findall(txt)


class TestFailedProbe(AnnotationBase):

    def test_api_error_keeps_enrichment_and_annotates_it(self):
        self.seed(BODY_AND_COMMENTS)
        self.stub_probe(raises=refresh.ApiError(500, "upstream boom"))
        txt = self.run_upsert()
        self.assertIn("stored body line", txt)
        self.assertIn("stored comment", txt)
        self.assertIn(f"## Body\n\n_[probe failed {today()}]_\n\n  stored body line", txt)
        self.assertEqual(self.annotations(txt), [today()])

    def test_run_report_records_the_annotated_row(self):
        self.seed(BODY_AND_COMMENTS)
        self.stub_probe(raises=refresh.ApiError(500, "upstream boom"))
        self.run_upsert()
        rec = self.report["dbs"]["probe_annotated"]
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["row"], self.RID)
        self.assertEqual(rec[0]["db"], self.DB_TITLE)
        self.assertIn("500", rec[0]["why"])

    def test_comments_only_row_is_annotated_under_the_comments_heading(self):
        self.seed(COMMENTS_ONLY)
        self.stub_probe(raises=refresh.ApiError(502, "bad gateway"))
        txt = self.run_upsert()
        self.assertIn(f"## Comments\n\n_[probe failed {today()}]_\n\n- _Someone", txt)
        self.assertEqual(self.annotations(txt), [today()])

    def test_row_with_no_stored_enrichment_gains_nothing(self):
        # Nothing stale to flag — and an annotation would be text after the
        # marker, which is exactly what db_probe_policy's enriched-only rule
        # reads as "this row has a body, keep probing it".
        self.seed("")
        self.stub_probe(raises=refresh.ApiError(500, "boom"))
        txt = self.run_upsert()
        self.assertNotIn("probe failed", txt)
        self.assertNotIn("## Body", txt)
        self.assertEqual(refresh.existing_enrichment(self.path()).strip(), "")

    def test_capped_block_scan_annotates_the_fresh_enrichment(self):
        # The body came back fine but block-anchored comments were carried over
        # unverified: the row is not fully current and must not read as if it is.
        self.seed(BODY_AND_COMMENTS)
        self.stub_probe(result=(FRESH, True))
        txt = self.run_upsert()
        self.assertIn("fresh body line", txt)
        self.assertEqual(self.annotations(txt), [today()])
        self.assertEqual(len(self.report["dbs"]["probe_annotated"]), 1)
        self.assertIn("capped", self.report["dbs"]["probe_annotated"][0]["why"])

    def test_repeat_failure_keeps_the_date_it_was_first_missed(self):
        """The date means "first missed", matching the retention stamp's ≤date
        semantics. Re-stamping today's on every run churned the tree: `capped`
        is a function of body size, so a big-bodied row caps on every probe
        forever, and the byte-difference write gate then rewrites the same rows
        nightly with the date as their entire diff."""
        self.seed("\n\n## Body\n\n_[probe failed 2020-01-01]_\n\n  stored body line\n")
        self.stub_probe(raises=refresh.ApiError(500, "boom"))
        txt = self.run_upsert()
        self.assertEqual(self.annotations(txt), ["2020-01-01"])
        self.assertIn("  stored body line", txt)

    def test_a_still_capping_row_is_not_rewritten_the_next_day(self):
        """The churn itself, as an observable. Same-day re-annotation was already
        byte-stable; what was not is the day boundary — an unchanged row whose
        scan caps again tomorrow rewrote with the date as its whole diff, every
        night, forever."""
        self.seed(refresh.annotate_probe_failure(FRESH, "2020-01-01"))
        self.stub_probe(result=(FRESH, True))
        before = os.stat(self.path()).st_mtime_ns
        os.utime(self.path(), ns=(before - 10**9, before - 10**9))
        stamped = os.stat(self.path()).st_mtime_ns
        self.run_upsert()
        self.assertEqual(os.stat(self.path()).st_mtime_ns, stamped)

    def test_a_first_failure_is_stamped_with_today(self):
        self.seed(BODY_AND_COMMENTS)
        self.stub_probe(raises=refresh.ApiError(500, "boom"))
        self.assertEqual(self.annotations(self.run_upsert()), [today()])

    def test_budget_defers_the_row_without_annotating_it(self):
        # A budget wall stops every remaining row at once and each one is queued
        # for the next run; annotating would rewrite thousands of files and
        # unwrite them tomorrow.
        self.seed(BODY_AND_COMMENTS)
        self.stub_probe(raises=refresh.Budget())
        txt = self.run_upsert()
        self.assertNotIn("probe failed", txt)
        self.assertEqual(self.state["queue"],
                         [{"kind": "row_probe", "db": self.DB_ID, "row": self.RID}])
        self.assertEqual(self.report["dbs"]["probe_annotated"], [])

    def test_unprobed_row_keeps_its_existing_annotation(self):
        # probe=False conflates three callers — the enriched-only policy skip, a
        # property-only change, and first sight during state seeding — so it can
        # neither add nor clear an annotation.
        self.seed("\n\n## Body\n\n_[probe failed 2020-01-01]_\n\n  stored body line\n")
        self.stub_probe(raises=AssertionError("probe_row must not be called"))
        txt = self.run_upsert(probe=False)
        self.assertEqual(self.annotations(txt), ["2020-01-01"])
        self.assertEqual(self.report["dbs"]["probe_annotated"], [])


class TestSuccessfulProbe(AnnotationBase):

    def test_success_clears_a_previous_annotation(self):
        self.seed("\n\n## Body\n\n_[probe failed 2020-01-01]_\n\n  stored body line\n")
        self.stub_probe(result=(FRESH, False))
        txt = self.run_upsert()
        self.assertNotIn("probe failed", txt)
        self.assertIn("fresh body line", txt)
        self.assertEqual(refresh.existing_enrichment(self.path()), FRESH)

    def test_no_write_when_the_text_is_unchanged(self):
        self.seed(FRESH)
        self.stub_probe(result=(FRESH, False))
        before = os.stat(self.path()).st_mtime_ns
        os.utime(self.path(), ns=(before - 10**9, before - 10**9))
        stamped = os.stat(self.path()).st_mtime_ns
        self.run_upsert()
        self.assertEqual(os.stat(self.path()).st_mtime_ns, stamped)

    def test_no_write_when_a_still_failing_row_is_re_annotated_the_same_day(self):
        self.seed(f"\n\n## Body\n\n_[probe failed {today()}]_\n\n  stored body line\n")
        self.stub_probe(raises=refresh.ApiError(500, "boom"))
        before = os.stat(self.path()).st_mtime_ns
        os.utime(self.path(), ns=(before - 10**9, before - 10**9))
        stamped = os.stat(self.path()).st_mtime_ns
        self.run_upsert()
        self.assertEqual(os.stat(self.path()).st_mtime_ns, stamped)
        # still reported: the row is stale today whether or not the byte changed
        self.assertEqual(len(self.report["dbs"]["probe_annotated"]), 1)


class TestProbePolicyUnaffected(AnnotationBase):
    """The reason unenriched rows are left unmarked, stated as a test.

    db_probe_policy's enriched-only rule reads "does this .md carry anything
    after the marker" — so annotating a row that carries nothing would enrol it
    in the probe set permanently. On the 80k jobs DB that is ~1,900 skipped rows
    a run turning into ~1,900 body+comment walks a run, for good.
    """

    def test_a_failed_probe_on_a_bare_row_leaves_the_policy_alone(self):
        for n in range(300):
            p = os.path.join(self.dir, f"Row {n} " + f"{n:032x}" + ".md")
            with open(p, "w") as f:
                f.write(refresh.render_row_md(page(f"{n:032x}", f"Row {n}"),
                                              self.DB_ID, self.DB_TITLE, self.COLS, None, ""))
        self.seed("")
        self.stub_probe(raises=refresh.ApiError(500, "boom"))
        self.run_upsert()
        state = {"probe_policy": {}}
        self.assertEqual(refresh.db_probe_policy(self.dir, 301, state, self.DB_ID), "enriched")
        enr = refresh.existing_enrichment(self.path())
        self.assertFalse(enr and enr.strip())  # the per-row rule still skips it


class TestAnnotationHelpers(unittest.TestCase):

    def test_annotate_places_the_line_under_the_first_heading(self):
        out = refresh.annotate_probe_failure(BODY_AND_COMMENTS, "2026-08-06")
        self.assertIn("## Body\n\n_[probe failed 2026-08-06]_\n\n  stored body line", out)
        self.assertNotIn("## Comments\n\n_[probe", out)

    def test_annotate_is_idempotent_and_never_stacks(self):
        once = refresh.annotate_probe_failure(BODY_AND_COMMENTS, "2026-08-06")
        twice = refresh.annotate_probe_failure(once, "2026-08-06")
        self.assertEqual(once, twice)
        later = refresh.annotate_probe_failure(twice, "2026-08-07")
        self.assertEqual(refresh.PROBE_FAIL_RE.findall(later), ["2026-08-07"])

    def test_strip_restores_the_original_text(self):
        for src in (BODY_AND_COMMENTS, COMMENTS_ONLY, FRESH):
            with self.subTest(src=src[:20]):
                marked = refresh.annotate_probe_failure(src, "2026-08-06")
                self.assertEqual(refresh.strip_probe_annotation(marked), src)

    def test_strip_leaves_a_body_line_that_looks_like_an_annotation(self):
        """A real body line reading `_[probe failed YYYY-MM-DD]_` is now
        reachable: before the indent-0 walk every body line carried two leading
        spaces and the anchored regex could not match one. The strip is bounded
        to the slot the annotation is written into, so a look-alike further down
        survives — silently deleting a line of somebody's body is the one thing
        this helper must not do."""
        body = ("\n\n## Body\n\n  a real body line\n_[probe failed 2020-01-01]_\n\n"
                "  another body line\n")
        self.assertEqual(refresh.strip_probe_annotation(body), body)

    def test_strip_still_removes_the_annotation_ahead_of_such_a_line(self):
        """And the bound must not cost it the annotation it exists to remove."""
        body = ("\n\n## Body\n\n  a real body line\n_[probe failed 2020-01-01]_\n\n"
                "  another body line\n")
        marked = refresh.annotate_probe_failure(body, "2026-08-06")
        self.assertEqual(refresh.strip_probe_annotation(marked), body)

    def test_strip_handles_a_region_handed_over_without_its_heading(self):
        """A caller that splits the enrichment into regions and strips each one
        hands the annotation over leading a fragment with no heading above it.
        That is the shape an indent measurement over a region depends on."""
        self.assertEqual(
            refresh.strip_probe_annotation("\n_[probe failed 2026-08-01]_\n\n  - outer\n"),
            "\n  - outer\n")

    def test_strip_finds_a_leading_annotation_ahead_of_a_later_heading(self):
        """A heading-less fragment can still contain a heading further down —
        `comments_heading` hands over everything after one `## Comments` while a
        second may follow. Keying off the first heading found anywhere skips past
        the annotation that leads the fragment, and that region then reads as
        indent 0 and disowns its own comments."""
        frag = "\n_[probe failed 2020-01-01]_\n\n- _A (2026-01-01):_ hi\n## Comments\n- _B (2026-01-02):_ yo\n"
        self.assertEqual(refresh.strip_probe_annotation(frag),
                         "\n- _A (2026-01-01):_ hi\n## Comments\n- _B (2026-01-02):_ yo\n")

    def test_strip_leaves_unannotated_text_alone(self):
        self.assertEqual(refresh.strip_probe_annotation(BODY_AND_COMMENTS), BODY_AND_COMMENTS)
        self.assertEqual(refresh.strip_probe_annotation(""), "")
        self.assertIsNone(refresh.strip_probe_annotation(None))

    def test_annotate_without_a_section_is_a_no_op(self):
        self.assertEqual(refresh.annotate_probe_failure("", "2026-08-06"), "")
        self.assertEqual(refresh.annotate_probe_failure("\n", "2026-08-06"), "\n")

    def test_probe_annotation_reads_the_date_back(self):
        marked = refresh.annotate_probe_failure(BODY_AND_COMMENTS, "2026-08-06")
        self.assertEqual(refresh.probe_annotation(marked), "2026-08-06")
        self.assertIsNone(refresh.probe_annotation(BODY_AND_COMMENTS))

    def test_annotation_never_reaches_the_comment_merge(self):
        # On a comments-only row the annotation lands inside the slice
        # extract_comments_body hands to the merge; it must not survive as a
        # bullet, or the next probe would carry it into the comment record.
        marked = refresh.annotate_probe_failure(COMMENTS_ONLY, "2026-08-06")
        slice_ = refresh.extract_comments_body(marked)
        self.assertIn("probe failed", slice_)
        self.assertEqual(refresh.split_bullets(slice_, prefix="- _"),
                         ["- _Someone (2026-07-01):_ stored comment"])
        merged, _ = refresh.merge_comment_bullets(slice_, [], "2026-08-07", prefix="- _")
        self.assertTrue(all("probe failed" not in b for b in merged))

    def test_annotation_survives_a_render_round_trip(self):
        marked = refresh.annotate_probe_failure(BODY_AND_COMMENTS, "2026-08-06")
        txt = refresh.render_row_md(page("cc" * 16), "dd" * 16, "DB", ["Name"], None, marked)
        self.assertEqual(txt.split(refresh.MARKER, 1)[1], marked)


if __name__ == "__main__":
    unittest.main()
