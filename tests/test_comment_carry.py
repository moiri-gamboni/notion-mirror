"""A body edit re-walks the body and carries the comments; it does not rescan them.

A row's `last_edited_time` moves when its body or properties change, never when
someone comments, so re-reading every block's comments on a body probe paid one
request per block for news the webhook capture already delivered. Body probes
now carry the stored comments over (with the row's captures folded in), the way
a props probe carries the body, and per-block comment reads happen only where
nothing else can see: a row or page the mirror has never read comments for, and
the rolling audit.
"""
import json
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh  # noqa: E402
from fake_notion import (FakeNotion, MirrorSandbox, comment, page_obj,  # noqa: E402
                         paragraph, rt)

ROW, DB, BLOCK, PAGE = "a1" * 16, "c3" * 16, "e7" * 16, "f8" * 16
C1, C2 = "d1" * 16, "d2" * 16


def stored_bullet(cid, text):
    return refresh.stamp_cid(f"- _Someone (2026-09-01):_ {text}", cid, cid)


class RowProbeModes(MirrorSandbox):
    def setUp(self):
        super().setUp()
        self.dbdir = os.path.join(self.dbs, f"Tasks {DB}")
        os.makedirs(self.dbdir)
        self.report = refresh.new_report("daily")
        self.args = types.SimpleNamespace(dry_run=False)
        self.st = self.state()
        self.api = FakeNotion(children={ROW: [paragraph(BLOCK, "the new body")]},
                              comments={ROW: [comment(C2, "live page-level")],
                                        BLOCK: [comment(C1, "live on block")]})

    def seed(self, enrichment):
        page = page_obj(ROW, "Row", "database_id", DB)
        txt = refresh.render_row_md(page, DB, "Tasks", [], self.users, enrichment)
        self.path = os.path.join(self.dbdir, f"Row {ROW}.md")
        with open(self.path, "w") as f:
            f.write(txt)
        return page

    def upsert(self, page, probe=True):
        refresh.upsert_row_md(self.api, self.users, page, DB, "Tasks", [], self.dbdir, probe,
                              self.st, self.report, self.args)
        return refresh.existing_enrichment(self.path)

    def enriched(self, *bullets):
        return "\n\n" + "\n".join(refresh.body_section_lines("old body")
                                  + refresh.comments_section_lines(list(bullets))) + "\n"

    def test_an_enriched_row_carries_its_comments_without_a_comment_request(self):
        page = self.seed(self.enriched(stored_bullet(C1, "stored comment")))
        enr = self.upsert(page)
        self.assertEqual(self.api.comment_calls(), [])
        self.assertIn("the new body", enr)
        self.assertIn("stored comment", enr)
        self.assertNotIn("live on block", enr)
        self.assertEqual(self.report["comments"]["row_probes"], {"carry": 1, "scan": 0})
        self.assertNotIn(ROW, self.st["comment_scans"], "a carry is not a read")

    def test_a_carry_folds_the_rows_captures_in(self):
        page = self.seed(self.enriched(stored_bullet(C1, "stored comment")))
        with open(os.path.join(self.state_dir, "webhook-comments-capture.jsonl"), "w") as f:
            f.write(json.dumps({"captured_at": "2026-09-20T00:00:00+00:00", "page_id": ROW,
                                "anchor": "(page-level)", "comments": [
                                    {"id": refresh.dashed(C2), "rich_text": rt("captured"),
                                     "author_id": "uu" * 16,
                                     "created_time": "2026-09-19T00:00:00.000Z",
                                     "discussion_id": refresh.dashed(C2)}]}) + "\n")
        enr = self.upsert(page)
        self.assertIn("stored comment", enr)
        self.assertIn("captured", enr)
        self.assertEqual(self.api.comment_calls(), [])

    def test_a_row_never_enriched_is_scanned_once(self):
        page = self.seed("")
        enr = self.upsert(page)
        self.assertEqual(sorted(self.api.comment_calls()), sorted([ROW, BLOCK]))
        self.assertIn("live on block", enr)
        self.assertIn(ROW, self.st["comment_scans"])

    def test_scan_forces_a_read_and_records_it(self):
        page = self.seed(self.enriched(stored_bullet(C1, "stale text")))
        enr = self.upsert(page, probe="scan")
        self.assertEqual(sorted(self.api.comment_calls()), sorted([ROW, BLOCK]))
        self.assertIn("live on block", enr)
        self.assertIn(ROW, self.st["comment_scans"])

    def test_a_carried_row_is_byte_stable(self):
        page = self.seed(self.enriched(stored_bullet(C1, "stored comment")))
        first = self.upsert(page)
        again = self.upsert(page)
        self.assertEqual(first, again)


class ContentReWalk(MirrorSandbox):
    """A re-walked content page reads its comments only if it was never scanned."""

    def run_content(self, api, st):
        refresh.phase_content(api, self.users, st, refresh.new_report("daily"),
                              types.SimpleNamespace(dry_run=False), "daily", set())

    def api(self):
        return FakeNotion(search=[page_obj(PAGE, "Page", le="2026-09-24T00:00:00.000Z")],
                          children={PAGE: [paragraph(BLOCK, "edited")]},
                          comments={BLOCK: [comment(C1, "on a block")]})

    def test_an_edited_page_that_was_scanned_before_is_not_rescanned(self):
        self.write_page("", PAGE, "Page")
        self.write_meta(page_obj(PAGE, "Page", le="2026-09-01T00:00:00.000Z"))
        api = self.api()
        self.run_content(api, self.state(comment_scans={PAGE: "2026-09-20T00:00:00.000Z"}))
        self.assertEqual(api.walked(), [PAGE])
        self.assertEqual(api.comment_calls(), [])

    def test_a_rewalk_stamps_the_walk_so_the_minute_edge_recheck_settles(self):
        """The recheck re-walks a page whose last_edited_time sits just before the
        moment it was last read. It used to key on the comment scan, which every
        walk set; walks no longer scan, so without their own stamp a page the
        audit read a minute after an edit would be re-walked every night."""
        self.write_page("", PAGE, "Page")
        edited, audited = "2026-09-20T10:00:00.000Z", "2026-09-20T10:01:00.000Z"
        self.write_meta(page_obj(PAGE, "Page", le=edited))
        st = self.state(comment_scans={PAGE: audited})
        unchanged = FakeNotion(search=[page_obj(PAGE, "Page", le=edited)],
                               children={PAGE: [paragraph(BLOCK, "body")]})
        self.run_content(unchanged, st)
        self.assertEqual(unchanged.walked(), [PAGE], "the recheck fires once")
        again = FakeNotion(search=[page_obj(PAGE, "Page", le=edited)],
                           children={PAGE: [paragraph(BLOCK, "body")]})
        self.run_content(again, st)
        self.assertEqual(again.walked(), [], "and settles")

    def test_a_new_page_is_scanned_while_its_blocks_are_in_hand(self):
        api = self.api()
        st = self.state()
        self.run_content(api, st)
        self.assertEqual(sorted(api.comment_calls()), sorted([PAGE, BLOCK]))
        self.assertIn(PAGE, st["comment_scans"])
        self.assertIn("on a block", self.read(os.path.join(self.ws, "_comments.md")))


if __name__ == "__main__":
    unittest.main()
