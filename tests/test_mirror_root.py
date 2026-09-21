"""`mirror_root.py` — where the mirror is, and the refusal when it is not there.

Every engine module derives its roots from this one answer, and every writer downstream
`makedirs` what it is handed, so the resolver's job is to refuse a wrong root with the
three facts a reader needs — what was tried, how it was arrived at, what to do — rather
than to return a best guess. Fully offline: the env file and its `env.d/` directory are
redirected into a temp dir, and the environment passed in explicitly.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import mirror_root  # noqa: E402


class Sandbox(unittest.TestCase):
    """A temp home: `env` and `env.d/` redirected there, a mirror directory to point at."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.env_file = os.path.join(self.home, ".config", "notion-mirror", "env")
        self.env_dir = os.path.join(self.home, ".config", "notion-mirror", "env.d")
        for attr, value in (("ENV_FILE", self.env_file), ("ENV_DIR", self.env_dir)):
            p = mock.patch.object(mirror_root, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.mirror = os.path.join(self.home, "mirror")
        os.makedirs(os.path.join(self.mirror, "workspace", "_databases"))

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)


class ConfigTest(Sandbox):
    """`config(key)`: the environment, else the last `KEY=VALUE` across the env files."""

    def test_the_environment_wins_over_the_file(self):
        self.write(self.env_file, "NOTION_MIRROR=/from/file\n")
        self.assertEqual(mirror_root.config("NOTION_MIRROR", {"NOTION_MIRROR": "/from/env"}),
                         "/from/env")

    def test_the_file_answers_when_the_environment_does_not(self):
        self.write(self.env_file, "NOTION_MIRROR=/from/file\n")
        self.assertEqual(mirror_root.config("NOTION_MIRROR", {}), "/from/file")

    def test_an_empty_environment_value_counts_as_unset(self):
        self.write(self.env_file, "NOTION_MIRROR=/from/file\n")
        self.assertEqual(mirror_root.config("NOTION_MIRROR", {"NOTION_MIRROR": ""}),
                         "/from/file")

    def test_nothing_configured_is_none(self):
        self.assertIsNone(mirror_root.config("NOTION_MIRROR", {}))

    def test_quotes_comments_and_export_are_tolerated(self):
        self.write(self.env_file,
                   "# the mirror\n"
                   "export NOTION_MIRROR='/quoted/single'\n"
                   "\n"
                   'NOTION_MIRROR_AUTOMATION_SUBTREES="Hub/Automations:Other/Logs"\n')
        self.assertEqual(mirror_root.config("NOTION_MIRROR", {}), "/quoted/single")
        self.assertEqual(mirror_root.config("NOTION_MIRROR_AUTOMATION_SUBTREES", {}),
                         "Hub/Automations:Other/Logs")

    def test_the_last_assignment_wins_within_a_file(self):
        self.write(self.env_file, "NOTION_MIRROR=/first\nNOTION_MIRROR=/last\n")
        self.assertEqual(mirror_root.config("NOTION_MIRROR", {}), "/last")

    def test_env_d_files_are_read_after_the_main_file_in_sorted_order(self):
        """`env.d/` is how a second deployer ships a value beside the box's own file; the
        later file wins, so the order has to be the one `ls` shows."""
        self.write(self.env_file, "KEY=main\n")
        self.write(os.path.join(self.env_dir, "20-second.env"), "KEY=second\n")
        self.write(os.path.join(self.env_dir, "10-first.env"), "KEY=first\n")
        self.write(os.path.join(self.env_dir, "notes.txt"), "KEY=ignored\n")
        self.assertEqual(mirror_root.config("KEY", {}), "second")

    def test_a_dangling_env_d_link_is_a_deployment_error_not_a_missing_file(self):
        """A deploy that links `env.d/org.env` before its target exists would otherwise
        make every key in that file read as unset, silently, and the nightly would run
        with no automation subtrees and no log line."""
        target = os.path.join(self.home, "not-deployed-yet.env")
        link = os.path.join(self.env_dir, "org.env")
        os.makedirs(self.env_dir)
        os.symlink(target, link)
        with self.assertRaises(mirror_root.MirrorError) as cm:
            mirror_root.config("NOTION_MIRROR_AUTOMATION_SUBTREES", {})
        msg = str(cm.exception)
        self.assertIn(link, msg)
        self.assertIn(target, msg)
        self.assertIn("re-run the deploy, or remove the link", msg)

    def test_an_env_d_file_does_not_unset_a_main_file_value_it_does_not_mention(self):
        self.write(self.env_file, "NOTION_MIRROR=/main\n")
        self.write(os.path.join(self.env_dir, "org.env"), "OTHER=x\n")
        self.assertEqual(mirror_root.config("NOTION_MIRROR", {}), "/main")


class MirrorDirTest(Sandbox):
    """`mirror_dir()`: the realpath of `NOTION_MIRROR`, asserting `workspace/_databases`."""

    def test_a_configured_mirror_resolves_to_its_realpath(self):
        link = os.path.join(self.home, "link")
        os.symlink(self.mirror, link)
        self.assertEqual(mirror_root.mirror_dir({"NOTION_MIRROR": link}),
                         os.path.realpath(self.mirror))

    def test_a_tilde_in_the_configured_value_means_the_home_directory(self):
        """The env file is hand-written and shell-shaped, so `NOTION_MIRROR=~/mirror` has
        to mean what it would mean to a shell."""
        with mock.patch.dict(os.environ, {"HOME": self.home}):
            self.assertEqual(mirror_root.mirror_dir({"NOTION_MIRROR": "~/mirror"}),
                             os.path.realpath(self.mirror))

    def test_the_file_is_read_when_the_environment_is_silent(self):
        self.write(self.env_file, f"NOTION_MIRROR={self.mirror}\n")
        self.assertEqual(mirror_root.mirror_dir({}), os.path.realpath(self.mirror))

    def test_a_directory_without_the_fingerprint_refuses_with_three_facts(self):
        wrong = os.path.join(self.home, "elsewhere")
        os.makedirs(os.path.join(wrong, "workspace"))
        with self.assertRaises(mirror_root.MirrorError) as cm:
            mirror_root.mirror_dir({"NOTION_MIRROR": wrong})
        msg = str(cm.exception)
        self.assertIn(os.path.realpath(wrong), msg)
        self.assertIn("resolved as: $NOTION_MIRROR", msg)
        self.assertIn("missing:     workspace/_databases", msg)
        self.assertIn("fix:", msg)
        self.assertIn("NOTION_MIRROR=", msg)

    def test_a_missing_directory_refuses_without_the_cold_start_hint(self):
        with self.assertRaises(mirror_root.MirrorError) as cm:
            mirror_root.mirror_dir({"NOTION_MIRROR": os.path.join(self.home, "absent")})
        self.assertNotIn("refresh.sh init", str(cm.exception))

    def test_an_existing_directory_without_databases_names_init(self):
        """A directory that exists but holds no mirror yet is a cold start, not a
        misconfiguration: the fix is `init`, not re-pointing a variable."""
        fresh = os.path.join(self.home, "fresh")
        os.makedirs(fresh)
        with self.assertRaises(mirror_root.MirrorError) as cm:
            mirror_root.mirror_dir({"NOTION_MIRROR": fresh})
        msg = str(cm.exception)
        self.assertIn("cold start", msg)
        self.assertIn("refresh.sh init", msg)

    def test_unset_everywhere_names_both_sources(self):
        with self.assertRaises(mirror_root.MirrorError) as cm:
            mirror_root.mirror_dir({})
        msg = str(cm.exception)
        self.assertIn("$NOTION_MIRROR", msg)
        self.assertIn(self.env_file, msg)
        self.assertIn("fix:", msg)

    def test_a_file_sourced_root_names_the_file_it_came_from(self):
        wrong = os.path.join(self.home, "elsewhere")
        os.makedirs(wrong)
        self.write(self.env_file, f"NOTION_MIRROR={wrong}\n")
        with self.assertRaises(mirror_root.MirrorError) as cm:
            mirror_root.mirror_dir({})
        self.assertIn(f"resolved as: NOTION_MIRROR in {self.env_file}", str(cm.exception))


class StateDirTest(Sandbox):
    def test_state_dir_is_meta_state_under_the_mirror(self):
        self.assertEqual(mirror_root.state_dir(self.mirror),
                         os.path.join(self.mirror, "_meta", "state"))

    def test_state_dir_resolves_the_mirror_when_given_no_root(self):
        with mock.patch.dict(os.environ, {"NOTION_MIRROR": self.mirror}):
            self.assertEqual(mirror_root.state_dir(),
                             os.path.join(os.path.realpath(self.mirror), "_meta", "state"))


class CliTest(Sandbox):
    """`python3 mirror_root.py [--unchecked]` — how `refresh.sh` gets the Python answer."""

    def run_cli(self, *args, env=None):
        base = {k: v for k, v in os.environ.items() if k != "NOTION_MIRROR"}
        base["HOME"] = self.home
        base.update(env or {})
        return subprocess.run([sys.executable, os.path.join(HERE, "mirror_root.py"), *args],
                              capture_output=True, text=True, env=base)

    def test_bare_prints_the_checked_mirror(self):
        res = self.run_cli(env={"NOTION_MIRROR": self.mirror})
        self.assertEqual((res.returncode, res.stdout.strip()),
                         (0, os.path.realpath(self.mirror)), res.stderr)

    def test_bare_refuses_a_cold_start_on_stderr_with_exit_2(self):
        fresh = os.path.join(self.home, "fresh")
        os.makedirs(fresh)
        res = self.run_cli(env={"NOTION_MIRROR": fresh})
        self.assertEqual(res.returncode, 2)
        self.assertEqual(res.stdout, "")
        self.assertIn("refresh.sh init", res.stderr)

    def test_unchecked_prints_the_configured_value_without_the_fingerprint(self):
        fresh = os.path.join(self.home, "fresh")
        res = self.run_cli("--unchecked", env={"NOTION_MIRROR": fresh})
        self.assertEqual((res.returncode, res.stdout.strip()), (0, fresh), res.stderr)

    def test_unchecked_still_refuses_when_nothing_is_configured(self):
        res = self.run_cli("--unchecked")
        self.assertEqual(res.returncode, 2)
        self.assertIn("$NOTION_MIRROR", res.stderr)

    def test_the_cli_reads_the_env_file_under_home(self):
        """The subprocess sees `HOME`, not the patched module attributes, so this is the
        one case that proves the file path is derived from the home directory."""
        self.write(os.path.join(self.home, ".config", "notion-mirror", "env"),
                   f"NOTION_MIRROR={self.mirror}\n")
        res = self.run_cli()
        self.assertEqual((res.returncode, res.stdout.strip()),
                         (0, os.path.realpath(self.mirror)), res.stderr)

    def test_an_unknown_argument_is_a_usage_error(self):
        res = self.run_cli("--mirror", env={"NOTION_MIRROR": self.mirror})
        self.assertEqual(res.returncode, 2)
        self.assertIn("usage", res.stderr)

    def test_importing_the_module_asserts_nothing(self):
        res = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {HERE!r}); import mirror_root; print('imported')"],
            capture_output=True, text=True,
            env={k: v for k, v in os.environ.items() if k != "NOTION_MIRROR"} | {"HOME": self.home})
        self.assertEqual((res.returncode, res.stdout.strip()), (0, "imported"), res.stderr)


if __name__ == "__main__":
    unittest.main()
