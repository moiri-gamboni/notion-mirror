"""Every open thread is re-listed where it lives, every night.

Notion fires no webhook when a thread is resolved, and `GET /comments?block_id=`
lists only open comments, so a resolution shows up only as absence from a fresh
listing of the thread's block. It can only happen to a thread the mirror holds
open, so the nightly lists exactly those blocks — one request per block, plus
one `GET /comments/{id}` the first time a thread's block is unknown — instead of
re-reading every block of a third of the workspace.
"""
import json
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh  # noqa: E402
from fake_notion import FakeNotion, MirrorSandbox, comment, page_obj  # noqa: E402

PAGE, ROW, DB, BLOCK = "a1" * 16, "b2" * 16, "c3" * 16, "e7" * 16
A1, A2, B1, C1, D1, R1, N1, Z1 = ("d1" * 16, "d2" * 16, "d3" * 16, "d4" * 16, "d5" * 16,
                                  "d6" * 16, "d7" * 16, "d8" * 16)


def page_bullet(cid, did, text, anchor="(page-level)"):
    return refresh.stamp_cid(f'- **on** "{anchor}" — Someone (2026-09-01): {text}', cid, did)


def row_bullet(cid, text):
    return refresh.stamp_cid(f"- _Someone (2026-09-01):_ {text}", cid, cid)


class ResolutionCheck(MirrorSandbox):
    def setUp(self):
        super().setUp()
        self.write_meta(page_obj(PAGE, "A page"))
        refresh.update_comments_md({PAGE: {"title": "A page", "bullets": [
            page_bullet(A1, A1, "first in thread A", "a block"),
            page_bullet(A2, A1, "reply in thread A", "a block"),
            page_bullet(B1, B1, "page-level thread B"),
            page_bullet(D1, D1, "deleted since"),
            refresh.annotate_resolved(page_bullet(Z1, Z1, "reopened later", "a block"), "2026-09-10"),
            "- **on** \"x\" — Someone (2024-01-01): legacy <!-- notion:cid legacy -->",
        ]}}, refresh.new_report("x"), merge=False)
        dbdir = os.path.join(self.dbs, f"Tasks {DB}")
        os.makedirs(dbdir)
        self.row_path = os.path.join(dbdir, f"Row {ROW}.md")
        enr = "\n\n" + "\n".join(refresh.body_section_lines("row body")
                                 + refresh.comments_section_lines([row_bullet(R1, "on the row")])) + "\n"
        with open(self.row_path, "w") as f:
            f.write("<!-- row -->\n" + refresh.MARKER + enr)
        # thread A's block is known from the capture log; B, Z, R are looked up
        with open(os.path.join(self.state_dir, "webhook-comments-capture.jsonl"), "w") as f:
            f.write(json.dumps({"captured_at": "2026-09-01T00:00:00+00:00", "page_id": PAGE,
                                "entity_id": BLOCK, "entity_type": "block", "anchor": "a block",
                                "comments": [{"id": refresh.dashed(A1),
                                              "discussion_id": refresh.dashed(A1)}]}) + "\n")
        self.api = FakeNotion(
            comment_parents={B1: ("page_id", PAGE), R1: ("page_id", ROW)},
            comments={BLOCK: [comment(A1, "first in thread A"), comment(N1, "missed by the webhook")],
                      PAGE: [comment(B1, "page-level thread B")]})
        self.st = self.state()
        self.report = refresh.new_report("daily")
        self.args = types.SimpleNamespace(dry_run=False)

    def run_check(self):
        refresh.phase_resolution_check(self.api, self.users, self.st, self.report, self.args)
        return self.report["comments"]["resolution"]

    def page_bullets(self):
        return refresh.split_bullets(next(s["body"] for s in refresh.load_comments_md()[1]
                                          if s["id"] == PAGE))

    def row_bullets(self):
        txt = self.read(self.row_path)
        return refresh.split_bullets(refresh.extract_comments_body(txt.split(refresh.MARKER, 1)[1]),
                                     prefix="- _")

    def by_cid(self, bullets):
        return {refresh.bullet_cid(b): b for b in bullets if refresh.bullet_cid(b)}

    def test_absent_comments_are_resolved_present_ones_stay_open(self):
        stats = self.run_check()
        got = self.by_cid(self.page_bullets())
        self.assertNotIn("resolved/deleted", got[A1])
        self.assertIn("resolved/deleted", got[A2], "listed block no longer returns the reply")
        self.assertNotIn("resolved/deleted", got[B1])
        self.assertIn("resolved/deleted", got[D1], "GET /comments/{id} answered 404")
        self.assertIn("resolved/deleted", self.by_cid(self.row_bullets())[R1])
        self.assertEqual(stats["resolved"], 3)

    def test_it_costs_one_listing_per_block_and_one_lookup_per_unknown_thread(self):
        stats = self.run_check()
        listings = sorted(self.api.comment_calls())
        self.assertEqual(listings, sorted([BLOCK, PAGE, ROW]))
        lookups = [c for c in self.api.calls if c.startswith("/comments/")]
        self.assertEqual(len(lookups), 3, "B, D and R: A came from the capture log")
        self.assertEqual(stats["from_captures"], 1)
        self.assertEqual(self.st["comment_parents"][B1], PAGE)

    def test_a_second_night_asks_no_lookups(self):
        self.run_check()
        self.api.calls.clear()
        self.run_check()
        self.assertEqual([c for c in self.api.calls if c.startswith("/comments/")], [])

    def test_a_comment_the_webhook_missed_is_added_with_its_blocks_anchor(self):
        stats = self.run_check()
        new = self.by_cid(self.page_bullets())[N1]
        self.assertIn('"a block"', new)
        self.assertIn("missed by the webhook", new)
        self.assertEqual(stats["added"], 1)

    def test_the_legacy_bullet_is_left_alone(self):
        self.run_check()
        self.assertTrue(any("notion:cid legacy" in b and "resolved/deleted" not in b
                            for b in self.page_bullets()))

    def test_a_block_that_is_gone_resolves_its_threads(self):
        self.api.gone_blocks = {BLOCK}
        self.run_check()
        got = self.by_cid(self.page_bullets())
        self.assertIn("resolved/deleted", got[A1])

    def test_a_dry_run_writes_nothing(self):
        before = (self.read(os.path.join(self.ws, "_comments.md")), self.read(self.row_path))
        self.args.dry_run = True
        stats = self.run_check()
        self.assertEqual(stats["resolved"], 3)
        self.assertEqual(before, (self.read(os.path.join(self.ws, "_comments.md")),
                                  self.read(self.row_path)))


class RateLimitReasons(unittest.TestCase):
    def test_reasons_are_counted_and_retry_after_is_read_from_the_body(self):
        import io
        import urllib.error
        from unittest import mock
        api = refresh.Api("t", 1000.0)
        body = json.dumps({"object": "error", "status": 429, "code": "rate_limited",
                           "additional_data": {"rate_limit_reason": "workspace",
                                               "retry_after": 2}}).encode()
        err = urllib.error.HTTPError("u", 429, "rl", {}, io.BytesIO(body))

        class Ok:
            def read(self):
                return b'{"ok": 1}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        served = [err, Ok()]
        slept = []
        with mock.patch.object(refresh.urllib.request, "urlopen", lambda r, timeout=None: (
                served.pop(0) if not isinstance(served[0], Exception) else (_ for _ in ()).throw(served.pop(0)))), \
                mock.patch.object(refresh.time, "sleep", slept.append):
            self.assertEqual(api.get("/users/me"), {"ok": 1})
        self.assertEqual(dict(api.limit_reasons), {"workspace": 1})
        self.assertIn(2.5, slept)


if __name__ == "__main__":
    unittest.main()
