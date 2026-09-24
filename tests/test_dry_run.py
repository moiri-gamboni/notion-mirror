"""A dry run writes nothing but its own report, and reads what a real run reads.

It used to write: the attachment download, a new page's folder, changed
`_schema.json`/`_schema.md`/`_ALL-SCHEMAS.md`, the consumed webhook DB events and
users.json all ran below any `args.dry_run` check. And it skipped every row
probe, so its request count could not estimate a real night. This runs the whole
nightly through `main()` against a fake Notion and a temp mirror that exercises
each of those paths, and diffs the tree.
"""
import csv
import hashlib
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh  # noqa: E402
from fake_notion import FakeNotion, MirrorSandbox, comment, page_obj, paragraph, rt  # noqa: E402

DB, ROW, PAGE, NEWPAGE, BLOCK = "c3" * 16, "a1" * 16, "f8" * 16, "e9" * 16, "b7" * 16
OLD, NEW = "2026-09-01T00:00:00.000Z", "2026-09-24T00:00:00.000Z"


def snapshot(root):
    out = {}
    for dirpath, dirs, files in os.walk(root):
        out[os.path.relpath(dirpath, root) + "/"] = None
        for f in files:
            p = os.path.join(dirpath, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = hashlib.sha256(fh.read()).hexdigest()
    return out


class DryRunWritesNothing(MirrorSandbox):
    def setUp(self):
        super().setUp()
        dbdir = os.path.join(self.dbs, f"Tasks {DB}")
        os.makedirs(dbdir)
        db_obj = {"object": "database", "id": refresh.dashed(DB), "title": rt("Tasks"),
                  "properties": {"Name": {"id": "title", "type": "title", "title": {}}}}
        refresh.jsave(os.path.join(dbdir, "_schema.json"),
                      {"id": DB, "title": "Tasks", "database": {"properties": {}},
                       "data_sources": []})  # stale: a real run would rewrite it
        props = {"Name": {"id": "title", "type": "title", "title": rt("Row")}}
        row = page_obj(ROW, "Row", "database_id", DB, le=NEW, db_props=props)
        with open(os.path.join(dbdir, f"Tasks {DB}.csv"), "w", newline="") as f:
            csv.writer(f).writerows([["_row_id", "Name"], [ROW, "Row"]])
        enr = "\n\n" + "\n".join(refresh.body_section_lines("old body")) + "\n"
        with open(os.path.join(dbdir, f"Row {ROW}.md"), "w") as f:
            f.write(refresh.render_row_md(row, DB, "Tasks", ["Name"], self.users, enr))
        refresh.jsave(os.path.join(self.state_dir, "rows-last-edited.json"), {DB: {ROW: OLD}})
        self.write_page("", PAGE, "Page")
        self.write_meta(page_obj(PAGE, "Page", le=OLD))
        refresh.jsave(os.path.join(self.state_dir, "webhook-db-events.json"),
                      {"dd" * 16: {"type": "database.created", "parent": "", "parent_type": ""}})
        with open(os.path.join(self.state_dir, "webhook-comments-capture.jsonl"), "w") as f:
            f.write(json.dumps({"captured_at": "2026-09-23T00:00:00+00:00", "page_id": ROW,
                                "anchor": "(page-level)", "comments": [
                                    {"id": refresh.dashed("d1" * 16), "rich_text": rt("captured"),
                                     "author_id": "uu" * 16,
                                     "created_time": "2026-09-22T00:00:00.000Z",
                                     "discussion_id": refresh.dashed("d1" * 16)}]}) + "\n")
        image = {"object": "block", "id": refresh.dashed("99" * 16), "type": "image",
                 "has_children": False,
                 "image": {"type": "file", "file": {"url": "https://files.invalid/x.png"},
                           "caption": []}}
        self.fake = FakeNotion(
            search=[page_obj(PAGE, "Page", le=NEW),
                    page_obj(NEWPAGE, "New", "page_id", PAGE, le=NEW)],
            pages={ROW: row},
            children={ROW: [paragraph(BLOCK, "new row body"), image],
                      PAGE: [paragraph("c1" * 16, "edited page")],
                      NEWPAGE: [image]},
            comments={BLOCK: [comment("d2" * 16, "on a row block")]},
            dbs={DB: (db_obj, [row])})
        for k, v in (("NOTION_TOKEN", "t"), ("NOTION_MIRROR_LOCK", os.path.join(self.root, "lock"))):
            p = mock.patch.dict(os.environ, {k: v})
            p.start()
            self.addCleanup(p.stop)
        for name, value in (("Api", lambda token, rps: self.fake),
                            ("Users", lambda api: self.users)):
            p = mock.patch.object(refresh, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(setattr, refresh, "DRY_RUN", False)
        urlopen = mock.patch.object(refresh.urllib.request, "urlopen",
                                    side_effect=AssertionError("a dry run downloaded a file"))
        urlopen.start()
        self.addCleanup(urlopen.stop)

    def run_main(self, *argv):
        with mock.patch.object(sys, "argv", ["refresh.py", *argv]):
            return refresh.main()

    def test_only_the_dry_run_report_is_written(self):
        before = snapshot(self.root)
        self.assertEqual(self.run_main("--mode", "daily", "--dry-run"), 0)
        after = snapshot(self.root)
        changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        self.assertEqual(changed, ["_meta/state/last-run-report.dry-run.json",
                                   "_meta/state/last-run-report.dry-run.md"])

    def test_it_still_makes_the_reads_a_real_run_would(self):
        self.run_main("--mode", "daily", "--dry-run")
        report = refresh.jload(os.path.join(self.state_dir, "last-run-report.dry-run.json"), {})
        self.assertIn(ROW, self.fake.walked(), "the changed row's body probe was skipped")
        self.assertIn(NEWPAGE, self.fake.walked())
        self.assertEqual(report["comments"]["folded"]["added"], 1)
        self.assertEqual(report["requests"], sum(report["requests_by_endpoint"].values()))
        self.assertGreater(report["requests_by_endpoint"]["blocks/children"], 0)


if __name__ == "__main__":
    unittest.main()
