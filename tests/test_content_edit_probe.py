"""An edited row body is read even in a database probed enriched-only.

A large database where almost no row carries a body is probed enriched-only: a changed
row is re-read only if its file already holds a body or comments, so property churn in
a feed costs nothing. A body written later into one of its rows was never read: the row
changed, its file held nothing, and the probe was skipped every night. The receiver
logs `page.content_updated` for exactly that edit, so a row with such an event newer
than the edit time the mirror last saw is probed whatever the policy.
"""
import json
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh  # noqa: E402
from fake_notion import FakeNotion, MirrorSandbox, page_obj, paragraph  # noqa: E402

DB, ROW = "c3" * 16, "b2" * 16
OLD, NEW = "2026-09-01T10:00:00.000Z", "2026-09-21T10:05:00.000Z"


class ContentEditProbe(MirrorSandbox):
    def setUp(self):
        super().setUp()
        refresh._CONTENT_EDITS = None
        self.addCleanup(setattr, refresh, "_CONTENT_EDITS", None)
        self.dirname = f"People {DB}"
        d = os.path.join(self.dbs, self.dirname)
        os.makedirs(d)
        with open(os.path.join(d, f"People {DB}.csv"), "w") as f:
            f.write("_row_id,title\n" + f"{ROW},Someone\n")
        page = page_obj(ROW, "Someone", "database_id", DB, le=OLD)
        self.row_path = os.path.join(d, f"Someone {ROW}.md")
        with open(self.row_path, "w") as f:
            f.write(refresh.render_row_md(page, DB, "People", ["title"], self.users, "\n"))
        self.st = self.state(rows={DB: {ROW: OLD}},
                             probe_policy={DB: {"mode": "enriched", "ts": "2999-01-01T00:00:00"}})
        self.api = FakeNotion(
            dbs={DB: ({"object": "database", "id": refresh.dashed(DB), "title": [], "properties": {}},
                      [page_obj(ROW, "Someone", "database_id", DB, le=NEW)])},
            children={ROW: [paragraph("44" * 16, "a body written later")]})
        self.report = refresh.new_report("daily")

    def events(self, *evs):
        with open(os.path.join(self.state_dir, "webhook-events.jsonl"), "w") as f:
            for typ, eid, ts in evs:
                f.write(json.dumps({"type": typ, "entity": {"id": refresh.dashed(eid), "type": "page"},
                                    "timestamp": ts}) + "\n")

    def sweep(self):
        refresh.refresh_db(self.api, self.users, DB, self.dirname, self.st, self.report,
                           types.SimpleNamespace(dry_run=False, dbs=None), set())
        return self.read(self.row_path)

    def test_without_a_content_event_the_policy_still_skips(self):
        self.events(("page.properties_updated", ROW, "2026-09-21T10:04:30.000Z"))
        self.assertNotIn("a body written later", self.sweep())

    def test_a_content_event_after_the_last_seen_edit_gets_the_body_read(self):
        self.events(("page.content_updated", ROW, "2026-09-21T10:04:30.000Z"))
        self.assertIn("a body written later", self.sweep())

    def test_a_content_event_the_mirror_had_already_seen_does_not(self):
        self.events(("page.content_updated", ROW, "2026-08-30T09:00:00.000Z"))
        self.assertNotIn("a body written later", self.sweep())


if __name__ == "__main__":
    unittest.main()
