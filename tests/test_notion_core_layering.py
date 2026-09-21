"""The layering invariant: `notion_core` is the bottom of both toolchains.

`refresh.py` imports from `notion_core`, and so does tasksync. Nothing under
`notion_core/` may import back out — not `refresh.py`, not `paths.py`, not a sibling
engine script, not tasksync. Two things break the moment it does:

  * `paths` asserts the mirror volume at import, so a `notion_core` module that reached
    for it would make every tasksync verb — `ls` and `adopt` included — require the
    mirror to be mounted, which is exactly the coupling this package exists to remove.
  * an import of `refresh` would be a cycle (it imports this package at its top), and
    Python resolves a cycle by handing back a half-built module rather than raising, so
    the failure would land somewhere else entirely.

Checked by reading the source rather than by importing: an import that happens to work
today because some other module already ran is not the invariant, and a lazy import
inside a function would never fire in a test that only imports the package.
"""
import ast
import json
import os
import subprocess
import sys
import unittest

CORE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "notion_core")
ENGINE = os.path.dirname(CORE)


def core_modules():
    return sorted(f for f in os.listdir(CORE) if f.endswith(".py"))


def imports_of(path):
    """-> [(module, level)] for every import anywhere in the file, lazy ones included."""
    found = []
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(alias.name, 0) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            found.append((node.module or "", node.level))
    return found


class TestNotionCoreImportsNothingAbove(unittest.TestCase):
    def test_there_are_modules_to_check(self):
        """A glob that matched nothing would pass every test below vacuously."""
        self.assertGreater(len(core_modules()), 3, core_modules())

    def test_every_import_is_stdlib_or_this_package(self):
        for name in core_modules():
            for module, level in imports_of(os.path.join(CORE, name)):
                with self.subTest(module=f"notion_core/{name}", imports=module, level=level):
                    if level:
                        continue  # `from .sibling import x`
                    top = module.split(".")[0]
                    # assertTrue rather than assertIn: the failure message is the point
                    # here, and printing the whole stdlib set buries it.
                    self.assertTrue(
                        top in sys.stdlib_module_names or top == "notion_core",
                        f"notion_core/{name} imports {module!r}, which is neither stdlib nor "
                        f"part of notion_core. The package is the bottom layer of both the "
                        f"mirror engine and tasksync; whatever it needs has to move down "
                        f"into it.")

    def test_no_module_names_refresh_paths_or_tasksync(self):
        """Beyond imports: a module that merely *reads* a path out of `paths` or reaches
        for a tasksync file has the same coupling by another route."""
        for name in core_modules():
            with open(os.path.join(CORE, name), encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    code = line.split("#", 1)[0]
                    for banned in ("import refresh", "import paths", "import tasksync",
                                   "from tasksync", "notionlib"):
                        with self.subTest(module=name, line=lineno, banned=banned):
                            self.assertNotIn(banned, code)


class TestNotionCoreNeedsNoWorkspace(unittest.TestCase):
    def test_importing_every_module_without_a_mirror_succeeds(self):
        """The property tasksync's narrowed imports rest on: `notion_core` resolves with
        the engine directory on `sys.path` and nothing else — no mirror volume, no
        `NOTION_MIRROR`, no `paths` import. `refresh.py` in the same directory must
        stay unimported, which is what makes this a real check rather than a slow one.
        """
        script = (
            "import sys\n"
            f"sys.path.insert(0, {ENGINE!r})\n"
            f"for name in {[m[:-3] for m in core_modules() if m != '__init__.py']!r}:\n"
            "    __import__('notion_core.' + name)\n"
            "assert 'refresh' not in sys.modules, 'importing notion_core pulled in refresh.py'\n"
            "assert 'paths' not in sys.modules, 'importing notion_core pulled in paths.py'\n"
            "print('CLEAN')\n")
        env = {k: v for k, v in os.environ.items()
               if k not in ("NOTION_MIRROR", "NOTION_MIRROR_TOOLS")}
        env["NOTION_MIRROR"] = "/nonexistent-mirror-for-this-test"
        done = subprocess.run([sys.executable, "-c", script], capture_output=True,
                              text=True, timeout=120, env=env)
        self.assertEqual(done.stdout.strip(), "CLEAN", done.stderr)


class TestAutomationSubtreesComeFromConfig(unittest.TestCase):
    """`refresh.AUTOMATION_SUBTREES` is deployment configuration, read through the
    resolver's env-file reader, so a workspace names its machine-generated subtrees
    without editing the engine."""

    def subtrees(self, value):
        env = {k: v for k, v in os.environ.items() if k != "NOTION_MIRROR_AUTOMATION_SUBTREES"}
        if value is not None:
            env["NOTION_MIRROR_AUTOMATION_SUBTREES"] = value
        done = subprocess.run(
            [sys.executable, "-c",
             f"import sys, json; sys.path.insert(0, {ENGINE!r}); import refresh;"
             " print(json.dumps(refresh.AUTOMATION_SUBTREES))"],
            capture_output=True, text=True, timeout=120, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        return tuple(json.loads(done.stdout))

    def test_colon_separated_prefixes(self):
        self.assertEqual(self.subtrees("Hub/Automations:Other/Logs"),
                         ("Hub/Automations", "Other/Logs"))

    def test_unset_means_no_automation_subtrees(self):
        self.assertEqual(self.subtrees(None), ())

    def test_empty_segments_are_dropped(self):
        self.assertEqual(self.subtrees(":Hub/Automations::"), ("Hub/Automations",))


if __name__ == "__main__":
    unittest.main()
