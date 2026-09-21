"""The cron entrypoint's preflights (`refresh.sh`): the token, and the mirror itself.

Driven end to end through the two env knobs that exist for exactly this — `NOTION_MIRROR`
points the run at a throwaway mirror and `NOTION_MIRROR_TOOLS` at a stub engine beside a
copy of the resolver, so the refusal branches can be drilled without touching a real
mirror. Nothing here reaches Notion or a phone: `refresh.py` in the sandbox is a stub that
records the token it was handed, and `curl`/`logger` are stubbed on PATH, so an attempted
ntfy is recorded rather than delivered — which is also how the never-ntfy contract of rows
mode is asserted.

The sandbox has the shape of a real deployment, which is the part that carries meaning: an
engine directory (`mirror_root.py` beside the scripts) pointed at a mirror it does not
live inside.
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REFRESH_SH = os.path.join(TOOLS, "refresh.sh")

STUB_REFRESH = '''#!/usr/bin/env python3
"""Stands in for refresh.py: records whether the token it was handed is the one
the test planted, writes the rows report the wrapper reads, and exits clean."""
import json, os, sys
token = os.environ.get("NOTION_TOKEN", "")
expected = os.environ["SANDBOX_EXPECTED_TOKEN"]
with open(os.environ["SANDBOX_TOKEN_LOG"], "w") as f:
    f.write("match" if token == expected else "mismatch (len %d)" % len(token))
# The mirror, not this file's neighbourhood: the engine does not live in the tree
# it writes, which is the whole point of the split being tested here.
state = os.path.join(os.environ["NOTION_MIRROR"], "_meta", "state")
os.makedirs(state, exist_ok=True)
with open(os.path.join(state, "last-run-report.rows.json"), "w") as f:
    json.dump({"rows": {"requested": 0, "refreshed": []}, "props_probe": {},
               "requests": 0, "budget_exhausted": False}, f)
print(json.dumps({"changes": 0}))
'''

STUB_RECORDER = '''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$SANDBOX_CALL_LOG.{name}"
exit 1
'''


def write_exec(path, text):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


class Sandbox(unittest.TestCase):
    """A stub engine (with the resolver beside it) pointed at a throwaway mirror. No tests."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # The directory *around* the mirror: not a repository, as in production, where a
        # checkout that ignores the mirror directory may sit there.
        self.repo = os.path.join(self.tmp, "repo")
        self.mirror = os.path.join(self.repo, "mirror")
        self.home = os.path.join(self.tmp, "home")
        self.config = os.path.join(self.tmp, "config")
        self.bin = os.path.join(self.tmp, "bin")
        # The stub engine, in a directory of its own: `$NOTION_MIRROR_TOOLS` decides
        # where the wrapper reads its engine scripts from, and those scripts resolve the
        # mirror through the `mirror_root.py` beside them.
        self.tools = os.path.join(self.tmp, "clone")
        # `workspace/_databases` is the fingerprint the resolver asserts; without it this
        # sandbox would be refused, not run.
        for d in (self.tools,
                  os.path.join(self.mirror, "workspace", "_databases"),
                  os.path.join(self.mirror, "_meta", "state"),
                  self.home, self.config, self.bin):
            os.makedirs(d)

        write_exec(os.path.join(self.tools, "refresh.py"), STUB_REFRESH)
        # rows_status.py records the outcome; row_floor.py is the pre-commit row
        # check the wrapper runs on every mode, and it fails closed when absent —
        # a sandbox without it would refuse every run for the wrong reason. Both
        # import the resolver beside them.
        for name in ("rows_status.py", "row_floor.py", "mirror_root.py"):
            shutil.copy(os.path.join(TOOLS, name), os.path.join(self.tools, name))
        for name in ("curl", "logger"):
            write_exec(os.path.join(self.bin, name), STUB_RECORDER.replace("{name}", name))

        self.state = os.path.join(self.mirror, "_meta", "state")
        self.token_log = os.path.join(self.tmp, "token-seen")
        self.call_log = os.path.join(self.tmp, "calls")
        # The mirror is its own repository. row_floor diffs against HEAD, so the mirror
        # needs one; its runtime state is ignored, as in production.
        with open(os.path.join(self.mirror, "workspace", ".keep"), "w") as f:
            f.write("")
        with open(os.path.join(self.mirror, ".gitignore"), "w") as f:
            f.write("_meta/state/\n")
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "test")
        self.git("config", "commit.gpgsign", "false")
        self.git("add", "-A")
        self.git("commit", "-qm", "sandbox")

    def git(self, *args, cwd=None):
        return subprocess.run(("git", "-C", cwd or self.mirror) + args, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True).stdout

    def write_config(self, token):
        with open(os.path.join(self.config, ".claude.json"), "w") as f:
            json.dump({"mcpServers": {"notion": {"env": {"NOTION_TOKEN": token}}}}, f)

    def run_refresh(self, expected_token, inherited=None, args=("rows", "--props-only"),
                    mirror=None):
        env = dict(os.environ,
                   NOTION_MIRROR=self.mirror, NOTION_MIRROR_TOOLS=self.tools, HOME=self.home,
                   CLAUDE_CONFIG_DIR=self.config, PATH=self.bin + os.pathsep + os.environ["PATH"],
                   SANDBOX_EXPECTED_TOKEN=expected_token, SANDBOX_TOKEN_LOG=self.token_log,
                   SANDBOX_CALL_LOG=self.call_log)
        env.pop("NOTION_TOKEN", None)
        if mirror is not None:
            env["NOTION_MIRROR"] = mirror
        if inherited is not None:
            env["NOTION_TOKEN"] = inherited
        return subprocess.run(("bash", REFRESH_SH) + tuple(args), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def token_seen(self):
        if not os.path.exists(self.token_log):
            return None
        with open(self.token_log) as f:
            return f.read()

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


class TokenPreflight(Sandbox):
    """Which token the wrapper hands the engine, and how it refuses without one."""

    def test_an_inherited_token_is_used_instead_of_the_live_one(self):
        """A NOTION_MIRROR rehearsal points the script at a throwaway tree;
        deriving the live token here anyway would have it authenticate as
        production against that sandbox."""
        self.write_config("config-file-token")
        res = self.run_refresh("inherited-token", inherited="inherited-token")
        self.assertEqual(self.token_seen(), "match", res.stdout)

    def test_the_token_is_read_from_the_configured_claude_config_dir(self):
        self.write_config("config-file-token")
        res = self.run_refresh("config-file-token")
        self.assertEqual(self.token_seen(), "match", res.stdout)

    def test_rows_mode_records_an_unreadable_config_instead_of_ntfying(self):
        """rows mode runs hourly and never ntfys — every outcome goes to the
        status marker, which a health check's dead-man ages."""
        res = self.run_refresh("never-reached")  # no .claude.json written
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIsNone(self.token_seen(), "refresh.py must not run without a token")
        self.assertEqual(self.ntfy_attempts(), [], "rows mode must not ntfy")
        self.assertEqual(self.marker().get("last_attempt", {}).get("outcome"), "failed")

    def test_the_nightly_still_alerts_loudly_on_the_same_refusal(self):
        """Paired with the test above, which is otherwise vacuous: silence in rows
        mode would prove nothing if the notifier were broken. The same refusal in
        daily mode is a high-priority notification and no status marker."""
        res = self.run_refresh("never-reached", args=("daily",))
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertTrue(any("Priority: high" in c for c in self.ntfy_attempts()),
                        f"expected a high-priority ntfy, got {self.ntfy_attempts()}")
        self.assertEqual(self.marker(), {}, "the nightly does not write the rows marker")


class MirrorPreflight(Sandbox):
    """The refusal that has to happen before anything else does.

    A root that is not a mirror used to be unrepresentable — the script derived it from
    its own location — and is now the ordinary consequence of an unmounted volume, a
    plugin-cache copy, or a mistyped env var. Every writer downstream `makedirs` what it is
    handed, so the run has to stop here or it materialises an empty parallel mirror and
    commits it. Both volumes are asserted, because the two callers are different: the
    nightly is a person's incident, the hourly tick is a clock the dead-man reads.
    """

    def not_a_mirror(self):
        path = os.path.join(self.tmp, "somewhere-else")
        os.makedirs(path, exist_ok=True)
        return path

    def test_rows_mode_refuses_silently_and_writes_no_marker(self):
        """No ntfy (24 ticks a day) and no marker — the marker lives under the root that
        just refused to resolve. The 6h dead-man is the listener."""
        self.write_config("config-file-token")
        res = self.run_refresh("never-reached", mirror=self.not_a_mirror())
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("is not a Notion mirror", res.stdout)
        self.assertIn("missing:", res.stdout)
        self.assertIsNone(self.token_seen(), "refresh.py must not run at an unresolved root")
        self.assertEqual(self.ntfy_attempts(), [], "rows mode must not ntfy")
        self.assertEqual(self.marker(), {})

    def test_the_nightly_refuses_loudly_and_says_how_to_fix_it(self):
        """`fail()`, so the resolver's own message — resolved-as, missing, fix — is what
        reaches the operator rather than a shell error about a missing directory."""
        self.write_config("config-file-token")
        res = self.run_refresh("never-reached", args=("daily",), mirror=self.not_a_mirror())
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("FAIL:", res.stdout)
        self.assertIn("is not a Notion mirror", res.stdout)
        self.assertIn("NOTION_MIRROR=", res.stdout)
        self.assertIsNone(self.token_seen())
        self.assertEqual(self.marker(), {}, "the nightly does not write the rows marker")

    def test_nothing_configured_refuses_naming_both_sources(self):
        self.write_config("config-file-token")
        res = self.run_refresh("never-reached", args=("daily",), mirror="")
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("$NOTION_MIRROR", res.stdout)
        self.assertIn(os.path.join(self.home, ".config", "notion-mirror", "env"), res.stdout)
        self.assertIsNone(self.token_seen())


# Leaves a real content change in the mirror, so a rows run has something to commit.
STUB_REFRESH_TOUCH = STUB_REFRESH.replace(
    'print(json.dumps({"changes": 0}))',
    'open(os.path.join(os.environ["NOTION_MIRROR"], "workspace", "touched.txt"),'
    ' "w").write("x")\nprint(json.dumps({"changes": 1}))')


class MirrorRepoPreflight(Sandbox):
    """The mirror directory has to be a repository root of its own.

    Every commit the wrapper makes goes into the repository that contains the mirror. If
    that is a repository *around* the mirror (a checkout tracking the mirror directory),
    the refresh commits a mirror's worth of churn into it; the wrapper refuses instead,
    before the token is read and before the engine runs. Rows mode records the reason and
    stays silent; the nightly is loud.
    """

    def init_repo(self, cwd, message):
        for args in (("init", "-q"), ("config", "user.email", "test@example.invalid"),
                     ("config", "user.name", "test"), ("config", "commit.gpgsign", "false"),
                     ("add", "-A"), ("commit", "-qm", message)):
            self.git(*args, cwd=cwd)

    def fold_into_outer_repo(self):
        """The retired layout: one repository around the mirror, the mirror tracked in it."""
        shutil.rmtree(os.path.join(self.mirror, ".git"))
        self.init_repo(self.repo, "outer repository tracks the mirror")

    def test_rows_mode_records_the_refusal_and_runs_nothing(self):
        self.write_config("config-file-token")
        self.fold_into_outer_repo()
        res = self.run_refresh("never-reached")
        self.assertEqual(res.returncode, 1, res.stdout)
        attempt = self.marker().get("last_attempt", {})
        self.assertEqual((attempt.get("outcome"), attempt.get("reason")),
                         ("failed", "no_mirror_repo"), res.stdout)
        self.assertIn(self.mirror, attempt.get("detail", ""))
        self.assertIsNone(self.token_seen(),
                          "refresh.py must not run against a mirror that is not its own repository")
        self.assertEqual(self.ntfy_attempts(), [], "rows mode must not ntfy")

    def test_the_nightly_refuses_loudly(self):
        self.write_config("config-file-token")
        self.fold_into_outer_repo()
        res = self.run_refresh("never-reached", args=("daily",))
        self.assertEqual(res.returncode, 1, res.stdout)
        self.assertIn("FAIL:", res.stdout)
        self.assertIn(self.mirror, res.stdout)
        self.assertTrue(any("Priority: high" in c for c in self.ntfy_attempts()),
                        f"expected a high-priority ntfy, got {self.ntfy_attempts()}")
        self.assertIsNone(self.token_seen())
        self.assertEqual(self.marker(), {}, "the nightly does not write the rows marker")

    def test_the_commit_lands_in_the_mirror_and_a_repo_around_it_is_untouched(self):
        """A checkout that ignores the mirror directory may sit around it, and the mirror
        is a repository of its own. The refresh commits into the latter and never stages
        the former."""
        write_exec(os.path.join(self.tools, "refresh.py"), STUB_REFRESH_TOUCH)
        with open(os.path.join(self.repo, ".gitignore"), "w") as f:
            f.write("/mirror/\n")
        self.init_repo(self.repo, "outer repository")
        outer_before = self.git("rev-parse", "HEAD", cwd=self.repo)
        inner_before = self.git("rev-parse", "HEAD")
        self.write_config("config-file-token")
        res = self.run_refresh("config-file-token")
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertNotEqual(self.git("rev-parse", "HEAD"), inner_before, "no commit in the mirror")
        self.assertEqual(self.git("status", "--porcelain"), "",
                         "the mirror must be clean after the commit")
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=self.repo), outer_before)
        self.assertEqual(self.git("status", "--porcelain", cwd=self.repo), "")


if __name__ == "__main__":
    unittest.main()
