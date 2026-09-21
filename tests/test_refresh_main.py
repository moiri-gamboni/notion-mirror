"""`main()`'s own bookkeeping: what survives a run, and what a run reports.

Everything here is decided in the ~100 lines of `main()` that no phase test can
reach — the state persistence block, the `finally`, and the mode filters on it.
That is also where a mistake is most expensive: these lines run after the mirror
has been fetched and written, so losing them loses the night's work rather than
one row.

No network. Every phase is stubbed out; the API object is constructed but never
called, and the whole run happens inside a temp tree.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import refresh  # noqa: E402  (_tools is not a package; discover's top dir is tests/)

PHASES = ("check_webhook_liveness", "consume_db_events", "phase_queue", "phase_dbs",
          "drain_props_probe", "phase_content", "phase_schema_sweep", "phase_discovery",
          "phase_comment_shard", "phase_full_comment_sweep", "phase_rows",
          "run_coverage_assert", "regenerate_structure_md", "build_comment_index")


class MainCase(unittest.TestCase):
    """One temp mirror, every phase neutralised, `main()` runnable by argv."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="refresh-main-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state_dir = os.path.join(self.tmp, "state")
        self.dbs = os.path.join(self.tmp, "_databases")
        for d in (self.state_dir, self.dbs):
            os.makedirs(d)
        for name, value in (("STATE", self.state_dir), ("DBS", self.dbs), ("WS", self.tmp)):
            self.addCleanup(setattr, refresh, name, getattr(refresh, name))
            setattr(refresh, name, value)
        for name in PHASES:
            self.addCleanup(setattr, refresh, name, getattr(refresh, name))
        self.stub_phases()
        self.addCleanup(os.environ.pop, "NOTION_TOKEN", None)
        os.environ["NOTION_TOKEN"] = "test-token-not-used"
        self.addCleanup(setattr, sys, "argv", sys.argv)
        # Every write mode now takes the mirror lock in `main()`. Point it at the
        # temp tree: a test that took the real `~/.locks/notion-mirror-internal`
        # would refuse a live refresh for as long as it ran, and a live refresh
        # holding it would fail the suite.
        self.lock = os.path.join(self.tmp, "mirror.lock")
        self.addCleanup(os.environ.pop, "NOTION_MIRROR_LOCK", None)
        os.environ["NOTION_MIRROR_LOCK"] = self.lock
        self.addCleanup(os.environ.pop, "NOTION_MIRROR_LOCK_FD", None)
        os.environ.pop("NOTION_MIRROR_LOCK_FD", None)

    def page_with_standin(self):
        """A corpus the coverage census can actually measure something in."""
        with open(os.path.join(self.tmp, "Parent.md"), "w") as f:
            f.write("# Parent\n\n- 🗄️ Some DB `" + "c" * 32 + "`\n")

    def stub_phases(self):
        self.real_coverage_assert = refresh.run_coverage_assert
        refresh.check_webhook_liveness = lambda report: None
        refresh.consume_db_events = lambda state, report, discovered: None
        refresh.regenerate_structure_md = lambda: False
        refresh.run_coverage_assert = lambda *a, **kw: None
        refresh.build_comment_index = lambda: {}
        for name in ("phase_queue", "phase_dbs", "drain_props_probe", "phase_schema_sweep",
                     "phase_discovery", "phase_comment_shard", "phase_full_comment_sweep",
                     "phase_rows"):
            setattr(refresh, name, lambda *a, **kw: None)
        refresh.phase_content = lambda *a, **kw: {}

    def run_main(self, *argv):
        sys.argv = ["refresh.py", *argv]
        return refresh.main()

    def state_file(self, name):
        with open(os.path.join(self.state_dir, name)) as f:
            return json.load(f)

    def write_state(self, name, obj):
        with open(os.path.join(self.state_dir, name), "w") as f:
            json.dump(obj, f)


class UnsharedRoundTripTest(MainCase):
    """The `unshared` bucket is written by `coverage_backfill.py` and read by the
    nightly's discovery phase, so it has to survive a nightly untouched — a run
    that dropped it would put all 1,322 ids back into discovery's work list."""

    def test_the_bucket_survives_a_run(self):
        self.write_state("db-flags.json",
                         {"not_a_db": {}, "db404": {}, "unshared": {"a" * 32: "2026-08-07"}})
        self.run_main("--mode", "daily", "--budget", "5")
        self.assertEqual(self.state_file("db-flags.json")["unshared"], {"a" * 32: "2026-08-07"})

    def test_a_flags_file_without_the_bucket_gains_an_empty_one(self):
        self.write_state("db-flags.json", {"not_a_db": {}, "db404": {}})
        self.run_main("--mode", "daily", "--budget", "5")
        self.assertEqual(self.state_file("db-flags.json")["unshared"], {})

    def test_an_unshared_id_is_left_out_of_pending_discovery(self):
        """Otherwise the pending file carries them from run to run forever."""
        self.write_state("db-flags.json",
                         {"not_a_db": {}, "db404": {}, "unshared": {"a" * 32: "2026-08-07"}})

        def discover(api, users, state, report, args, discovered):
            discovered.update({"db:" + "a" * 32, "db:" + "b" * 32})
        refresh.phase_discovery = discover
        self.run_main("--mode", "daily", "--budget", "5")
        self.assertEqual(self.state_file("pending-discovery.json"), ["db:" + "b" * 32])


class DryRunReportTest(MainCase):
    """A dry run writes nothing, so its report is the entire deliverable."""

    def queue_two(self):
        def queue(api, users, state, report, args, max_req=None, discovered=None):
            state["queue"] += [{"kind": "row_probe", "db": "d" * 32, "row": "r" * 32},
                               {"kind": "row_probe", "db": "d" * 32, "row": "s" * 32}]
        refresh.phase_queue = queue

    def test_a_dry_run_reports_the_probes_it_would_defer(self):
        """The count sat inside the not-dry-run arm of the persistence branch, so
        a dry run always reported 0 and report_md dropped its "Deferred to next
        run" line — one of the things a dry run exists to show. It writes to the
        report, not to disk, so it belongs above the branch."""
        self.queue_two()
        self.run_main("--mode", "daily", "--budget", "5", "--dry-run")
        report = self.state_file("last-run-report.dry-run.json")
        self.assertEqual(report["deferred"]["row_probes_queued"], 2)
        self.assertIn("Deferred to next run: 2", self.report_md("last-run-report.dry-run.md"))

    def test_a_real_run_still_reports_it(self):
        self.queue_two()
        self.run_main("--mode", "daily", "--budget", "5")
        self.assertEqual(self.state_file("last-run-report.json")["deferred"]
                         ["row_probes_queued"], 2)

    def test_a_dry_run_still_writes_no_state(self):
        """The reason the count was in there in the first place — moving it must
        not move any of the writes with it."""
        self.queue_two()
        self.run_main("--mode", "daily", "--budget", "5", "--dry-run")
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, "probe-queue.json")))
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, "last-run.json")))

    def test_a_dry_run_does_not_record_a_coverage_floor(self):
        """The coverage assert runs on a dry run — it reads the corpus, which a
        dry run does not change — but it must not leave the floor behind. The
        write can only ever raise the floor, and raising is the direction that
        makes a later honest run report INCONCLUSIVE, so a dry run against a
        mid-sync or partially-restored tree is exactly where it would bite."""
        refresh.run_coverage_assert = self.real_coverage_assert
        self.page_with_standin()
        self.run_main("--mode", "daily", "--budget", "5", "--dry-run")
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, "coverage-floor.json")))

    def test_a_real_run_does_record_one(self):
        refresh.run_coverage_assert = self.real_coverage_assert
        self.page_with_standin()
        self.run_main("--mode", "daily", "--budget", "5")
        self.assertTrue(os.path.exists(os.path.join(self.state_dir, "coverage-floor.json")))

    def report_md(self, name):
        with open(os.path.join(self.state_dir, name)) as f:
            return f.read()


class ContaminationScanPlacementTest(MainCase):
    """The contamination assert reads the whole corpus from inside the `finally`,
    and it ran there BEFORE the state was persisted.

    Its scan is not exception-free — it guards `open().read()` with `except
    OSError` only, while a non-UTF-8 byte anywhere in the 86,750-file corpus
    raises UnicodeDecodeError (a ValueError), and `os.listdir` on a directory
    that vanished mid-run raises FileNotFoundError. Either one, raised from the
    top of the `finally`, discarded every state write and the run report with
    them: the night's newly-queued probes, the new audit-pool members, and the
    content cursor, which then re-walks the whole content corpus next run.
    """

    def seed_state(self):
        self.write_state("probe-queue.json", [{"kind": "row_probe", "db": "d" * 32,
                                               "row": "r" * 32}])
        self.write_state("last-run.json", {"content_since": "OLD"})

        def queue(api, users, state, report, args, max_req=None, discovered=None):
            state["queue"].append({"kind": "row_probe", "db": "e" * 32, "row": "n" * 32})
            state["content_since"] = "NEW"
        refresh.phase_queue = queue

    def explode(self):
        def boom():
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        refresh.build_comment_index = boom

    def test_a_raising_scan_does_not_discard_the_queued_probes(self):
        self.seed_state()
        self.explode()
        self.run_main("--mode", "daily", "--budget", "5")
        self.assertEqual(len(self.state_file("probe-queue.json")), 2)

    def test_a_raising_scan_does_not_rewind_the_content_cursor(self):
        self.seed_state()
        self.explode()
        self.run_main("--mode", "daily", "--budget", "5")
        self.assertEqual(self.state_file("last-run.json")["content_since"], "NEW")

    def test_a_raising_scan_still_leaves_a_run_report(self):
        self.seed_state()
        self.explode()
        self.run_main("--mode", "daily", "--budget", "5")
        self.assertTrue(os.path.exists(os.path.join(self.state_dir, "last-run-report.json")))

    def test_a_scan_that_could_not_run_is_not_reported_as_clean(self):
        """An assert that could not run must not read as a pass — that is the
        whole failure class this assert exists to rule out."""
        self.seed_state()
        self.explode()
        rc = self.run_main("--mode", "daily", "--budget", "5")
        note = next(n for n in self.state_file("last-run-report.json")["notes"]
                    if "contamination" in n)
        self.assertIn("UnicodeDecodeError", note)
        self.assertEqual(rc, 1)

    def test_a_clean_scan_still_passes(self):
        self.seed_state()
        rc = self.run_main("--mode", "daily", "--budget", "5")
        self.assertEqual(rc, 0)
        self.assertEqual(self.state_file("last-run-report.json")["comments"]
                         ["contamination_breaches"], 0)

    def test_a_breach_still_stops_the_run(self):
        """The block-the-commit posture is unchanged: refresh.sh keys on the
        exit status, so a breach must still be non-zero."""
        self.seed_state()
        refresh.build_comment_index = lambda: {"a copied thread": set(range(200))}
        self.assertEqual(self.run_main("--mode", "daily", "--budget", "5"), 1)


class RowsModeIsCheckedTest(MainCase):
    """`--mode rows` writes comments 24 times a day — `phase_rows` probes each
    named row, which renders the comment bullets into the row file, and the
    hourly job commits them. It was outside the assert's mode list, which was
    written before rows mode existed, so contamination introduced by an hourly
    tick was committed and caught by the next nightly instead of at write time.
    The 2026-07-27 incident is exactly the class where "committed and caught
    later" is expensive: 37,519 misattributed instances over 3,390 pages.

    Measured cost of including it: 6.55s and no API requests."""

    def test_a_rows_run_checks_for_contamination(self):
        scanned = []
        refresh.build_comment_index = lambda: scanned.append(1) or {}
        self.run_main("--mode", "rows", "--budget", "5")
        self.assertEqual(len(scanned), 1)

    def test_a_rows_run_reports_a_breach_and_exits_non_zero(self):
        refresh.build_comment_index = lambda: {"a copied thread": set(range(200))}
        rc = self.run_main("--mode", "rows", "--budget", "5")
        self.assertEqual(rc, 1)
        self.assertEqual(self.state_file("last-run-report.rows.json")["comments"]
                         ["contamination_breaches"], 1)

    def test_a_place_run_is_still_left_out(self):
        """`--mode place` writes no comments, so it buys nothing."""
        scanned = []
        refresh.build_comment_index = lambda: scanned.append(1) or {}
        real = refresh.place_unplaced_pass
        self.addCleanup(setattr, refresh, "place_unplaced_pass", real)
        refresh.place_unplaced_pass = lambda *a, **kw: None
        refresh.load_meta_jsonl = lambda: ({}, [])
        self.run_main("--mode", "place", "--budget", "5")
        self.assertEqual(scanned, [])


class MirrorLockTest(MainCase):
    """The engine's own mutual exclusion, added after 2026-08-13.

    Until then the lock lived only in `refresh.sh`, so running the engine
    directly forfeited it in silence. A manual People re-pull did exactly that,
    the hourly rows tick fired mid-flight, and the two renders of one CSV ended
    with 57 of 8,682 rows committed. The wrapper still takes the lock — it also
    commits, and the commit belongs in the same critical section — so `main()`
    has to be re-entrant *under the wrapper* and under nothing else.

    `flock` locks belong to the open file description, not to the process, so a
    second fd on the same file conflicts even from inside this one: holding it
    here is a faithful stand-in for another writer, no subprocess needed.
    """

    def hold_the_lock(self):
        """Hold it the way a foreign writer would: no fd marker in the env."""
        import fcntl
        fh = open(self.lock, "w")
        self.addCleanup(fh.close)
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh

    def hold_it_as_the_wrapper_does(self):
        """Hold it and hand the fd down, which is what `refresh.sh` now does."""
        fh = self.hold_the_lock()
        os.environ["NOTION_MIRROR_LOCK_FD"] = str(fh.fileno())
        return fh

    def report_exists(self, name="last-run-report.rows.json"):
        return os.path.exists(os.path.join(self.state_dir, name))

    def test_a_write_mode_refuses_while_another_writer_holds_the_lock(self):
        self.hold_the_lock()
        rc = self.run_main("--mode", "rows", "--budget", "5")
        self.assertEqual(rc, 3)
        self.assertFalse(self.report_exists(), "a refused run must not write anything")

    def test_the_refusal_says_which_lock_and_how_to_proceed(self):
        """The message is the whole interface here: whoever meets it is holding a
        terminal and needs to know it is a lock, which one, and that refresh.sh
        takes it for them."""
        self.hold_the_lock()
        err = io.StringIO()
        real, sys.stderr = sys.stderr, err
        try:
            self.run_main("--mode", "daily", "--budget", "5")
        finally:
            sys.stderr = real
        self.assertIn(self.lock, err.getvalue())
        self.assertIn("refresh.sh", err.getvalue())

    def test_a_run_under_the_wrapper_s_own_lock_proceeds(self):
        """The re-entrancy that keeps `refresh.sh` working: the script holds the
        lock across the commit, and the engine it invokes must not deadlock on
        it. Without the fd handshake this is the 'a mirror run is in progress'
        report where the only run in progress is the caller's own."""
        self.hold_it_as_the_wrapper_does()
        self.assertEqual(self.run_main("--mode", "rows", "--budget", "5"), 0)
        self.assertTrue(self.report_exists())

    def test_an_fd_marker_pointing_somewhere_else_is_not_believed(self):
        """The env var alone is an unchecked claim, and a stale one inherited from
        an unrelated environment would buy write access while a real writer is
        mid-render. The fd has to be open on the lock file itself."""
        self.hold_the_lock()
        other = open(os.path.join(self.tmp, "not-the-lock"), "w")
        self.addCleanup(other.close)
        os.environ["NOTION_MIRROR_LOCK_FD"] = str(other.fileno())
        self.assertEqual(self.run_main("--mode", "rows", "--budget", "5"), 3)

    def test_the_lock_is_still_held_during_the_phases(self):
        """The lock has to cover the writes, not just the acquire. It is held on
        an os-level fd for exactly this reason — a Python file object releases its
        flock as soon as it is collected, so the protection would rest on nobody
        tidying away a reference, which is the bug wearing the fix's clothes."""
        seen = []

        def probe(*a, **kw):
            import fcntl
            fh = open(self.lock, "w")
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen.append("free")
            except OSError:
                seen.append("held")
            finally:
                fh.close()
        refresh.phase_dbs = probe
        self.assertEqual(self.run_main("--mode", "daily", "--budget", "5"), 0)
        self.assertEqual(seen, ["held"])

    def test_a_dry_run_does_not_contend(self):
        """A dry run guards every write, so blocking it would only mean nobody can
        look at what the mirror would do while the nightly runs."""
        self.hold_the_lock()
        self.assertEqual(self.run_main("--mode", "daily", "--budget", "5", "--dry-run"), 0)


if __name__ == "__main__":
    unittest.main()
