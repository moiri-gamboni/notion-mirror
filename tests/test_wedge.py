"""A dirty mirror no longer wedges every run until a human clears it.

In 2026-09 the nightly refused on 7 of 10 nights. Six of them followed two
crashed nightlies (09-09, 09-17) whose partial writes sat uncommitted until
someone ran NOTION_REFRESH_RESUME=1; the seventh (09-22) was one README edit in
the mirror repository left uncommitted overnight. The hourly rows tick refused
all the while, which is what fired tasksync's dead-man.

Two rules replace the blanket refusal. An uncommitted path outside what the
engine writes is someone's work and is left alone (never staged, never
committed). An uncommitted engine path still stops a run, unless the run marker
says the last run died before committing, in which case the nightly resumes over
it by itself. A refused run (row floor, contamination) is left for inspection.
"""
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tree_status  # noqa: E402
from test_refresh_sh import Sandbox, write_exec  # noqa: E402

STUB = '''#!/usr/bin/env python3
import json, os, sys
mirror = os.environ["NOTION_MIRROR"]
state = os.path.join(mirror, "_meta", "state")
os.makedirs(state, exist_ok=True)
with open(os.path.join(mirror, "workspace", "touched.txt"), "a") as f:
    f.write("x")
rows = "--mode" in sys.argv and sys.argv[sys.argv.index("--mode") + 1] == "rows"
name = "last-run-report.rows.json" if rows else "last-run-report.json"
with open(os.path.join(state, name), "w") as f:
    json.dump({"rows": {"requested": 0, "refreshed": []}, "props_probe": {}, "requests": 0,
               "comments": {"contamination_breaches":
                            int(os.environ.get("SANDBOX_BREACHES", "0"))}}, f)
if os.environ.get("SANDBOX_EXIT", "0") != "0":
    raise SystemExit(int(os.environ["SANDBOX_EXIT"]))
print(json.dumps({"changes": 0}))
'''


class Wedge(Sandbox):
    def setUp(self):
        super().setUp()
        write_exec(os.path.join(self.tools, "refresh.py"), STUB)
        self.write_config("tok")
        with open(os.path.join(self.mirror, "README.md"), "w") as f:
            f.write("mirror readme\n")
        self.git("add", "README.md")
        self.git("commit", "-qm", "readme")

    def go(self, mode, **env):
        args = ("rows", "--props-only") if mode == "rows" else (mode,)
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            return self.run_refresh("tok", args=args)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def run_mark(self):
        p = os.path.join(self.state, "refresh-run.json")
        return json.load(open(p)) if os.path.exists(p) else None

    def dirty(self):
        return self.git("status", "--porcelain").strip()

    def head_files(self):
        return self.git("show", "--name-only", "--format=", "HEAD").split()


class ForeignEdits(Wedge):
    def test_a_readme_edit_no_longer_stops_the_hourly_tick(self):
        with open(os.path.join(self.mirror, "README.md"), "a") as f:
            f.write("an agent's edit\n")
        res = self.go("rows")
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertEqual(self.head_files(), ["workspace/touched.txt"])
        self.assertEqual(self.dirty(), "M README.md", "the edit is left, unstaged")
        self.assertIsNone(self.run_mark(), "a committed run clears its marker")

    def test_the_nightly_proceeds_and_says_so(self):
        with open(os.path.join(self.mirror, "notes.md"), "w") as f:
            f.write("scratch\n")
        res = self.go("daily")
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertIn("leaving 1 uncommitted path(s) outside the engine's alone: notes.md", res.stdout)
        self.assertNotIn("notes.md", self.head_files())

    def test_a_staged_foreign_change_still_refuses(self):
        with open(os.path.join(self.mirror, "README.md"), "a") as f:
            f.write("mid-commit\n")
        self.git("add", "README.md")
        res = self.go("rows")
        self.assertEqual(res.returncode, 1)
        self.assertEqual(self.marker()["last_attempt"]["reason"], "git_busy")


class CrashedRuns(Wedge):
    def crash(self, mode="daily", **env):
        res = self.go(mode, SANDBOX_EXIT="1", **env)
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(self.dirty(), "the crashed run leaves its writes")
        return res

    def test_a_crash_is_recorded_and_the_next_nightly_resumes_by_itself(self):
        self.crash()
        self.assertEqual(self.run_mark()["status"], "crashed")
        res = self.go("daily")
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertIn("auto-resume", res.stdout)
        self.assertEqual(self.dirty(), "")
        self.assertIsNone(self.run_mark())

    def test_a_run_killed_outright_is_resumed_too(self):
        self.crash()
        mark = self.run_mark()
        mark["status"] = "running"  # killed: nothing got to record the crash
        json.dump(mark, open(os.path.join(self.state, "refresh-run.json"), "w"))
        self.assertEqual(self.go("daily").returncode, 0)

    def test_the_hourly_tick_does_not_sweep_a_crashed_run_into_its_commit(self):
        self.crash()
        res = self.go("rows")
        self.assertEqual(res.returncode, 1)
        self.assertEqual(self.marker()["last_attempt"]["reason"], "dirty_tree")

    def test_a_contamination_refusal_is_left_for_inspection(self):
        self.crash(SANDBOX_BREACHES="3")
        self.assertEqual(self.run_mark()["status"], "refused")
        res = self.go("daily")
        self.assertEqual(res.returncode, 1)
        self.assertIn("uncommitted changes in the engine's own paths", res.stdout)
        self.assertIn("workspace/touched.txt", res.stdout)

    def test_a_dirty_tree_no_run_accounts_for_still_refuses(self):
        with open(os.path.join(self.mirror, "workspace", "hand-edit.md"), "w") as f:
            f.write("x\n")
        res = self.go("daily")
        self.assertEqual(res.returncode, 1)
        self.assertIn("FAIL:", res.stdout)


class TreeStatus(Wedge):
    def test_classify(self):
        for rel in ("workspace/a.md", "README.md", "summaries/s.md", "CHANGELOG.md"):
            os.makedirs(os.path.dirname(os.path.join(self.mirror, rel)) or self.mirror,
                        exist_ok=True)
            with open(os.path.join(self.mirror, rel), "w") as f:
                f.write("x\n")
        got = tree_status.classify(self.mirror)
        self.assertEqual(sorted(got["engine"]), ["CHANGELOG.md", "workspace/a.md"])
        self.assertEqual(sorted(got["foreign"]), ["README.md", "summaries/s.md"])
        self.assertEqual(got["staged"], [])

    def test_stage_takes_engine_paths_only_including_deletions(self):
        os.remove(os.path.join(self.mirror, "workspace", ".keep"))
        with open(os.path.join(self.mirror, "README.md"), "a") as f:
            f.write("y\n")
        tree_status.stage(self.mirror)
        staged = subprocess.run(("git", "-C", self.mirror, "diff", "--cached", "--name-status"),
                                capture_output=True, text=True, check=True).stdout.split("\n")
        self.assertEqual([s for s in staged if s], ["D\tworkspace/.keep"])


if __name__ == "__main__":
    unittest.main()
