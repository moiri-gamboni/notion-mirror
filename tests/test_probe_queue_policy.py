"""Which queued rows `phase_queue` actually probes.

The enriched-only economy exists for feed databases: in a DB where under 5% of
rows carry a body or comments, probing every changed row costs a body+comment
walk the nightly sweep would have skipped (80k AIS jobs: 1,573 queued against
1,906 skipped on one run). Applying it to *every* queue entry, though, closes the
only door a webhook-captured comment has.

No network: the API stub answers one GET and `upsert_row_md` is replaced by a
recorder, so what each test reads is the probe decision itself.
"""
import json
import os
import shutil
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import refresh  # noqa: E402  (_tools is not a package; discover's top dir is tests/)

DB_ID = "bb" * 16
ROW_ID = "aa" * 16
DIRNAME = f"Feed DB {DB_ID}"


class StubApi:
    def __init__(self):
        self.n = 0

    def get(self, path):
        self.n += 1
        return {"id": path.rsplit("/", 1)[-1],
                "properties": {"Name": {"type": "title", "title": [{"plain_text": "Row"}]}},
                "last_edited_time": "2026-08-01T00:00:00.000Z"}


class QueuePolicyCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="probe-queue-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dbs = os.path.join(self.tmp, "_databases")
        self.state_dir = os.path.join(self.tmp, "state")
        self.dirpath = os.path.join(self.dbs, DIRNAME)
        os.makedirs(self.dirpath)
        os.makedirs(self.state_dir)
        for name, value in (("DBS", self.dbs), ("STATE", self.state_dir)):
            self.addCleanup(setattr, refresh, name, getattr(refresh, name))
            setattr(refresh, name, value)
        for name in ("db_dirs", "upsert_row_md", "expand_truncated_props"):
            self.addCleanup(setattr, refresh, name, getattr(refresh, name))
        self.addCleanup(setattr, refresh, "_WEBHOOK_CAPTURES", None)
        refresh._WEBHOOK_CAPTURES = None
        refresh.db_dirs = lambda: {DB_ID: DIRNAME}
        refresh.expand_truncated_props = lambda *a, **kw: None
        self.probed = []
        refresh.upsert_row_md = lambda api, users, page, dbid, title, cols, dirpath, probe, \
            *a, **kw: self.probed.append(probe)
        self.write_row(enriched=False)
        with open(os.path.join(self.dirpath, f"Feed DB {DB_ID}.csv"), "w") as f:
            f.write("_row_id,Name\n")
        self.report = refresh.new_report("daily")
        self.args = types.SimpleNamespace(dry_run=False)

    def write_row(self, enriched):
        page = {"id": refresh.dashed(ROW_ID),
                "properties": {"Name": {"type": "title", "title": [{"plain_text": "Row"}]}},
                "last_edited_time": "2026-08-01T00:00:00.000Z"}
        body = "\n\n## Comments\n\n- _A (2026-01-01):_ hi\n" if enriched else ""
        with open(os.path.join(self.dirpath, f"Row {ROW_ID}.md"), "w") as f:
            f.write(refresh.render_row_md(page, DB_ID, "Feed DB", ["Name"], None, body))

    def capture(self, page_id):
        """A comment thread the receiver captured for that row."""
        with open(os.path.join(self.state_dir, "webhook-comments-capture.jsonl"), "a") as f:
            f.write(json.dumps({"page_id": page_id, "anchor": "(page-level)",
                                "comments": [{"id": "c1", "text": "a captured comment",
                                              "created_time": "2026-08-07T00:00:00.000Z"}]}) + "\n")

    def drain(self, mode="enriched"):
        state = {"queue": [{"kind": "row_probe", "db": DB_ID, "row": ROW_ID}],
                 "rows": {DB_ID: {ROW_ID: ""}},
                 "probe_policy": {DB_ID: {"mode": mode, "ts": refresh.now_iso()}},
                 "comment_rows": {}}
        refresh.phase_queue(StubApi(), None, state, self.report, self.args)
        return state


class CapturedCommentTest(QueuePolicyCase):
    """A row the receiver captured a comment for must be probed even in a
    sparse-enrichment DB.

    `union_captured` runs only inside `probe_row`; the comment-audit pool seeds
    only from rows where `has_comments` already holds; and the full comment sweep
    is content-pages-only. So for a row with no stored enrichment the probe is
    the single path by which that capture ever reaches the mirror — and the
    policy verdict is cached 7 days while the DB stays under the threshold
    precisely because nothing is enriched. Silent and self-latching."""

    def test_a_row_with_a_capture_is_probed(self):
        self.capture(ROW_ID)
        self.drain()
        self.assertEqual(self.probed, [True])

    def test_it_is_not_counted_as_a_policy_skip(self):
        self.capture(ROW_ID)
        self.drain()
        self.assertEqual([n for n in self.report["notes"] if "enriched-only policy" in n], [])

    def test_a_capture_on_a_different_row_does_not_force_this_one(self):
        self.capture("cc" * 16)
        self.drain()
        self.assertEqual(self.probed, [False])


class EconomyPreservedTest(QueuePolicyCase):
    """What the enriched-only rule is for, kept intact — the fix must not turn
    feed churn back into body walks."""

    def test_an_unenriched_row_with_no_capture_is_not_probed(self):
        self.drain()
        self.assertEqual(self.probed, [False])

    def test_the_skip_is_reported(self):
        self.drain()
        self.assertTrue(any("enriched-only policy" in n for n in self.report["notes"]))

    def test_an_enriched_row_is_probed(self):
        self.write_row(enriched=True)
        self.drain()
        self.assertEqual(self.probed, [True])

    def test_every_row_is_probed_when_the_db_is_not_sparse(self):
        self.drain(mode="all")
        self.assertEqual(self.probed, [True])


if __name__ == "__main__":
    unittest.main()
