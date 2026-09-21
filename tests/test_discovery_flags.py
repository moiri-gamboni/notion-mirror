"""What the nightly records about a database it could not capture, and what it
does with that record next run.

Both halves are permanent decisions taken from one API response, so the failure
mode is the same shape twice: a moment's error written down as a verdict about
the id, after which nothing ever looks again.

No network: the API is a stub that raises whatever the test names.
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import refresh  # noqa: E402  (_tools is not a package; discover's top dir is tests/)


def rid(n):
    return f"{n:032x}"


class RaisingApi:
    """An API whose every call fails the same way, counting the attempts."""

    def __init__(self, exc):
        self.exc = exc
        self.calls = 0

    def get(self, path):
        self.calls += 1
        raise self.exc


class CaptureNewDbTest(unittest.TestCase):
    """`capture_new_db` wrote `not_a_db` on *any* ApiError, so one 5xx retired a
    live database from the mirror for good: `phase_discovery` skips flagged ids,
    nothing re-probes them, and the coverage assert then reports the gap with a
    verdict attached that nobody checked."""

    def setUp(self):
        self.state = {"not_a_db": {}, "db404": {}}
        self.report = refresh.new_report("daily")
        self.args = types.SimpleNamespace(dry_run=False)

    def capture(self, exc):
        api = RaisingApi(exc)
        refresh.capture_new_db(api, None, rid(1), self.state, self.report, self.args)
        return api

    def test_a_404_is_a_verdict_about_the_id_and_is_flagged(self):
        self.capture(refresh.ApiError(404, "object_not_found"))
        self.assertIn(rid(1), self.state["not_a_db"])

    def test_a_linked_view_400_is_flagged_too(self):
        """The commonest real cause: Notion refuses a linked database view."""
        self.capture(refresh.ApiError(400, "validation_error: is a linked database"))
        self.assertIn(rid(1), self.state["not_a_db"])

    def test_a_403_is_flagged(self):
        self.capture(refresh.ApiError(403, "restricted_resource"))
        self.assertIn(rid(1), self.state["not_a_db"])

    def test_a_5xx_does_not_retire_the_database(self):
        """A 500 is a verdict about the moment. Flagging it means a live database
        disappears from the mirror on one bad night, permanently."""
        self.capture(refresh.ApiError(500, "internal_server_error"))
        self.assertEqual(self.state["not_a_db"], {})

    def test_a_5xx_is_reported_rather_than_swallowed(self):
        self.capture(refresh.ApiError(502, "bad_gateway"))
        errs = [e for e in self.report["dbs"]["errors"] if rid(1) in str(e)]
        self.assertEqual(len(errs), 1)
        self.assertIn("502", errs[0]["error"])

    def test_a_5xx_does_not_abort_the_run(self):
        """It must not propagate either: `phase_discovery` catches only Budget,
        so an escaping ApiError would unwind past main's try and take the whole
        night's uncommitted work with it."""
        self.capture(refresh.ApiError(503, "service_unavailable"))  # no raise = pass


class DiscoverySkipTest(unittest.TestCase):
    """Which ids `phase_discovery` spends a request on."""

    def setUp(self):
        self.report = refresh.new_report("daily")
        self.args = types.SimpleNamespace(dry_run=False)
        self.captured = []
        real = refresh.capture_new_db
        self.addCleanup(setattr, refresh, "capture_new_db", real)
        refresh.capture_new_db = lambda api, users, did, *a, **kw: self.captured.append(did)
        real_dirs = refresh.db_dirs
        self.addCleanup(setattr, refresh, "db_dirs", real_dirs)
        refresh.db_dirs = lambda: {}

    def discover(self, state, tags):
        class NoSearch:
            def paginate(self, *a, **kw):
                raise refresh.ApiError(404, "no search in this test")
        refresh.phase_discovery(NoSearch(), None, state, self.report, self.args, set(tags))
        return self.captured

    def state(self, **kw):
        base = {"not_a_db": {}, "db404": {}, "unshared": {}}
        base.update(kw)
        return base

    def test_an_unflagged_id_is_captured(self):
        self.assertEqual(self.discover(self.state(), ["db:" + rid(1)]), [rid(1)])

    def test_a_not_a_db_id_is_skipped(self):
        self.assertEqual(
            self.discover(self.state(not_a_db={rid(1): "2026-08-07"}), ["db:" + rid(1)]), [])

    def test_an_unshared_id_is_skipped(self):
        """The backfill files a database whose data sources are not shared with
        the integration. 1,322 of the 1,400 exclusions came through that branch,
        and every one of them is reachable from a row body — so without this the
        nightly re-asks Notion about all of them, against a budget that already
        goes PARTIAL on a third of runs."""
        self.assertEqual(
            self.discover(self.state(unshared={rid(1): "2026-08-07"}), ["db:" + rid(1)]), [])

    def test_state_without_the_key_at_all_still_works(self):
        """`coverage_backfill` builds its own state dict; a missing bucket must
        not crash the nightly's discovery phase."""
        s = {"not_a_db": {}, "db404": {}}
        self.assertEqual(self.discover(s, ["db:" + rid(1)]), [rid(1)])


if __name__ == "__main__":
    unittest.main()
