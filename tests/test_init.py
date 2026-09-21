"""`refresh.sh init` — the cold-start bootstrap, and the refusal that names it.

A fresh install has a directory for the mirror but no `workspace/_databases` in it, so the
resolver refuses and every engine module refuses with it: there is nothing to refresh yet.
`init` is the one command that runs *before* the mirror exists — it takes the configured
path unchecked, makes the empty skeleton, and states the cost of the first refresh
honestly.

These run the real `refresh.sh` against a throwaway cold-start directory. `curl`/`logger`
are stubbed on PATH so an init that fails (the re-init refusal) records its ntfy attempt
rather than delivering one. The `paths`-import check uses the real engine modules — it is
the property that actually matters: an import that raised before init succeeds after it.
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REFRESH_SH = os.path.join(TOOLS, "refresh.sh")

STUB_RECORDER = '''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$SANDBOX_CALL_LOG.{name}"
exit 0
'''


def write_exec(path, text):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


class InitMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # A cold-start mirror: the directory exists, nothing is in it. This is exactly
        # the shape the resolver refuses and `init` exists to advance.
        self.mirror = os.path.join(self.tmp, "mirror")
        os.makedirs(self.mirror)
        self.home = os.path.join(self.tmp, "home")
        self.bin = os.path.join(self.tmp, "bin")
        for d in (self.home, self.bin):
            os.makedirs(d)
        for name in ("curl", "logger"):
            write_exec(os.path.join(self.bin, name), STUB_RECORDER.replace("{name}", name))
        self.call_log = os.path.join(self.tmp, "calls")

    def env(self, mirror=None):
        env = dict(os.environ, NOTION_MIRROR=mirror or self.mirror, HOME=self.home,
                   PATH=self.bin + os.pathsep + os.environ["PATH"],
                   SANDBOX_CALL_LOG=self.call_log)
        env.pop("NOTION_TOKEN", None)
        return env

    def run_init(self, mirror=None):
        return subprocess.run(("bash", REFRESH_SH, "init"), env=self.env(mirror),
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def paths_imports(self):
        """True iff `import paths` succeeds against this mirror, via the real engine."""
        res = subprocess.run((sys.executable, "-c", "import paths"), env=self.env(), cwd=TOOLS,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        return res.returncode == 0

    def test_init_creates_the_skeleton_and_states_the_cost(self):
        res = self.run_init()
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertTrue(os.path.isdir(os.path.join(self.mirror, "workspace", "_databases")))
        self.assertTrue(os.path.isdir(os.path.join(self.mirror, "_meta", "state")))
        # The next step, and a plain statement of what the first refresh costs: hours of
        # full discovery, resumable across nights under a budget, and not narrowable.
        self.assertIn("refresh.sh daily", res.stdout)
        self.assertIn("FULL discovery", res.stdout)
        self.assertIn("resumable", res.stdout)
        self.assertIn("NOTION_REFRESH_BUDGET", res.stdout)
        self.assertIn("--dbs cannot scope this", res.stdout)

    def test_init_refuses_when_the_mirror_already_exists(self):
        os.makedirs(os.path.join(self.mirror, "workspace", "_databases"))
        res = self.run_init()
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn("already initialised", res.stdout)

    def test_init_refuses_a_directory_that_does_not_exist(self):
        """A mistyped path must not become a mirror somewhere random: the directory (or
        the mount) has to be there first."""
        absent = os.path.join(self.tmp, "absent")
        res = self.run_init(mirror=absent)
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn("does not exist", res.stdout)
        self.assertFalse(os.path.exists(absent))

    def test_paths_imports_only_after_init(self):
        """The property that matters: a `paths` import raises on a cold start and
        succeeds once init has made the mirror fingerprint."""
        self.assertFalse(self.paths_imports(), "paths must refuse before the mirror exists")
        self.assertEqual(self.run_init().returncode, 0)
        self.assertTrue(self.paths_imports(), "paths must resolve after init")

    def test_the_cold_start_refusal_names_init(self):
        """The resolver's own message points a fresh directory at init, not at
        re-pointing an env var that is already correct."""
        res = subprocess.run((sys.executable, os.path.join(TOOLS, "mirror_root.py")),
                             env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn("refresh.sh init", res.stderr)
        self.assertIn("cold start", res.stderr)


if __name__ == "__main__":
    unittest.main()
