"""The rolling per-block comment audit is sized by count, not by requests.

It is the only remaining per-block comment read for pages and rows the mirror
already knows, and it exists for what no event reports: resolving a thread.
Every human content page is re-read once per NOTION_REFRESH_PAGE_AUDIT_DAYS
(default 3) and every comment-bearing row once per NOTION_REFRESH_ROW_AUDIT_DAYS
(default 4), longest-unscanned first. A request budget used to decide how many
pages got read — on 2026-09-23 it read none, after five priority pages spent it.
"""
import csv
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh  # noqa: E402
from fake_notion import FakeNotion, MirrorSandbox, comment, page_obj, rt  # noqa: E402

DB = "c3" * 16


def pid(n):
    return f"{n:02x}" * 16


class PageAudit(MirrorSandbox):
    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {"NOTION_REFRESH_PAGE_AUDIT_DAYS": "3"})
        p.start()
        self.addCleanup(p.stop)
        self.pages = [page_obj(pid(n), f"P{n}") for n in range(1, 8)]
        for p in self.pages:
            self.write_page("", refresh.undash(p["id"]), p["properties"]["title"]["title"][0]["plain_text"])
        self.auto = page_obj(pid(9), "Log")
        self.write_page("Hub/Auto", pid(9), "Log")
        self.write_meta(*self.pages, self.auto)
        # oldest first: P1 never scanned, then P2..P7 in date order
        self.scans = {pid(n): f"2026-09-{n:02d}T00:00:00.000Z" for n in range(2, 8)}
        self.api = FakeNotion(comments={pid(3): [comment("d1" * 16, "an open comment")]})
        self.st = self.state(comment_scans=dict(self.scans))
        self.report = refresh.new_report("daily")

    def run_audit(self):
        meta, _ = refresh.load_meta_jsonl()
        refresh.phase_comment_audit_pages(self.api, self.users, meta, self.st, self.report,
                                          types.SimpleNamespace(dry_run=False))

    def test_a_third_of_the_human_pages_longest_unscanned_first(self):
        self.run_audit()
        self.assertEqual(sorted(set(self.api.walked())), [pid(1), pid(2), pid(3)])
        self.assertNotIn(pid(9), self.api.walked(), "automation pages are never audited")
        self.assertEqual(self.report["comments"]["audit"]["pages_scanned"], 3)
        self.assertEqual(self.report["comments"]["audit"]["pages_total"], 7)
        for n in (1, 2, 3):
            self.assertGreater(self.st["comment_scans"][pid(n)], self.scans.get(pid(4)))
        self.assertIn("an open comment", self.read(os.path.join(self.ws, "_comments.md")))

    def test_three_nights_cover_every_page(self):
        seen = set()
        for _ in range(3):
            self.api = FakeNotion()
            self.run_audit()
            seen |= set(self.api.walked())
        self.assertEqual(seen, {pid(n) for n in range(1, 8)})

    def test_the_cycle_length_is_configurable(self):
        with mock.patch.dict(os.environ, {"NOTION_REFRESH_PAGE_AUDIT_DAYS": "7"}):
            self.run_audit()
        self.assertEqual(self.api.walked(), [pid(1)])

    def test_the_default_cycle_is_a_monthly_backstop(self):
        with mock.patch.dict(os.environ, {"NOTION_REFRESH_PAGE_AUDIT_DAYS": ""}):
            self.run_audit()
        self.assertEqual(self.report["comments"]["audit"]["page_cycle_days"], 30)
        self.assertEqual(self.api.walked(), [pid(1)])  # ceil(7 / 30) = 1

    def test_pages_holding_id_less_comments_go_first(self):
        legacy = "- **on** \"(page-level)\" — Someone (2024-01-01): old <!-- notion:cid legacy -->"
        refresh.update_comments_md({pid(7): {"title": "P7", "bullets": [legacy]}},
                                   refresh.new_report("x"), merge=False)
        with mock.patch.dict(os.environ, {"NOTION_REFRESH_PAGE_AUDIT_DAYS": "7"}):
            self.run_audit()
        self.assertEqual(self.api.walked(), [pid(7), pid(1)], "the legacy page, then the share")
        self.assertEqual(self.report["comments"]["audit"]["legacy_pages_scanned"], 1)

    def test_a_nonsense_cycle_length_refuses(self):
        with mock.patch.dict(os.environ, {"NOTION_REFRESH_PAGE_AUDIT_DAYS": "0"}):
            with self.assertRaises(ValueError):
                self.run_audit()


class RowAudit(MirrorSandbox):
    def setUp(self):
        super().setUp()
        p = mock.patch.dict(os.environ, {"NOTION_REFRESH_ROW_AUDIT_DAYS": "4"})
        p.start()
        self.addCleanup(p.stop)
        self.dbdir = os.path.join(self.dbs, f"Tasks {DB}")
        os.makedirs(self.dbdir)
        refresh.jsave(os.path.join(self.dbdir, "_schema.json"),
                      {"id": DB, "title": "Tasks", "database": {"properties": {}}})
        self.rows = [pid(n) for n in range(1, 6)]
        with open(os.path.join(self.dbdir, f"Tasks {DB}.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["_row_id", "Name"])
            for r in self.rows:
                w.writerow([r, f"Row {r[:2]}"])
        pages = {}
        for r in self.rows:
            p = page_obj(r, f"Row {r[:2]}", "database_id", DB, db_props={
                "Name": {"id": "title", "type": "title", "title": rt(f"Row {r[:2]}")}})
            pages[r] = p
            bullets = [refresh.stamp_cid("- _Someone (2026-09-01):_ old", "d9" * 16)]
            enr = "\n\n" + "\n".join(refresh.comments_section_lines(bullets)) + "\n"
            with open(os.path.join(self.dbdir, f"Row {r[:2]} {r}.md"), "w") as f:
                f.write(refresh.render_row_md(p, DB, "Tasks", ["Name"], self.users, enr))
        self.api = FakeNotion(pages=pages, comments={pid(1): [comment("d8" * 16, "fresh")]})
        pool = {r: f"2026-09-{n:02d}T00:00:00.000Z" for n, r in enumerate(self.rows, 10)}
        pool[pid(1)] = ""
        pool["ee" * 16] = "2026-09-01T00:00:00.000Z"  # a row deleted since it joined
        self.st = self.state(rows={DB: {r: "x" for r in self.rows}}, comment_rows=pool)
        self.report = refresh.new_report("daily")

    def run_audit(self):
        refresh.phase_comment_audit_rows(self.api, self.users, self.st, self.report,
                                         types.SimpleNamespace(dry_run=False), set())

    def test_a_quarter_of_the_pool_is_read_per_block(self):
        self.run_audit()
        self.assertNotIn("ee" * 16, self.st["comment_rows"], "deleted rows leave the pool")
        audited = sorted({c for c in self.api.comment_calls()})
        self.assertEqual(audited, [pid(1), pid(2)])  # ceil(5 / 4) = 2, oldest first
        self.assertEqual(self.report["comments"]["audit"]["rows_scanned"], 2)
        self.assertIn(pid(1), self.st["comment_scans"])
        path = os.path.join(self.dbdir, f"Row 01 {pid(1)}.md")
        body = refresh.extract_comments_body(self.read(path).split(refresh.MARKER, 1)[1])
        self.assertIn("fresh", body)
        self.assertIn("resolved/deleted", body, "the stored comment the API no longer lists")

    def test_the_row_cycle_is_configurable(self):
        with mock.patch.dict(os.environ, {"NOTION_REFRESH_ROW_AUDIT_DAYS": "1"}):
            self.run_audit()
        self.assertEqual(self.report["comments"]["audit"]["rows_scanned"], 5)


if __name__ == "__main__":
    unittest.main()
