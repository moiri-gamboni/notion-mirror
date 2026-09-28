"""Webhook-captured comments reach the mirror without a rescan.

The receiver captures a comment's whole thread within about a minute of any
comment.* event, so by the nightly the comment is already on disk in the capture
log. Folding it into `_comments.md` or the row file costs nothing; what used to
happen instead was a full per-block rescan of the page it sat on (2,882 requests
for five pages on 2026-09-23). These pin the fold's rules: a capture newer than
the page's last scan is authoritative, an older one may only add what the scan
never had (and then as resolved), a comment.deleted event marks the bullet, and
folding the same log twice changes nothing.
"""
import json
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh  # noqa: E402
from fake_notion import FakeUsers, MirrorSandbox, page_obj, rt  # noqa: E402

PAGE, ROW, DB = "a1" * 16, "b2" * 16, "c3" * 16
C1, C2, C3 = "d1" * 16, "d2" * 16, "d3" * 16
SCAN = "2026-09-10T00:00:00.000Z"
BEFORE, AFTER = "2026-09-05T12:00:00.000000+00:00", "2026-09-20T12:00:00.000000+00:00"
TODAY = "2026-09-24"


def cap(cid, text, at=AFTER, did=None):
    return {"id": refresh.dashed(cid), "text": text, "rich_text": rt(text),
            "author_id": "uu" * 16, "created_time": "2026-09-01T09:00:00.000Z",
            "discussion_id": refresh.dashed(did or cid), "_captured_at": at}


def bullet(cid, text, row=False):
    return refresh.captured_bullet("(page-level)", cap(cid, text), FakeUsers(), row)


class FoldBullets(unittest.TestCase):
    def fold(self, stored, caps, last_scan=SCAN, deletions=None):
        return refresh.fold_bullets(stored, [("(page-level)", c) for c in caps], last_scan,
                                    FakeUsers(), False, deletions or {}, TODAY)

    def test_a_capture_newer_than_the_scan_is_added_open(self):
        out, st = self.fold([], [cap(C1, "new comment")])
        self.assertEqual(out, [bullet(C1, "new comment")])
        self.assertEqual(st["added"], 1)

    def test_a_page_never_scanned_takes_every_capture_as_current(self):
        out, _ = self.fold([], [cap(C1, "x", at=BEFORE)], last_scan="")
        self.assertNotIn("resolved/deleted", out[0])

    def test_a_capture_older_than_the_scan_that_the_scan_never_had_was_resolved(self):
        out, _ = self.fold([], [cap(C1, "gone", at=BEFORE)])
        self.assertIn("_[resolved/deleted ≤2026-09-10]_", out[0])

    def test_an_edit_captured_after_the_scan_updates_the_bullet_in_place(self):
        stored = [bullet(C2, "other"), bullet(C1, "first draft")]
        out, st = self.fold(stored, [cap(C1, "edited")])
        self.assertEqual(out, [bullet(C2, "other"), bullet(C1, "edited")])
        self.assertEqual(st["updated"], 1)

    def test_with_no_scan_on_record_a_capture_adds_but_never_rewrites(self):
        stored = [bullet(C1, "read by some later probe")]
        out, _ = self.fold(stored, [cap(C1, "older capture"), cap(C2, "missing")], last_scan="")
        self.assertEqual(out, stored + [bullet(C2, "missing")])

    def test_a_flattened_capture_never_rewrites_a_rendered_bullet(self):
        flat = cap(C1, "flattened")
        flat.pop("rich_text")
        stored = [bullet(C1, "[link](https://x) rendered")]
        out, _ = self.fold(stored, [flat])
        self.assertEqual(out, stored)

    def test_a_capture_older_than_the_scan_never_reverts_it(self):
        stored = [bullet(C1, "text the scan read")]
        out, _ = self.fold(stored, [cap(C1, "older text", at=BEFORE)])
        self.assertEqual(out, stored)

    def test_a_resolved_bullet_is_not_reopened_by_a_capture(self):
        stored = [refresh.annotate_resolved(bullet(C1, "done"), "2026-09-15")]
        out, _ = self.fold(stored, [cap(C1, "done again")])
        self.assertEqual(out, stored)

    def test_a_backfill_record_is_older_than_any_scan(self):
        out, _ = self.fold([], [cap(C1, "recovered", at="backfill")], last_scan="")
        self.assertIn("resolved/deleted ≤2026-09-24", out[0])

    def test_a_deleted_comment_is_marked_with_the_event_date(self):
        stored = [bullet(C1, "keep"), bullet(C2, "deleted later")]
        out, st = self.fold(stored, [], deletions={C2: "2026-09-21"})
        self.assertEqual(out[0], bullet(C1, "keep"))
        self.assertIn("_[resolved/deleted ≤2026-09-21]_", out[1])
        self.assertEqual(st["deleted"], 1)

    def test_folding_twice_changes_nothing(self):
        caps = [cap(C1, "a"), cap(C2, "b", at=BEFORE), cap(C3, "c")]
        once, _ = self.fold([bullet(C3, "old c")], caps, deletions={C1: "2026-09-22"})
        twice, st = self.fold(once, caps, deletions={C1: "2026-09-22"})
        self.assertEqual(once, twice)
        self.assertEqual(st, {"added": 0, "updated": 0, "deleted": 0})


class WithComments(unittest.TestCase):
    """The fold rewrites only the comments region, in the exact bytes a probe
    would, so the next probe of an unchanged row does not rewrite the file."""

    def probe_bytes(self, body, bullets):
        parts = (refresh.body_section_lines(body) if body else []) + \
            (refresh.comments_section_lines(bullets) if bullets else [])
        return "\n\n" + "\n".join(parts).strip("\n") + "\n" if parts else ""

    def test_matches_probe_row_bytes(self):
        b1, b2 = bullet(C1, "one", True), bullet(C2, "two\n  line", True)
        for body in ("", "- a body line\n  - nested"):
            for old, new in (([], [b1]), ([b1], [b1, b2]), ([b1, b2], [b2])):
                with self.subTest(body=body, old=len(old), new=len(new)):
                    self.assertEqual(refresh.with_comments(self.probe_bytes(body, old), new),
                                     self.probe_bytes(body, new))

    def test_keeps_a_probe_annotation_above_the_body(self):
        enr = refresh.annotate_probe_failure(self.probe_bytes("body", []), "2026-09-01")
        out = refresh.with_comments(enr, [bullet(C1, "c", True)])
        self.assertIn("_[probe failed 2026-09-01]_", out)
        self.assertIn(refresh.COMMENTS_OPEN, out)


class FoldCaptures(MirrorSandbox):
    def setUp(self):
        super().setUp()
        self.dbdir = os.path.join(self.dbs, f"Tasks {DB}")
        os.makedirs(self.dbdir)
        self.row_path = os.path.join(self.dbdir, f"Row {ROW}.md")
        body = "\n\n" + "\n".join(refresh.body_section_lines("- the row body")) + "\n"
        with open(self.row_path, "w") as f:
            f.write("<!-- notion db row -->\n# Row\n\n" + refresh.MARKER + body)
        self.write_meta(page_obj(PAGE, "A page"))
        self.args = types.SimpleNamespace(dry_run=False)
        self.report = refresh.new_report("daily")
        self.st = self.state()

    def capture_log(self, *records):
        with open(os.path.join(self.state_dir, "webhook-comments-capture.jsonl"), "w") as f:
            for page_id, comments, at in records:
                f.write(json.dumps({"captured_at": at, "page_id": page_id,
                                    "anchor": "(page-level)", "comments": comments}) + "\n")

    def events(self, *deleted):
        with open(os.path.join(self.state_dir, "webhook-events.jsonl"), "w") as f:
            for cid, ts in deleted:
                f.write(json.dumps({"type": "comment.deleted",
                                    "entity": {"id": refresh.dashed(cid), "type": "comment"},
                                    "timestamp": ts}) + "\n")

    def raw(self, cid, text):
        c = cap(cid, text)
        c.pop("_captured_at")
        return c

    def run_fold(self):
        refresh._WEBHOOK_CAPTURES = None
        refresh.fold_captures(FakeUsers(), self.st, self.report, self.args)

    def test_row_and_page_comments_land_without_a_request(self):
        self.capture_log((ROW, [self.raw(C1, "on the row")], AFTER),
                         (PAGE, [self.raw(C2, "on the page")], AFTER))
        self.run_fold()
        row = self.read(self.row_path)
        self.assertIn("- the row body", row)
        self.assertIn("on the row", refresh.extract_comments_body(row.split(refresh.MARKER, 1)[1]))
        cm = self.read(os.path.join(self.ws, "_comments.md"))
        self.assertIn(f"## A page  `{PAGE}`", cm)
        self.assertIn("on the page", cm)
        self.assertIn(ROW, self.st["comment_rows"], "a newly commented row joins the audit pool")
        self.assertEqual(self.report["comments"]["folded"]["added"], 2)

    def test_a_comment_already_held_by_another_object_is_not_folded_in_again(self):
        """A backfill record filed a child page's comment under its parent; the
        comment lives with the child, and the fold must not copy it back."""
        self.capture_log((PAGE, [self.raw(C2, "on the child page")], AFTER))
        self.run_fold()
        self.capture_log((PAGE, [self.raw(C2, "on the child page")], AFTER),
                         (ROW, [self.raw(C2, "on the child page")], "backfill"))
        self.report = refresh.new_report("daily")
        self.run_fold()
        body = refresh.extract_comments_body(self.read(self.row_path).split(refresh.MARKER, 1)[1])
        self.assertNotIn("on the child page", body)
        self.assertEqual(self.report["comments"]["folded"]["held_elsewhere"], 1)

    def test_a_new_comment_goes_where_its_live_capture_says(self):
        self.capture_log((ROW, [self.raw(C2, "misfiled by a backfill")], "backfill"),
                         (PAGE, [self.raw(C2, "misfiled by a backfill")], AFTER))
        self.run_fold()
        body = refresh.extract_comments_body(self.read(self.row_path).split(refresh.MARKER, 1)[1])
        self.assertNotIn("misfiled", body)
        self.assertIn("misfiled", self.read(os.path.join(self.ws, "_comments.md")))

    def test_the_latest_capture_of_an_edited_comment_wins(self):
        self.capture_log((ROW, [self.raw(C1, "draft")], BEFORE),
                         (ROW, [self.raw(C1, "final")], AFTER))
        self.run_fold()
        body = refresh.extract_comments_body(self.read(self.row_path).split(refresh.MARKER, 1)[1])
        self.assertIn("final", body)
        self.assertNotIn("draft", body)

    def test_a_deleted_event_marks_the_bullet_on_the_page(self):
        self.capture_log((PAGE, [self.raw(C2, "soon deleted")], AFTER))
        self.run_fold()
        self.events((C2, "2026-09-22T10:00:00.000Z"))
        self.run_fold()
        self.assertIn("_[resolved/deleted ≤2026-09-22]_",
                      self.read(os.path.join(self.ws, "_comments.md")))

    def test_a_second_fold_writes_nothing(self):
        self.capture_log((ROW, [self.raw(C1, "c")], AFTER), (PAGE, [self.raw(C2, "p")], AFTER))
        self.run_fold()
        before = {p: os.path.getmtime(p) for p in (self.row_path,
                                                   os.path.join(self.ws, "_comments.md"))}
        self.report = refresh.new_report("daily")
        self.run_fold()
        self.assertEqual(self.report["comments"]["folded"]["pages"] +
                         self.report["comments"]["folded"]["rows"], 0)
        self.assertEqual(before, {p: os.path.getmtime(p) for p in before})

    def test_a_dry_run_reports_and_writes_nothing(self):
        self.capture_log((ROW, [self.raw(C1, "c")], AFTER), (PAGE, [self.raw(C2, "p")], AFTER))
        before = self.read(self.row_path)
        self.args.dry_run = True
        self.run_fold()
        self.assertEqual(self.read(self.row_path), before)
        self.assertFalse(os.path.exists(os.path.join(self.ws, "_comments.md")))
        self.assertEqual(self.report["comments"]["folded"]["added"], 2)

    def test_a_capture_on_a_page_not_mirrored_yet_waits(self):
        self.capture_log(("e5" * 16, [self.raw(C3, "elsewhere")], AFTER))
        self.run_fold()
        self.assertEqual(self.report["comments"]["folded"]["unplaced"], 1)
        self.assertFalse(os.path.exists(os.path.join(self.ws, "_comments.md")))


if __name__ == "__main__":
    unittest.main()
