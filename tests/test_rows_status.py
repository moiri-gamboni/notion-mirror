"""The hourly rows job's status marker.

The job never ntfys, so this file is the whole alerting surface — everything it
records is read by a dead-man in a different cron. Two verdicts therefore matter
more than the plumbing: a run that refreshed none of the rows it named, and a run
that ran out of budget partway, both exit 0 from `refresh.py` and leave a clean
tree. If either of those advanced the success clock, the clock would say the job
is healthy while the rows it exists to keep fresh go stale indefinitely.

Fully offline: every write goes to a temp state directory.
"""
import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mirror_root  # noqa: E402
import rows_status  # noqa: E402


def report(requested=3, refreshed=3, skipped=0, errors=0, drained=0,
           deferred=0, requests=12, budget_exhausted=False):
    return {
        "rows": {"requested": requested,
                 "refreshed": [f"{i:032x}" for i in range(refreshed)],
                 "skipped": [{"row": "x"}] * skipped,
                 "errors": [{"row": "x"}] * errors},
        "props_probe": {"drained": drained, "deferred": deferred, "errors": []},
        "requests": requests,
        "budget_exhausted": budget_exhausted,
    }


class MarkerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name

    def marker(self):
        return rows_status.load(self.dir)

    def record(self, *a, **kw):
        return rows_status.record(*a, state_dir=self.dir, **kw)


class VerdictTest(unittest.TestCase):
    """What a finished run's report means."""

    def test_a_normal_run_is_ok(self):
        outcome, reason, _detail, stats = rows_status.verdict_from_report(report())
        self.assertEqual((outcome, reason), (rows_status.OK, ""))
        self.assertEqual(stats["refreshed"], 3)

    def test_every_named_row_refused_is_a_failure_not_an_empty_success(self):
        # Exits 0, commits nothing, leaves a clean tree — identical from the
        # outside to an hour in which nothing was edited.
        outcome, reason, _d, _s = rows_status.verdict_from_report(
            report(requested=44, refreshed=0, skipped=44))
        self.assertEqual((outcome, reason), (rows_status.FAILED, "all_rows_refused"))

    def test_a_props_only_tick_names_no_rows_and_is_still_ok(self):
        # requested == 0 is the common hourly tick: no task row changed, drain
        # the property queue. It must not be read as "everything was refused".
        outcome, reason, _d, stats = rows_status.verdict_from_report(
            report(requested=0, refreshed=0, drained=5, requests=5))
        self.assertEqual((outcome, reason), (rows_status.OK, ""))
        self.assertEqual(stats["props_drained"], 5)

    def test_budget_exhaustion_is_a_failure_because_the_tail_stays_stale(self):
        outcome, reason, _d, _s = rows_status.verdict_from_report(
            report(requested=44, refreshed=40, budget_exhausted=True))
        self.assertEqual((outcome, reason), (rows_status.FAILED, "budget_exhausted"))

    def test_errors_are_counted_across_both_phases(self):
        r = report(errors=2)
        r["props_probe"]["errors"] = [{"row": "y"}]
        _o, _r, _d, stats = rows_status.verdict_from_report(r)
        self.assertEqual(stats["errors"], 3)


class RecordTest(MarkerTestCase):
    def test_only_an_ok_advances_the_success_clock(self):
        self.record(rows_status.OK)
        first = self.marker()["last_success_ts"]
        self.record(rows_status.SKIPPED, "lock_contention", "the nightly holds it")
        self.record(rows_status.FAILED, "refresh_failed", "boom")
        after = self.marker()
        self.assertEqual(after["last_success_ts"], first)
        self.assertEqual(after["last_attempt"]["reason"], "refresh_failed")

    def test_consecutive_non_ok_counts_up_and_an_ok_clears_it(self):
        for _ in range(3):
            self.record(rows_status.SKIPPED, "lock_contention", "x")
        self.assertEqual(self.marker()["consecutive_not_ok"], 3)
        self.record(rows_status.OK)
        self.assertEqual(self.marker()["consecutive_not_ok"], 0)

    def test_a_corrupt_marker_is_replaced_rather_than_crashing_the_job(self):
        # The marker is the alerting surface; a truncated write from a killed
        # process must not stop the next tick from recording.
        with open(rows_status.path(self.dir), "w") as f:
            f.write("{not json")
        self.assertEqual(rows_status.load(self.dir), {})
        self.record(rows_status.OK)
        self.assertIn("last_success_ts", self.marker())

    def test_the_file_is_valid_json_after_every_write(self):
        self.record(rows_status.OK)
        with open(rows_status.path(self.dir)) as f:
            self.assertEqual(json.load(f)["last_attempt"]["outcome"], rows_status.OK)

    def test_the_writer_never_guesses_where_the_marker_lives(self):
        """`record` takes its state directory from the caller — the wrapper, which has
        already resolved the mirror — rather than resolving one itself, so a writer can
        never land a marker under a root that the caller did not choose."""
        with self.assertRaises(TypeError):
            rows_status.record(rows_status.OK)


class DeadManFixture(MarkerTestCase):
    """Plumbing for the marker's reader. No tests of its own."""

    def check(self, **kw):
        kw.setdefault("state_dir", self.dir)
        return rows_status.check(**kw)

    def check_argv(self, *extra):
        return ["--check", "--state-dir", self.dir, *extra]

    def age(self, hours):
        return (datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(hours=hours)).isoformat()


class DeadManTest(DeadManFixture):
    """The reader, run from a health check in a different cron — the marker's only listener."""

    def test_a_fresh_success_is_silent(self):
        self.record(rows_status.OK)
        self.assertEqual(self.check(), [])

    def test_a_stale_success_alarms(self):
        rows_status._save({"last_success_ts": self.age(7),
                           "last_attempt": {"outcome": "skipped",
                                            "reason": "lock_contention"}}, self.dir)
        alarms = self.check()
        self.assertEqual(len(alarms), 1)
        self.assertIn("lock_contention", alarms[0])

    def test_skips_alone_never_clear_the_alarm(self):
        # The wedged-nightly case: every tick exits non-zero from flock -n, which
        # is not the job failing. Only a success may quiet this.
        rows_status._save({"last_success_ts": self.age(9)}, self.dir)
        for _ in range(5):
            self.record(rows_status.SKIPPED, "lock_contention", "held")
        self.assertEqual(len(self.check()), 1)
        self.record(rows_status.OK)
        self.assertEqual(self.check(), [])

    def test_a_missing_marker_alarms_rather_than_reading_as_healthy(self):
        # Cron line never deployed, or the job dies before its first write.
        alarms = self.check()
        self.assertEqual(len(alarms), 1)
        self.assertIn("never run", alarms[0])

    def test_a_corrupt_marker_alarms(self):
        with open(rows_status.path(self.dir), "w") as f:
            f.write("{truncated")
        self.assertEqual(len(self.check()), 1)

    def test_the_threshold_clears_a_normal_nightly_blackout(self):
        # The nightly holds the lock up to ~4.5h, so 2-4 consecutive ticks are
        # refused every night by design. That must not alarm.
        rows_status._save({"last_success_ts": self.age(5)}, self.dir)
        self.assertEqual(self.check(), [])

    def test_the_threshold_is_overridable(self):
        rows_status._save({"last_success_ts": self.age(5)}, self.dir)
        self.assertEqual(len(self.check(max_age_h=4)), 1)

    def test_the_cli_exit_status_is_what_health_check_branches_on(self):
        self.record(rows_status.OK)
        self.assertEqual(rows_status.main(self.check_argv()), 0)
        rows_status._save({"last_success_ts": self.age(12)}, self.dir)
        self.assertEqual(rows_status.main(self.check_argv()), 1)
        self.assertEqual(rows_status.main(self.check_argv("--max-age-hours", "24")), 0)


class CliTest(MarkerTestCase):
    def run_cli(self, *argv):
        return rows_status.main([*argv, "--state-dir", self.dir])

    def test_exit_status_is_zero_only_for_ok(self):
        self.assertEqual(self.run_cli("--outcome", "ok"), 0)
        self.assertEqual(self.run_cli("--outcome", "skipped", "--reason", "x"), 1)
        self.assertEqual(self.run_cli("--outcome", "failed", "--reason", "x"), 1)

    def test_from_report_applies_the_verdict_rules(self):
        path = os.path.join(self.dir, "r.json")
        with open(path, "w") as f:
            json.dump(report(requested=44, refreshed=0), f)
        self.assertEqual(self.run_cli("--from-report", path), 1)
        self.assertEqual(self.marker()["last_attempt"]["reason"], "all_rows_refused")

    def test_an_unreadable_report_is_a_failure_not_a_silent_success(self):
        self.assertEqual(self.run_cli("--from-report",
                                      os.path.join(self.dir, "absent.json")), 1)
        self.assertEqual(self.marker()["last_attempt"]["reason"], "no_report")

    def test_outcome_and_from_report_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            self.run_cli("--outcome", "ok", "--from-report", "/dev/null")


class UnmountedMirrorTest(unittest.TestCase):
    """The dead-man with the mirror gone — the case that decided where paths are resolved.

    A mirror on a `nofail` bind mount from a second volume is, unmounted, an empty
    directory, and the mirror root stops resolving. Every other engine module asserts that
    at import and refuses, which is right for a writer. This module is what a health check
    runs *because* something is broken, so it resolves on use instead: the mirror alarm
    degrades to one line naming the refusal, and a caller composing further alarms on top
    of it keeps answering.

    Nothing is injected here on purpose. The injected paths every other class uses would
    hide exactly the bug this guards: a resolution hoisted back to import time passes with
    every path handed in and fails on the one morning the volume does not come back.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mount = os.path.join(self.tmp.name, "mirror")
        os.makedirs(self.mount)   # the empty mount point
        prev = os.environ.get("NOTION_MIRROR")
        os.environ["NOTION_MIRROR"] = self.mount
        self.addCleanup(lambda: os.environ.__setitem__("NOTION_MIRROR", prev)
                        if prev is not None else os.environ.pop("NOTION_MIRROR", None))

    def test_an_unmounted_mirror_is_one_alarm_line_naming_the_refusal(self):
        alarms = rows_status.check()
        self.assertEqual(len(alarms), 1, alarms)
        self.assertTrue(alarms[0].startswith("mirror alarms unavailable: "), alarms[0])
        self.assertIn("is not a Notion mirror", alarms[0])

    def test_importing_this_module_asserts_nothing(self):
        """The pin for the split: resolution hoisted to import time would take the whole
        health check down with the volume, silently, since the import is at the top."""
        done = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r});"
             " import rows_status; print(rows_status.OK)"],
            env=dict(os.environ, NOTION_MIRROR=self.mount),
            capture_output=True, text=True, check=False)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "ok"), done.stderr)


class WhollyWrongRootTest(unittest.TestCase):
    """Neither side of the CLI presents as a crash when nothing resolves."""

    def test_recording_with_nothing_configured_prints_the_refusal_and_exits_two(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with tempfile.TemporaryDirectory() as home:
            env = {k: v for k, v in os.environ.items()
                   if k not in ("NOTION_MIRROR", "NOTION_MIRROR_TOOLS")}
            env["HOME"] = home
            done = subprocess.run(
                [sys.executable, os.path.join(here, "rows_status.py"), "--outcome", "ok"],
                capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn(f"${mirror_root.ENV}", done.stderr)
        self.assertNotIn("Traceback", done.stderr)

    def test_check_with_nothing_configured_prints_one_line_and_exits_one(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with tempfile.TemporaryDirectory() as home:
            env = {k: v for k, v in os.environ.items()
                   if k not in ("NOTION_MIRROR", "NOTION_MIRROR_TOOLS")}
            env["HOME"] = home   # no env file either
            done = subprocess.run(
                [sys.executable, os.path.join(here, "rows_status.py"), "--check"],
                capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("mirror alarms unavailable", done.stdout)
        self.assertIn(f"${mirror_root.ENV}", done.stdout)
        self.assertNotIn("Traceback", done.stderr)


if __name__ == "__main__":
    unittest.main()
