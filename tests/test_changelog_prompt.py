"""The changelog prompt is built by one helper (`changelog_prompt`) at both call sites —
the nightly `daily` run and `reanalyze <sha>` — and both append the per-installation
reader context (`~/.config/notion-mirror/changelog-context.md`) when it exists, falling
back to the generic prompt when it does not.

Driven through the real `refresh.sh` with a stubbed `claude` that records the prompt it is
handed. `NOTION_MIRROR_CHANGELOG_CONTEXT` points the context lookup at a sandbox file so
the test never reads or writes the real `~/.config` path. `NOTION_MIRROR_TOOLS` is a stub
engine (a fake `refresh.py` that reports one change and writes the report the wrapper
reads), beside the real `changelog-prompt.md`, `row_floor.py`, `rows_status.py` and
`mirror_root.py`.
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

# Reports one DB change and leaves a dirty mirror tree so the wrapper reaches the
# changelog analysis and then commits. Writes both report files the wrapper reads.
STUB_REFRESH = '''#!/usr/bin/env python3
import json, os
mirror = os.environ["NOTION_MIRROR"]
state = os.path.join(mirror, "_meta", "state")
os.makedirs(state, exist_ok=True)
report = {"dbs": {"changed": [], "new": [], "deleted": []},
          "pages": {"new": [], "changed": [], "deleted": []},
          "comments": {}, "requests": 1, "duration_s": 60, "budget_exhausted": False}
for name in ("last-run-report.json",):
    with open(os.path.join(state, name), "w") as f:
        json.dump(report, f)
with open(os.path.join(state, "last-run-report.md"), "w") as f:
    f.write("stub report\\n")
# a real content change so the tree is dirty and the commit is non-empty
with open(os.path.join(mirror, "workspace", "changed.txt"), "w") as f:
    f.write("changed\\n")
print(json.dumps({"changes": 1}))
'''

# Records the -p prompt it was handed and the directory it was run from, then emits a
# minimal valid note (starts with #).
STUB_CLAUDE = '''#!/usr/bin/env bash
prompt=""
while [ $# -gt 0 ]; do
    if [ "$1" = "-p" ]; then shift; prompt="$1"; fi
    shift
done
printf '%s' "$prompt" > "$CLAUDE_PROMPT_LOG"
pwd -P > "$CLAUDE_PROMPT_LOG.cwd"
printf '# stub note\\n'
'''

STUB_RECORDER = '''#!/usr/bin/env bash
exit 0
'''


def write_exec(path, text):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ndir = os.path.join(self.tmp, "mirror")
        self.home = os.path.join(self.tmp, "home")
        self.config = os.path.join(self.tmp, "config")
        self.bin = os.path.join(self.tmp, "bin")
        self.tools = os.path.join(self.tmp, "clone")
        for d in (self.tools,
                  os.path.join(self.ndir, "workspace", "_databases"),
                  os.path.join(self.ndir, "_meta", "state"),
                  os.path.join(self.ndir, "_meta", "changelog"),
                  self.home, self.config, self.bin):
            os.makedirs(d)
        write_exec(os.path.join(self.tools, "refresh.py"), STUB_REFRESH)
        for name in ("rows_status.py", "row_floor.py", "changelog-prompt.md", "mirror_root.py"):
            shutil.copy(os.path.join(TOOLS, name), os.path.join(self.tools, name))
        write_exec(os.path.join(self.bin, "claude"), STUB_CLAUDE)
        for name in ("curl", "logger"):
            write_exec(os.path.join(self.bin, name), STUB_RECORDER)
        with open(os.path.join(self.config, ".claude.json"), "w") as f:
            json.dump({"mcpServers": {"notion": {"env": {"NOTION_TOKEN": "t"}}}}, f)
        self.prompt_log = os.path.join(self.tmp, "prompt")
        self.context_file = os.path.join(self.tmp, "changelog-context.md")

        # The mirror is its own repository. row_floor diffs against HEAD, so the mirror
        # needs one, and its state dir is ignored.
        with open(os.path.join(self.ndir, "workspace", ".keep"), "w") as f:
            f.write("")
        with open(os.path.join(self.ndir, ".gitignore"), "w") as f:
            f.write("_meta/state/\n")
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "test")
        self.git("config", "commit.gpgsign", "false")
        self.git("add", "-A")
        self.git("commit", "-qm", "seed")

    def git(self, *args):
        subprocess.run(("git", "-C", self.ndir) + args, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def run_refresh(self, *args, context=False):
        env = dict(os.environ, NOTION_MIRROR=self.ndir, NOTION_MIRROR_TOOLS=self.tools,
                   HOME=self.home, CLAUDE_CONFIG_DIR=self.config,
                   PATH=self.bin + os.pathsep + os.environ["PATH"],
                   CLAUDE_PROMPT_LOG=self.prompt_log,
                   NOTION_MIRROR_CHANGELOG_CONTEXT=self.context_file)
        env.pop("NOTION_TOKEN", None)
        if context:
            with open(self.context_file, "w") as f:
                f.write(READER_CONTEXT)
        return subprocess.run(("bash", REFRESH_SH) + args, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def prompt(self):
        with open(self.prompt_log) as f:
            return f.read()

    def claude_cwd(self):
        with open(self.prompt_log + ".cwd") as f:
            return f.read().strip()

    def seed_reanalyze_note(self):
        """reanalyze replaces the changelog note for a committed refresh's date."""
        date = subprocess.run(("git", "-C", self.ndir, "show", "-s", "--format=%cs", "HEAD"),
                              capture_output=True, text=True, check=True).stdout.strip()
        note = os.path.join(self.ndir, "_meta", "changelog", f"{date}.md")
        with open(note, "w") as f:
            f.write("# old stub note\n\nmachine report here\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "note")
        return subprocess.run(("git", "-C", self.ndir, "rev-parse", "HEAD"),
                              capture_output=True, text=True, check=True).stdout.strip()


READER_CONTEXT = "LANE MARKER: this reader owns the widget pipeline and the frob DB.\n"
BASE_MARKER = "Notion mirror changelog analysis"   # first line of the real prompt file


class DailyCallSite(Sandbox):
    def test_generic_when_no_context_file(self):
        res = self.run_refresh("daily")
        self.assertEqual(res.returncode, 0, res.stdout)
        p = self.prompt()
        self.assertIn(BASE_MARKER, p)
        self.assertNotIn("LANE MARKER", p)
        self.assertNotIn("Reader context", p)

    def test_appends_context_when_present(self):
        res = self.run_refresh("daily", context=True)
        self.assertEqual(res.returncode, 0, res.stdout)
        p = self.prompt()
        self.assertIn(BASE_MARKER, p)
        self.assertIn("LANE MARKER", p)
        self.assertIn("Reader context", p)


class MirrorRepoIsTheCwd(Sandbox):
    """The prompt's paths and git commands are relative to the mirror repository, so
    that is where `claude -p` has to run — at both call sites. A prompt written for a
    directory around the mirror would send the model to paths that do not exist under
    the mirror, and `git diff --cached` there is the mirror's staged diff only when the
    mirror is the cwd."""

    def test_the_nightly_runs_the_analysis_inside_the_mirror(self):
        res = self.run_refresh("daily")
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertEqual(self.claude_cwd(), os.path.realpath(self.ndir))

    def test_reanalyze_runs_the_analysis_inside_the_mirror(self):
        sha = self.seed_reanalyze_note()
        res = self.run_refresh("reanalyze", sha)
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertEqual(self.claude_cwd(), os.path.realpath(self.ndir))

    def test_the_prompt_addresses_the_mirror_repo(self):
        res = self.run_refresh("daily")
        self.assertEqual(res.returncode, 0, res.stdout)
        p = self.prompt()
        self.assertIn("`_meta/state/last-run-report.md`", p)
        self.assertIn("`git diff --cached --stat`", p)
        self.assertIn("../tasks/.sync/ls.md", p)
        self.assertNotIn("notion/_meta", p)
        self.assertNotIn("-- notion/", p)


class ReanalyzeCallSite(Sandbox):
    def test_generic_when_no_context_file(self):
        sha = self.seed_reanalyze_note()
        res = self.run_refresh("reanalyze", sha)
        self.assertEqual(res.returncode, 0, res.stdout)
        p = self.prompt()
        self.assertIn(BASE_MARKER, p)
        self.assertNotIn("LANE MARKER", p)

    def test_appends_context_when_present(self):
        sha = self.seed_reanalyze_note()
        res = self.run_refresh("reanalyze", sha, context=True)
        self.assertEqual(res.returncode, 0, res.stdout)
        p = self.prompt()
        self.assertIn(BASE_MARKER, p)
        self.assertIn("LANE MARKER", p)
        self.assertIn("Reader context", p)


class ReanalyzeAndTheDeferredDigest(Sandbox):
    """A run started with NOTION_REFRESH_DEFER_NTFY leaves its digest queued for the
    next `notify`. When that run's note was a stub and `reanalyze` replaces it, the
    reanalysis sends the digest itself — so the queued entry for the same note has
    already been delivered, and sending it again is a duplicate of a note that no
    longer says what the queue recorded."""

    def pending(self):
        return os.path.join(self.ndir, "_meta", "state", "pending-ntfy.tsv")

    def queue(self, note):
        with open(self.pending(), "w") as f:
            f.write(f"{note}\tone db changed\t_meta/changelog/x.md\n")

    def note_path(self, sha):
        date = subprocess.run(("git", "-C", self.ndir, "show", "-s", "--format=%cs", sha),
                              capture_output=True, text=True, check=True).stdout.strip()
        return os.path.join(os.path.realpath(self.ndir), "_meta", "changelog", f"{date}.md")

    def test_a_queued_digest_for_the_reanalyzed_note_is_dropped(self):
        sha = self.seed_reanalyze_note()
        self.queue(self.note_path(sha))
        res = self.run_refresh("reanalyze", sha)
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertFalse(os.path.exists(self.pending()),
                         "the digest was sent by the reanalysis; the queued copy would send it twice")

    def test_a_queued_digest_for_another_note_survives(self):
        sha = self.seed_reanalyze_note()
        other = os.path.join(os.path.realpath(self.ndir), "_meta", "changelog", "1999-12-31.md")
        self.queue(other)
        res = self.run_refresh("reanalyze", sha)
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertTrue(os.path.exists(self.pending()),
                        "an unrelated run's digest is still owed")


if __name__ == "__main__":
    unittest.main()
