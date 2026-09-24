"""The automation subtrees are a scope rule, not a request cap.

A page under `NOTION_MIRROR_AUTOMATION_SUBTREES` is captured at first sight and
then never re-walked and never comment-scanned: bot delta logs change every day
and carry no human signal, and on 2026-07-20 thirteen of them re-walked nightly
cost ~14k requests. The per-page request cap that followed only turned that into
a truncated render repeated every night; excluding them from the walk is the
same protection with nothing left to tune.
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh  # noqa: E402
from fake_notion import FakeNotion, MirrorSandbox, page_obj, paragraph  # noqa: E402

HUMAN, AUTO, NEWAUTO = "11" * 16, "22" * 16, "33" * 16
OLD, NEW = "2026-09-01T00:00:00.000Z", "2026-09-24T00:00:00.000Z"


class AutomationScope(MirrorSandbox):
    def setUp(self):
        super().setUp()
        self.human_path = self.write_page("", HUMAN, "Human")
        self.auto_path = self.write_page("Hub/Auto", AUTO, "Log", body="stored log body")
        self.write_meta(page_obj(HUMAN, "Human", le=OLD), page_obj(AUTO, "Log", le=OLD))
        self.args = types.SimpleNamespace(dry_run=False)
        self.report = refresh.new_report("daily")

    def run_content(self, api):
        refresh.phase_content(api, self.users, self.state(), self.report, self.args,
                              "daily", set())

    def test_a_changed_automation_page_is_not_walked_or_scanned(self):
        api = FakeNotion(search=[page_obj(HUMAN, "Human", le=NEW), page_obj(AUTO, "Log", le=NEW)],
                         children={HUMAN: [paragraph("44" * 16, "fresh")],
                                   AUTO: [paragraph("55" * 16, "never fetched")]})
        self.run_content(api)
        self.assertIn(HUMAN, api.walked())
        self.assertNotIn(AUTO, api.walked())
        self.assertNotIn(AUTO, api.comment_calls())
        self.assertIn("stored log body", self.read(self.auto_path))
        self.assertEqual(self.report["pages"]["automation_skipped"], 1)

    def test_a_new_automation_page_is_captured_once_but_not_comment_scanned(self):
        api = FakeNotion(search=[page_obj(NEWAUTO, "Recording", "page_id", AUTO, le=NEW)],
                         children={NEWAUTO: [paragraph("66" * 16, "first capture")]})
        self.run_content(api)
        self.assertEqual(api.walked(), [NEWAUTO])
        self.assertEqual(api.comment_calls(), [])
        path = os.path.join(self.ws, "Hub", "Auto", f"Log {AUTO}", f"Recording {NEWAUTO}.md")
        self.assertIn("first capture", self.read(path))


if __name__ == "__main__":
    unittest.main()
