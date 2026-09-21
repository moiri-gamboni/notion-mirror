"""`merge_backfill.py` is documented idempotent, and the mirror it merges into is
now cid-stamped. Its membership test therefore has to survive the
trailer: a bullet already on disk carrying ` <!-- notion:cid ... -->` is the same
comment as the trailer-free bullet the script renders from the capture, and
re-appending it would recreate the duplicate class the id migration ended.

Local only: the script makes no API calls, so these tests drive `main()` whole
over a temp mirror.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import merge_backfill as mb  # noqa: E402  (_tools is not a package; discover's top dir is tests/)

R = mb.R

DB_ID = "dd" * 16
ROW = "aa" * 16
PAGE = "bb" * 16
CID = "cc" * 16
AUTHOR = "uu" * 16
WHEN = "2026-07-01T09:00:00.000Z"


def capture(page_id, anchor="(page-level)", resolved=False):
    return {"captured_at": "backfill", "page_id": page_id, "anchor": anchor,
            "comments": [{"id": R.dashed(CID), "author_id": AUTHOR, "created_time": WHEN,
                          "text": "does this still block the send?", "resolved": resolved}]}


class Mirror(unittest.TestCase):
    """A temp mirror holding exactly one row file and one content page, each
    already carrying the captured comment as a cid-stamped bullet."""

    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        # `main()` takes the mirror lock now. Point it at this test's own tree:
        # taking the real one would refuse a live refresh for as long as the
        # suite runs, and a live refresh holding it would fail the suite.
        os.environ["NOTION_MIRROR_LOCK"] = os.path.join(self.ws, "mirror.lock")
        self.addCleanup(os.environ.pop, "NOTION_MIRROR_LOCK", None)
        self.dbs = os.path.join(self.ws, "_databases")
        self.state = os.path.join(self.ws, "_state")
        self.rowdir = os.path.join(self.dbs, f"Tasks {DB_ID}")
        os.makedirs(self.rowdir)
        os.makedirs(self.state)
        for attr, val in (("WS", self.ws), ("DBS", self.dbs), ("STATE", self.state)):
            patched = getattr(R, attr)
            setattr(R, attr, val)
            self.addCleanup(setattr, R, attr, patched)
        cap_path = os.path.join(self.state, "webhook-comments-capture.jsonl")
        patched_cap = mb.CAP
        mb.CAP = cap_path
        self.addCleanup(setattr, mb, "CAP", patched_cap)
        with open(os.path.join(self.state, "users.json"), "w") as f:
            json.dump({AUTHOR: "Sam Example"}, f)

        self.rowpath = os.path.join(self.rowdir, f"Row One {ROW}.md")
        self.pagepath = os.path.join(self.ws, f"Some Page {PAGE}.md")
        self.comments_md = os.path.join(self.ws, "_comments.md")

    def write_capture(self, *entries):
        with open(mb.CAP, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def write_row(self, bullet):
        text = ("# Row One\n\n" + R.MARKER + R.body_section("- a line")
                + R.comments_section([bullet]) + "\n")
        with open(self.rowpath, "w") as f:
            f.write(text)

    def write_page(self, bullet):
        with open(self.pagepath, "w") as f:
            f.write("# Some Page\n\nbody\n")
        with open(self.comments_md, "w") as f:
            f.write("# Notion comments (content pages)\n\n"
                    f"## Some Page  `{PAGE}`\n\n{bullet}\n\n")

    def run_merge(self):
        out = io.StringIO()
        with redirect_stdout(out):
            mb.main()
        return json.loads(out.getvalue().strip().split("\n")[-1])

    def test_a_stamped_row_bullet_is_recognised_as_already_merged(self):
        users = mb.NameCache()
        self.write_row(R.stamp_cid(mb.row_bullet(capture(ROW)["comments"][0], users), CID))
        self.write_capture(capture(ROW))
        before = open(self.rowpath).read()
        self.assertEqual(self.run_merge()["row_comments_added"], 0)
        self.assertEqual(open(self.rowpath).read(), before)

    def test_a_stamped_page_bullet_is_recognised_as_already_merged(self):
        users = mb.NameCache()
        entry = capture(PAGE, anchor="Next steps")
        self.write_page(R.stamp_cid(mb.page_bullet("Next steps", entry["comments"][0], users), CID))
        self.write_capture(entry)
        before = open(self.comments_md).read()
        self.assertEqual(self.run_merge()["page_comments_added"], 0)
        self.assertEqual(open(self.comments_md).read(), before)

    def test_an_unstamped_row_bullet_still_dedupes(self):
        """The pre-migration corpus is not all stamped, so the trailer-free key
        has to keep working — the fix widens the key, it does not move it."""
        users = mb.NameCache()
        self.write_row(mb.row_bullet(capture(ROW)["comments"][0], users))
        self.write_capture(capture(ROW))
        self.assertEqual(self.run_merge()["row_comments_added"], 0)

    def test_a_genuinely_new_comment_is_still_appended(self):
        users = mb.NameCache()
        self.write_row(R.stamp_cid(mb.row_bullet(capture(ROW)["comments"][0], users), CID))
        other = capture(ROW)
        other["comments"][0].update(id=R.dashed("ee" * 16), text="a second comment")
        self.write_capture(other)
        self.assertEqual(self.run_merge()["row_comments_added"], 1)


if __name__ == "__main__":
    unittest.main()
