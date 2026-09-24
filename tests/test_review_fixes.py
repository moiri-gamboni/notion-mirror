"""Edge cases found in review of the comment rework.

* A row body may contain its own `## Comments` heading. In a delimited file with
  no comments region yet, the heading fallback used to read body lines as
  comments and cut the body there when the region was rewritten.
* A comment the API stopped listing keeps the date it was first found gone; a
  re-scan that re-derives it from the capture log must not re-stamp it today.
* Deletion verification deletes a page only on a verdict (404), never on an
  outage.
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh  # noqa: E402
from fake_notion import FakeNotion, FakeUsers, MirrorSandbox, page_obj  # noqa: E402

C1, C2 = "d1" * 16, "d2" * 16


def bullet(cid, text):
    return refresh.stamp_cid(f"- _Someone (2026-09-01):_ {text}", cid, cid)


class BodyWithACommentsHeading(unittest.TestCase):
    BODY = "- intro\n## Comments\n- _Not (2026-01-01):_ a body line\n- more body"

    def enrichment(self):
        return "\n\n" + "\n".join(refresh.body_section_lines(self.BODY)) + "\n"

    def test_the_stored_comments_of_such_a_row_are_none(self):
        self.assertEqual(refresh.stored_comments_body(self.enrichment()), "")

    def test_adding_the_first_comment_keeps_the_whole_body(self):
        out = refresh.with_comments(self.enrichment(), [bullet(C1, "first")])
        self.assertIn(self.BODY, out)
        self.assertIn(refresh.BODY_CLOSE, out)
        self.assertEqual(refresh.split_bullets(refresh.extract_comments_body(out), prefix="- _"),
                         [bullet(C1, "first")])


class ResolvedDateIsSticky(unittest.TestCase):
    def test_a_rescan_keeps_the_first_resolved_date(self):
        stored = refresh.annotate_resolved(bullet(C1, "gone"), "2026-07-20")
        # union_captured re-adds the captured comment the scan no longer lists,
        # stamped today
        rescanned = [bullet(C2, "still open"), refresh.annotate_resolved(bullet(C1, "gone"), "2026-09-24")]
        merged, newly = refresh.merge_comment_bullets(stored, rescanned, "2026-09-24", prefix="- _")
        self.assertIn(stored, merged)
        self.assertEqual(len(merged), 2)
        self.assertEqual(newly, 0)


class DeletionNeedsAVerdict(MirrorSandbox):
    PAGE = "f8" * 16

    def run_content(self, code):
        path = self.write_page("", self.PAGE, "Page")
        self.write_meta(page_obj(self.PAGE, "Page"))
        api = FakeNotion(search=[])

        def get(p, params=None, ver=None):
            api.n += 1
            raise refresh.ApiError(code, "boom")
        api.get = get
        report = refresh.new_report("daily")
        refresh.phase_content(api, FakeUsers(), self.state(), report,
                              types.SimpleNamespace(dry_run=False), "daily", set())
        return path, report

    def test_an_outage_deletes_nothing(self):
        path, report = self.run_content(0)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(report["pages"]["deleted"], [])

    def test_a_404_still_deletes(self):
        path, report = self.run_content(404)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(len(report["pages"]["deleted"]), 1)


if __name__ == "__main__":
    unittest.main()
