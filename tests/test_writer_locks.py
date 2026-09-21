"""Every standalone mirror writer takes `~/.locks/notion-mirror-internal`.

`refresh.py` (the engine), `coverage_backfill.py` and the `migrate_*` scripts
already did. `merge_backfill.py` and `repair_backfill_attribution.py` did not:
both rewrite row `.md` files and `_comments.md` directly, and both are run by
hand — which is exactly how the 2026-08-13 truncation happened, a manual writer
starting while a scheduled one was mid-render. These pin that the two of them
now refuse rather than interleave.

Nothing here touches the real lock file: every test redirects
`NOTION_MIRROR_LOCK` into its own temp directory, one path per test. That
matters more than usual here, because `take_lock` holds the lock on an fd for
the life of the process — two tests sharing a path would have the second one
contend with the first one's own acquisition rather than with the holder it
planted.
"""
import fcntl
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TOOLS)


def _load(name, filename):
    """Load a `_tools` script the way its siblings do — by path, not by import —
    so this file does not depend on `_tools` being a package."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(TOOLS, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class WriterLockCase(unittest.TestCase):
    """A private lock path per test, and a helper to hold it like another writer."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="writer-lock-")
        self.lock = os.path.join(self.tmp, "mirror.lock")
        os.environ["NOTION_MIRROR_LOCK"] = self.lock
        self.addCleanup(os.environ.pop, "NOTION_MIRROR_LOCK", None)

    def hold_the_lock(self):
        fh = open(self.lock, "w")
        self.addCleanup(fh.close)
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh

    def assert_lock_is_held(self):
        """Somebody in this process holds it — the writer under test, after a run
        that was allowed to proceed."""
        fh = open(self.lock, "w")
        try:
            with self.assertRaises(OSError):
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            fh.close()

    def run_main(self, fn, *args):
        """`fn` with stdout and stderr captured; returns (exit code, stderr)."""
        err, out = io.StringIO(), io.StringIO()
        code = 0
        try:
            with redirect_stderr(err), redirect_stdout(out):
                fn(*args)
        except SystemExit as e:
            code = e.code or 0
        return code, err.getvalue()


class MergeBackfillTest(WriterLockCase):
    """`merge_backfill.py` has no dry run: reaching `main()` means writing."""

    def setUp(self):
        super().setUp()
        self.mb = _load("merge_backfill_locktest", "merge_backfill.py")
        # an absent capture file is the "nothing to do" path, which still has to
        # take the lock first — a writer that locks only once it has found work
        # is a writer that races on the way to finding it
        self.mb.CAP = os.path.join(self.tmp, "no-captures.jsonl")

    def test_it_refuses_while_another_writer_holds_the_lock(self):
        self.hold_the_lock()
        code, err = self.run_main(self.mb.main)
        self.assertEqual(code, 1)
        self.assertIn(self.lock, err)
        self.assertIn("refusing to write the mirror", err)

    def test_it_runs_and_holds_the_lock_when_nobody_else_does(self):
        code, err = self.run_main(self.mb.main)
        self.assertEqual((code, err), (0, ""))
        self.assert_lock_is_held()


class RepairAttributionTest(WriterLockCase):
    """`repair_backfill_attribution.py` locks on `--apply` only: a dry run reads,
    and taking the lock to read would refuse a refresh for the length of a
    report. Same split as `coverage_backfill.py` and `migrate_comment_ids.py`."""

    def setUp(self):
        super().setUp()
        try:
            self.rba = _load("repair_backfill_locktest", "repair_backfill_attribution.py")
        except (OSError, KeyError) as e:
            # It loads `backfill_resolved_comments.py`, which reads token_v2 and
            # backfill-ctx.json at import. Those are operator credentials, absent
            # on any machine that is not the one this script runs from.
            raise unittest.SkipTest(f"backfill credentials not present: {e}")
        self.captures = os.path.join(self.tmp, "captures.jsonl")
        with open(self.captures, "w") as f:
            f.write('{"captured_at": "backfill", "page_id": "%s", "comments": []}\n' % ("a" * 32))
        self.rba.CAP = self.captures

    def test_apply_refuses_while_another_writer_holds_the_lock(self):
        self.hold_the_lock()
        sys.argv = ["repair_backfill_attribution.py", "--apply"]
        code, err = self.run_main(self.rba.main)
        self.assertEqual(code, 1)
        self.assertIn(self.lock, err)
        self.assertIn("refusing to write the mirror", err)

    def test_a_dry_run_does_not_contend(self):
        self.hold_the_lock()
        sys.argv = ["repair_backfill_attribution.py"]
        code, err = self.run_main(self.rba.main)
        self.assertEqual((code, err), (0, ""))

    def test_apply_takes_the_lock_when_it_is_free(self):
        sys.argv = ["repair_backfill_attribution.py", "--apply"]
        code, err = self.run_main(self.rba.main)
        self.assertEqual((code, err), (0, ""))
        self.assert_lock_is_held()


if __name__ == "__main__":
    unittest.main()
