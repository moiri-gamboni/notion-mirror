"""Golden-fixture tests for refresh.py's block renderer.

The pre-existing `--mode validate` re-renders row property tables only; nothing
exercised `Walker.render`, so every renderer change was an unverified rewrite.
Each fixture is a real (or, where the workspace had no instance, hand-authored)
block payload; the expected file is its rendered form. Regenerate expected files
with `python3 capture_fixtures.py --expected` and review the diff — that diff is
the migration record for any renderer change.
"""
import unittest

# discovery starts inside tests/, so the package dir itself is on sys.path
from fixture_support import (dispatched_types, fixture_covers, fixture_names,
                             load_fixture, read_expected, render_fixture)

# the plan's minimum set, asserted independently of what render() happens to
# dispatch on today
REQUIRED = {
    "paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item",
    "numbered_list_item", "to_do", "toggle", "quote", "callout", "code",
    "divider", "equation", "table", "table_row", "image", "child_page",
    "child_database", "link_to_page", "bookmark", "table_of_contents",
    "template", "column_list", "synced_block",
}


class GoldenFixtures(unittest.TestCase):
    def test_fixtures_exist(self):
        self.assertTrue(fixture_names(), "no fixtures captured")

    def test_each_fixture_renders_to_its_expected_file(self):
        for name in fixture_names():
            with self.subTest(fixture=name):
                got, _walker, _api = render_fixture(load_fixture(name))
                self.assertEqual(read_expected(name), got)

    def test_to_do_renders_a_checkbox(self):
        """The canonical break: emitting a plain bullet here must fail."""
        got, _w, _a = render_fixture(load_fixture("to_do"))
        self.assertIn("- [x] ", got)
        self.assertIn("- [ ] ", got)

    def test_a_bookmark_keeps_its_caption(self):
        """The caption is content, and rendering the URL alone dropped it — from the
        mirror, which is read as a source in its own right, as well as from anything
        pushing the markdown back."""
        got, _w, _a = render_fixture(load_fixture("media"))
        self.assertIn("[the write-up](https://example.org/post)", got)

    def test_a_bookmark_an_embed_and_a_link_preview_are_distinguishable(self):
        """All three rendered as the same bare `[url](url)` — as did a link someone
        typed in a paragraph — so a reader could not tell an embedded video from a
        bookmark, and nothing reading the markdown back could either."""
        got, _w, _a = render_fixture(load_fixture("media"))
        marks = [line.split(" ")[1] for line in got.split("\n")
                 if line.startswith("- ") and "example.org/post" in line
                 or line.startswith("- ") and "example.org/embed" in line
                 or line.startswith("- ") and "repo/pull/1" in line]
        self.assertEqual(len(set(marks)), 3, f"not distinguishable: {marks}")

    def test_a_table_of_contents_and_a_template_are_marked_not_italicised(self):
        """`*(table of contents)*` and `*(template: …)*` read as ordinary italic prose
        — to someone reading the mirror, and to the parser, which absorbed them into a
        neighbouring paragraph or (inside a quote) turned them into a sibling quote
        while the real block was deleted with its parent."""
        toc, _w, _a = render_fixture(load_fixture("breadcrumb"))   # holds the TOC block
        self.assertTrue(toc.startswith("- "), toc)
        tpl, _w, _a = render_fixture(load_fixture("template"))
        self.assertTrue(tpl.startswith("- "), tpl)
        self.assertNotEqual(toc.split(" ")[1], tpl.split(" ")[1], "same marker for both")

    def test_fixture_mode_makes_no_live_calls(self):
        """FakeApi raises on anything but a fixture-served children fetch, so a
        green run is itself the proof; this pins the request count at the
        fixture-served children only."""
        served = 0
        for name in fixture_names():
            _got, _w, api = render_fixture(load_fixture(name))
            served += api.n
        self.assertGreater(served, 0, "no fixture exercises the child walk")


class SyntheticFixturesMatchTheirGenerator(unittest.TestCase):
    """`capture_fixtures.SYNTHETIC` is the source of the hand-authored fixtures, so a
    committed fixture that no longer matches it is a trap: `synthetic --force` is the
    documented way to regenerate, and it silently reverted a deliberate change to
    `row_mode_headings.json` — whose provenance line says why it is what it is."""

    def generator(self):
        import capture_fixtures
        return capture_fixtures

    def test_every_synthetic_fixture_matches_what_the_generator_would_write(self):
        gen = self.generator()
        for name, spec in gen.SYNTHETIC.items():
            with self.subTest(fixture=name):
                would_write = gen.strip_credentials(dict(spec, name=name))
                self.assertEqual(load_fixture(name), would_write,
                                 f"`synthetic --force` would rewrite {name}")


class DispatchCoverage(unittest.TestCase):
    def covered(self):
        out = set()
        for name in fixture_names():
            out |= fixture_covers(load_fixture(name))
        return out

    def test_minimum_set_is_covered(self):
        self.assertEqual(set(), REQUIRED - self.covered())

    def test_every_dispatched_type_is_covered(self):
        missing = dispatched_types() - self.covered()
        self.assertEqual(set(), missing,
                         f"render() branches on {sorted(missing)} with no fixture")

    def test_an_unknown_type_is_covered(self):
        got, _w, _a = render_fixture(load_fixture("unknown"))
        self.assertIn("<!-- unhandled block type:", got)


if __name__ == "__main__":
    unittest.main()
