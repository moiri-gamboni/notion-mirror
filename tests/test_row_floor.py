"""The pre-commit row floor (`row_floor.py`), unit and end to end.

The shape it guards: on 2026-08-13 an hourly rows tick rendered a large CSV
concurrently with a manual run and committed 57 of 8,682 rows over the good
state. Nothing in the pipeline objected, and the mirror's committed table was
99% empty until a human noticed.

Two halves, because they can fail independently:

  * `check()` against throwaway git repos — the arithmetic and, more importantly,
    the exemptions. Every exemption exists to keep a false positive from wedging
    the pipeline (a refusal leaves the mirror dirty, and a dirty tree stops every
    later nightly and hourly tick), so each one is pinned by a test.
  * `refresh.sh` end to end through `NOTION_MIRROR` + `NOTION_MIRROR_TOOLS`, the
    same sandbox pattern `test_refresh_sh.py` uses: a stub engine in a directory of
    its own truncates a committed CSV in a throwaway mirror, and the wrapper must
    refuse the commit, record the reason and leave the tree alone. Nothing here
    reaches Notion or a phone — `curl` and `logger` are stubbed on PATH, so an
    attempted ntfy is recorded rather than delivered.
"""
import csv
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REFRESH_SH = os.path.join(TOOLS, "refresh.sh")
sys.path.insert(0, TOOLS)
import row_floor  # noqa: E402  (the engine directory is not a package)

# Relative to the mirror repository.
CSV_REL = os.path.join("workspace", "_databases",
                       "T " + "d" * 32, "T " + "d" * 32 + ".csv")

# Rewrites the CSV to $SANDBOX_ROWS rows and leaves the report the wrapper reads.
STUB_REFRESH = '''#!/usr/bin/env python3
import csv, json, os
mirror = os.environ["NOTION_MIRROR"]
path = os.path.join(mirror, %r)
n = int(os.environ["SANDBOX_ROWS"])
with open(path, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["Name", "Notes"])
    for i in range(n):
        w.writerow([f"row {i}", "body\\nwith a newline"])
state = os.path.join(mirror, "_meta", "state")
os.makedirs(state, exist_ok=True)
with open(os.path.join(state, "last-run-report.rows.json"), "w") as f:
    json.dump({"rows": {"requested": 0, "refreshed": []}, "props_probe": {},
               "requests": 0, "budget_exhausted": False}, f)
print(json.dumps({"changes": 1}))
''' % CSV_REL

STUB_RECORDER = '''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$SANDBOX_CALL_LOG.{name}"
exit 1
'''


def write_exec(path, text):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


def write_csv(path, rows, cols=("Name", "Notes")):
    """A CSV with multi-line cells, so a line count and a row count differ — an
    8,682-row table spans 65,787 physical lines, which is why the check parses
    rather than counting `\\n`."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(list(cols))
        for i in range(rows):
            w.writerow([f"row {i}", "body\nwith a newline"])


class RepoCase(unittest.TestCase):
    """A throwaway mirror that is a git repo of its own, with one committed CSV in it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="row-floor-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mirror = os.path.join(self.tmp, "mirror")
        os.makedirs(self.mirror)
        # The CSV below supplies the mirror fingerprint (`workspace/_databases`), so the
        # sandbox resolves as a mirror.
        self.csv = os.path.join(self.mirror, CSV_REL)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "test")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args):
        return subprocess.run(("git", "-C", self.mirror) + args, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

    def commit(self, rows, path=None):
        write_csv(path or self.csv, rows)
        self.git("add", "-A")
        self.git("commit", "-qm", f"{rows} rows")

    def check(self, **kw):
        return row_floor.check(self.mirror, **kw)


class ArithmeticTest(RepoCase):
    def test_the_incident_shape_is_a_breach(self):
        self.commit(8682)
        write_csv(self.csv, 57)
        (line,) = self.check()
        self.assertIn("8682 -> 57 rows", line)
        self.assertIn("99.3% lost", line)

    def test_a_table_that_grew_is_clean(self):
        """The repair after the incident: 57 rows back up to 8,764. A floor that
        objected to that would block every fix of the thing it caught."""
        self.commit(57)
        write_csv(self.csv, 8764)
        self.assertEqual(self.check(), [])

    def test_ordinary_churn_is_clean(self):
        self.commit(100)
        write_csv(self.csv, 96)
        self.assertEqual(self.check(), [])

    def test_the_threshold_is_the_percentage_it_says(self):
        self.commit(100)
        write_csv(self.csv, 61)          # 39% lost
        self.assertEqual(self.check(), [])
        write_csv(self.csv, 59)          # 41% lost
        self.assertEqual(len(self.check()), 1)

    def test_the_percentage_is_overridable(self):
        """`--pct` and its env twin exist so a one-off run with a known mass
        deletion does not need the code changed."""
        self.commit(100)
        write_csv(self.csv, 50)
        self.assertEqual(len(self.check()), 1)
        self.assertEqual(self.check(pct=60), [])

    def test_an_unchanged_tree_is_not_even_read(self):
        self.commit(8682)
        self.assertEqual(self.check(), [])


class ExemptionTest(RepoCase):
    """Each of these would otherwise be a false positive, and a false positive
    here does not cost one run: the refusal leaves the tree dirty, and the
    dirty-tree preflight then stops every nightly and every hourly tick until a
    human clears it. That asymmetry is why the check is this narrow."""

    def test_a_small_table_is_exempt(self):
        """Twelve rows losing five is a normal cleanup and crosses any
        percentage; the check cannot tell that from a bad render, so it declines
        to guess."""
        self.commit(12)
        write_csv(self.csv, 7)
        self.assertEqual(self.check(), [])
        self.assertEqual(len(self.check(min_rows=5)), 1)

    def test_a_deleted_csv_is_exempt(self):
        """The engine deletes a DB directory when Notion says the database is
        gone — a legitimate, reported event. Guarding removals here would wedge
        the pipeline on a real upstream deletion, and a removal is not the shape
        that produced the incident."""
        self.commit(8682)
        os.remove(self.csv)
        self.assertEqual(self.check(), [])

    def test_a_new_csv_is_exempt(self):
        """Nothing to fall from."""
        self.commit(100)
        new = os.path.join(self.mirror, "workspace", "_databases",
                           "N " + "e" * 32, "N " + "e" * 32 + ".csv")
        write_csv(new, 3)
        self.git("add", "-A")
        self.assertEqual(self.check(), [])

    def test_a_staged_change_is_seen_too(self):
        """`git diff HEAD` rather than the working-tree diff, so the check works
        whether or not the caller has already staged."""
        self.commit(400)
        write_csv(self.csv, 4)
        self.git("add", "-A")
        self.assertEqual(len(self.check()), 1)


class UnparseableTest(RepoCase):
    def test_a_working_copy_that_does_not_parse_is_a_breach(self):
        """A torn write is exactly what this guard is for, and the committed copy
        is intact — refusing costs an hour of staleness, committing costs the
        table.

        Driven the one way the csv module actually raises on a plausibly-damaged
        file: a field past the size limit. An unterminated quote does not raise,
        it swallows the rest of the file into one cell — which lands as a row
        count of 1 and is caught by the count instead."""
        self.commit(400)
        real = csv.field_size_limit(300_000)   # HEAD's cells are tiny; this one is not
        self.addCleanup(csv.field_size_limit, real)
        write_csv(self.csv, 400)
        with open(self.csv, "a") as f:
            f.write('"big","%s"\n' % ("x" * 400_000))
        (line,) = self.check()
        self.assertIn("does not parse", line)

    def test_a_big_cell_is_not_mistaken_for_a_torn_file(self):
        """The default csv limit is 128 KB and mirrored cells hold whole
        documents, so the module raises its own limit; without that, every run
        with a long comment would read as unparseable."""
        self.commit(400)
        write_csv(self.csv, 400)
        with open(self.csv, "a") as f:
            f.write('"big","%s"\n' % ("x" * 200_000))
        self.assertEqual(self.check(), [])


class CliTest(RepoCase):
    def run_cli(self, *argv, env=None, repo=True):
        cmd = (sys.executable, os.path.join(TOOLS, "row_floor.py"))
        if repo:
            cmd += ("--repo", self.mirror)
        return subprocess.run(cmd + argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, env=dict(os.environ, **(env or {})))

    def test_a_breach_exits_one_and_names_the_way_out(self):
        self.commit(8682)
        write_csv(self.csv, 57)
        res = self.run_cli()
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("8682 -> 57 rows", res.stdout)
        self.assertIn("NOTION_MIRROR_ROW_FLOOR_ALLOW_SHRINK=1", res.stdout)

    def test_the_default_repo_is_the_configured_mirror(self):
        """Without `--repo` the floor resolves the configured mirror and checks it."""
        self.commit(8682)
        write_csv(self.csv, 57)
        res = self.run_cli(repo=False, env={"NOTION_MIRROR": self.mirror})
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("8682 -> 57 rows", res.stdout)

    def test_no_repo_and_no_mirror_is_a_refusal_not_a_traceback(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("NOTION_MIRROR", "NOTION_MIRROR_TOOLS")}
        env["HOME"] = self.tmp   # no env file under it either
        res = subprocess.run((sys.executable, os.path.join(TOOLS, "row_floor.py")),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn("$NOTION_MIRROR", res.stderr)
        self.assertNotIn("Traceback", res.stderr)

    def test_allow_shrink_reports_the_breach_and_exits_zero(self):
        """A real mass deletion still has to be visible in the log — this accepts
        the shrink, it does not hide it."""
        self.commit(8682)
        write_csv(self.csv, 57)
        res = self.run_cli("--allow-shrink")
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertIn("8682 -> 57 rows", res.stdout)

    def test_the_env_knobs_match_the_flags(self):
        self.commit(100)
        write_csv(self.csv, 50)
        self.assertEqual(self.run_cli(env={"NOTION_MIRROR_ROW_FLOOR_PCT": "60"}).returncode, 0)
        self.assertEqual(
            self.run_cli(env={"NOTION_MIRROR_ROW_FLOOR_ALLOW_SHRINK": "1"}).returncode, 0)

    def test_a_clean_run_says_nothing(self):
        self.commit(100)
        write_csv(self.csv, 99)
        res = self.run_cli()
        self.assertEqual((res.returncode, res.stdout), (0, ""))


class WrapperTest(RepoCase):
    """`refresh.sh` end to end: the floor has to sit before the commit, and its
    refusal has to be recorded rather than merely returned."""

    def setUp(self):
        super().setUp()
        self.home = os.path.join(self.tmp, "home")
        self.config = os.path.join(self.tmp, "config")
        self.bin = os.path.join(self.tmp, "bin")
        self.state = os.path.join(self.mirror, "_meta", "state")
        # The engine lives in a directory of its own, not inside the mirror —
        # `$NOTION_MIRROR_TOOLS` is what points the wrapper at it, and the scripts it
        # copies there resolve the mirror through the `mirror_root.py` beside them.
        self.tools = os.path.join(self.tmp, "clone")
        for d in (self.home, self.config, self.bin, self.state, self.tools):
            os.makedirs(d, exist_ok=True)
        write_exec(os.path.join(self.tools, "refresh.py"), STUB_REFRESH)
        for name in ("rows_status.py", "row_floor.py", "mirror_root.py"):
            shutil.copy(os.path.join(TOOLS, name), os.path.join(self.tools, name))
        for name in ("curl", "logger"):
            write_exec(os.path.join(self.bin, name), STUB_RECORDER.replace("{name}", name))
        with open(os.path.join(self.config, ".claude.json"), "w") as f:
            json.dump({"mcpServers": {"notion": {"env": {"NOTION_TOKEN": "t"}}}}, f)
        self.call_log = os.path.join(self.tmp, "calls")
        # As in production: the rows status marker is gitignored, so recording a
        # refusal cannot itself dirty the tree the refusal is guarding.
        with open(os.path.join(self.mirror, ".gitignore"), "w") as f:
            f.write("_meta/state/\n")
        self.commit(400)  # tree clean after: the engine is outside the repo entirely

    def run_refresh(self, rows, mode="rows", extra=None):
        """`rows` = how many rows the stub engine will leave in the CSV."""
        env = dict(os.environ, NOTION_MIRROR=self.mirror, NOTION_MIRROR_TOOLS=self.tools,
                   HOME=self.home, CLAUDE_CONFIG_DIR=self.config,
                   PATH=self.bin + os.pathsep + os.environ["PATH"],
                   SANDBOX_ROWS=str(rows), SANDBOX_CALL_LOG=self.call_log,
                   **(extra or {}))
        env.pop("NOTION_TOKEN", None)
        args = ("rows", "--props-only") if mode == "rows" else (mode,)
        return subprocess.run(("bash", REFRESH_SH) + args, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def commits(self):
        return self.git("log", "--oneline").decode().strip().split("\n")

    def marker(self):
        p = os.path.join(self.state, "rows-refresh-status.json")
        if not os.path.exists(p):
            return {}
        with open(p) as f:
            return json.load(f)

    def ntfy_attempts(self):
        p = self.call_log + ".curl"
        if not os.path.exists(p):
            return []
        with open(p) as f:
            return [ln for ln in f.read().split("\n") if ln.strip()]

    def test_a_truncated_render_is_not_committed(self):
        before = self.commits()
        res = self.run_refresh(5)
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertEqual(self.commits(), before, "the truncated render must not be committed")

    def test_the_refusal_is_recorded_with_a_reason(self):
        res = self.run_refresh(5)
        marker = self.marker().get("last_attempt", {})
        self.assertEqual(marker.get("outcome"), "failed", res.stdout)
        self.assertEqual(marker.get("reason"), "row_floor")
        self.assertIn("400 -> 5 rows", marker.get("detail", ""))

    def test_the_tree_is_left_for_inspection(self):
        """Not reverted: whatever produced a 5-row render is worth looking at, and
        the dirty tree is also what stops the next tick from trying again."""
        self.run_refresh(5)
        with open(self.csv) as f:
            self.assertEqual(len(list(csv.reader(f))) - 1, 5)
        self.assertTrue(self.git("status", "--porcelain").strip())

    def test_rows_mode_ntfys_this_one_refusal(self):
        """Rows mode is silent by design — 24 ticks a day against a lock held for
        hours — but a floor breach is not a designed outcome, and it cannot storm:
        the tree it leaves dirty stops the next tick at the preflight."""
        res = self.run_refresh(5)
        self.assertTrue(any("Priority: high" in c for c in self.ntfy_attempts()),
                        f"expected a high-priority ntfy, got {self.ntfy_attempts()} / {res.stdout}")

    def test_a_normal_render_still_commits(self):
        before = self.commits()
        res = self.run_refresh(395)
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertEqual(len(self.commits()), len(before) + 1, res.stdout)
        self.assertFalse(self.git("status", "--porcelain").strip())

    def test_a_real_mass_deletion_can_be_let_through(self):
        before = self.commits()
        res = self.run_refresh(5, extra={"NOTION_MIRROR_ROW_FLOOR_ALLOW_SHRINK": "1"})
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertEqual(len(self.commits()), len(before) + 1, res.stdout)

    def test_the_nightly_refuses_loudly_instead_of_quietly(self):
        """Same breach, different volume: `daily` routes through `fail()`, which
        is a high-priority ntfy and no status marker (the marker is the hourly
        job's channel)."""
        before = self.commits()
        res = self.run_refresh(5, mode="daily")
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertEqual(self.commits(), before)
        self.assertTrue(any("Priority: high" in c for c in self.ntfy_attempts()))
        self.assertEqual(self.marker(), {})


if __name__ == "__main__":
    unittest.main()
