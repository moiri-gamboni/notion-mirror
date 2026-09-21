"""Every mirror row file says on its face that it is generated, when the row it
mirrors was last edited, and where the working copy lives.

No network: `render_row_md` is pure, and the one test that goes through
`upsert_row_md` stubs `refresh.probe_row`.
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import refresh  # noqa: E402  (_tools is not a package; discover's top dir is tests/)

RID = "aa" * 16
DB_ID = "bb" * 16
LE = "2026-08-01T12:34:00.000Z"


def page(rid=RID, title="Row One", last_edited=LE):
    p = {"id": refresh.dashed(rid),
         "properties": {"Name": {"type": "title", "title": [{"plain_text": title}]}}}
    if last_edited is not None:
        p["last_edited_time"] = last_edited
    return p


class HeaderShape(unittest.TestCase):
    """The exact bytes. A later renderer change cannot quietly drop or reword
    the line without failing here."""

    def test_the_header_line_is_exactly_this(self):
        want = ("<!-- notion:generated row_last_edited=2026-08-01T12:34:00.000Z | "
                "generated file: edits here are overwritten on the next mirror run. "
                "If tasks/ holds a folder for this row, tasks/<slug>/task.md is the "
                "working copy. -->")
        self.assertEqual(refresh.generated_header(page()), want)

    def test_it_is_line_two_between_the_row_marker_and_the_title(self):
        lines = refresh.render_row_md(page(), DB_ID, "Test DB", ["Name"], None, "").split("\n")
        self.assertTrue(lines[0].startswith("<!-- notion db row | id: "))
        self.assertEqual(lines[1], refresh.generated_header(page()))
        self.assertEqual(lines[2], "# Row One")

    def test_it_carries_all_three_things_the_plan_asks_for(self):
        h = refresh.generated_header(page())
        self.assertIn(LE, h)                      # when the mirrored row last changed
        self.assertIn("overwritten", h)           # editing here achieves nothing
        self.assertIn("tasks/<slug>/task.md", h)  # where the working copy lives

    def test_a_page_with_no_last_edited_time_renders_unknown_rather_than_crashing(self):
        h = refresh.generated_header(page(last_edited=None))
        self.assertIn("row_last_edited=unknown", h)
        self.assertNotIn("None", h)

    def test_the_detector_matches_what_the_renderer_writes(self):
        """`GENERATED_HEADER_RE` is the detector counterpart to the renderer's
        header: it is how any reader tells a stamped row from an unstamped one.
        Pinned here so the two cannot drift."""
        self.assertTrue(refresh.GENERATED_HEADER_RE.search(refresh.generated_header(page())))
        self.assertTrue(refresh.GENERATED_HEADER_RE.search(
            refresh.generated_header(page(last_edited=None))))


class NoChurn(unittest.TestCase):
    """The stamp is derived from the page, never from the clock. That is what
    keeps 83k row files from being rewritten every night."""

    def test_two_renders_of_one_page_are_byte_identical(self):
        a = refresh.render_row_md(page(), DB_ID, "Test DB", ["Name"], None, "")
        b = refresh.render_row_md(page(), DB_ID, "Test DB", ["Name"], None, "")
        self.assertEqual(a, b)

    def test_no_module_reads_the_clock_to_build_the_header(self):
        src = refresh.generated_header.__code__.co_names
        self.assertNotIn("now", src)
        self.assertNotIn("datetime", src)

    def test_the_stamp_moves_only_when_the_row_was_edited(self):
        base = refresh.render_row_md(page(), DB_ID, "Test DB", ["Name"], None, "")
        same = refresh.render_row_md(page(), DB_ID, "Test DB", ["Name"], None, "")
        moved = refresh.render_row_md(page(last_edited="2026-08-02T00:00:00.000Z"),
                                      DB_ID, "Test DB", ["Name"], None, "")
        self.assertEqual(base, same)
        self.assertNotEqual(base, moved)


class WriteThrough(unittest.TestCase):
    """Through the real write path: an unchanged row is not rewritten."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self._probe = refresh.probe_row
        refresh.probe_row = lambda *a, **kw: ("\n\n## Body\n\n- body line\n", False)
        self.addCleanup(setattr, refresh, "probe_row", self._probe)
        self.state = {"queue": [], "comment_rows": {}}
        self.report = {"dbs": {"errors": []}}
        self.args = type("A", (), {"dry_run": False})()

    def _upsert(self):
        refresh.upsert_row_md(None, None, page(), DB_ID, "Test DB", ["Name"],
                              self.dir, True, self.state, self.report, self.args)

    def test_a_second_identical_run_does_not_rewrite_the_file(self):
        self._upsert()
        path = os.path.join(self.dir, os.listdir(self.dir)[0])
        first = open(path).read()
        mtime = os.stat(path).st_mtime_ns
        self.assertIn("notion:generated", first)
        self._upsert()
        self.assertEqual(open(path).read(), first)
        self.assertEqual(os.stat(path).st_mtime_ns, mtime, "the file was rewritten")


class LeavesEverythingElseAlone(unittest.TestCase):
    """The header rides in the head, which every other reader treats as opaque."""

    def test_enrichment_round_trips_unchanged(self):
        enrich = "\n\n## Body\n\n- a line\n\n## Comments\n\n- _A (2026-07-01):_ hi\n"
        txt = refresh.render_row_md(page(), DB_ID, "Test DB", ["Name"], None, enrich)
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        p = os.path.join(d, f"Row One {RID}.md")
        with open(p, "w") as f:
            f.write(txt)
        self.assertEqual(refresh.existing_enrichment(p), enrich)

    def test_a_marker_partition_rebuild_keeps_the_header(self):
        """merge_backfill.py, repair_backfill_attribution.py and
        migrate_comment_ids.py all rebuild a row as head + MARKER + tail. If any
        of them ever stopped preserving the head verbatim, the header would be
        silently dropped from the rows they touch."""
        txt = refresh.render_row_md(page(), DB_ID, "Test DB", ["Name"], None,
                                    "\n\n## Body\n\n- a line\n")
        head, _, tail = txt.partition(refresh.MARKER)
        rebuilt = head + refresh.MARKER + tail
        self.assertEqual(rebuilt, txt)
        self.assertTrue(refresh.GENERATED_HEADER_RE.search(head))


if __name__ == "__main__":
    unittest.main()
