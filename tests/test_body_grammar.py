"""Row bodies at indent 0, with explicit body/comment delimiters.

Three things are under test, all offline (a fake API serves block children):

* `refresh.probe_row` walks a row body at indent 0 and wraps both enrichment
  regions in delimiters.
* the region-resolving call sites keep resolving — including the case the
  delimiters exist for, a body that itself contains a `## Comments` heading.
* `db_probe_policy` still classifies a directory through the format change, on a
  mix of migrated and legacy files.

The rows written before this shape were normalised by a one-shot migration that
ran to completion and was removed afterwards; see `README.md` § Row-body grammar
for the shape it produced.
"""
import os
import shutil
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import refresh  # noqa: E402

ROW = "aa" * 16
DB_ID = "bb" * 16


class FakeApi:
    """Canned /blocks/{id}/children; /comments returns the canned comment list."""

    def __init__(self, children, comments=None):
        self.children = {refresh.undash(k): v for k, v in children.items()}
        self.comments = comments or {}
        self.n = 0

    def paginate(self, method, path, params=None, body=None, ver=None):
        self.n += 1
        if path == "/comments":
            return list(self.comments.get(refresh.undash(params["block_id"]), []))
        return list(self.children.get(refresh.undash(path.split("/")[2]), []))


def para(bid, text, has_children=False):
    return {"id": bid, "type": "paragraph", "has_children": has_children,
            "paragraph": {"rich_text": [{"type": "text", "plain_text": text,
                                         "annotations": {}}]}}


def bullet(bid, text, has_children=False):
    return {"id": bid, "type": "bulleted_list_item", "has_children": has_children,
            "bulleted_list_item": {"rich_text": [{"type": "text", "plain_text": text,
                                                  "annotations": {}}]}}


def heading(bid, text, has_children=False):
    return {"id": bid, "type": "heading_2", "has_children": has_children,
            "heading_2": {"rich_text": [{"type": "text", "plain_text": text,
                                         "annotations": {}}]}}


def comment(text, when="2026-07-01T00:00:00.000Z"):
    return {"rich_text": [{"type": "text", "plain_text": text, "annotations": {}}],
            "created_by": {"id": "u"}, "created_time": when}


USERS = types.SimpleNamespace(name=lambda ref: "Someone")


def probe(children, comments=None):
    api = FakeApi(children, comments)
    report = refresh.new_report("test")
    d = tempfile.mkdtemp(prefix="a3-probe-")
    try:
        enrichment, _capped = refresh.probe_row(api, USERS, refresh.dashed(ROW), d, report)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return enrichment


NESTED = {ROW: [bullet("c" * 32, "outer", has_children=True), para("d" * 32, "flat")],
          "c" * 32: [bullet("e" * 32, "inner")]}


class ProbeRowShape(unittest.TestCase):

    def test_the_body_starts_at_column_zero(self):
        enrichment = probe(NESTED)
        lines = enrichment.split("\n")
        self.assertIn("- outer", lines)      # not "  - outer"
        self.assertIn("  - inner", lines)    # one level of real nesting
        self.assertIn("flat", lines)

    def test_leading_spaces_now_mean_depth_and_nothing_else(self):
        # The property the mirror dialect rests on, and what B3's converter
        # reads back: depth == leading spaces / 2, top level == 0.
        for ln in probe(NESTED).split("\n"):
            if ln.strip() and not ln.startswith("<!--") and not ln.startswith("##"):
                self.assertEqual((len(ln) - len(ln.lstrip(" "))) % 2, 0, ln)

    def test_both_regions_are_delimited(self):
        enrichment = probe(NESTED, {ROW: [comment("hello")]})
        self.assertIn("## Body\n\n" + refresh.BODY_OPEN + "\n", enrichment)
        self.assertIn("\n" + refresh.BODY_CLOSE + "\n", enrichment)
        self.assertIn("## Comments\n\n" + refresh.COMMENTS_OPEN + "\n", enrichment)
        self.assertTrue(enrichment.rstrip("\n").endswith(refresh.COMMENTS_CLOSE))

    def test_a_body_only_row_gets_no_comment_delimiters(self):
        enrichment = probe(NESTED)
        self.assertIn(refresh.BODY_OPEN, enrichment)
        self.assertNotIn(refresh.COMMENTS_OPEN, enrichment)

    def test_a_probe_failure_annotation_stays_outside_the_body_region(self):
        # A2 places it under the first heading; with delimiters that is above
        # BODY_OPEN, so it can never be read back as a body line.
        fresh = probe(NESTED, {ROW: [comment("hello")]})
        marked = refresh.annotate_probe_failure(fresh, "2026-08-06")
        self.assertIn("## Body\n\n_[probe failed 2026-08-06]_\n\n" + refresh.BODY_OPEN, marked)
        self.assertEqual(refresh.strip_probe_annotation(marked), fresh)
        self.assertNotIn("probe failed", refresh.extract_comments_body(marked))

    def test_the_headings_are_still_there(self):
        # Additive, not a replacement: the mirror is read by humans too.
        enrichment = probe(NESTED, {ROW: [comment("hello")]})
        self.assertIn("\n## Body\n", enrichment)
        self.assertIn("\n## Comments\n", enrichment)


class RegionResolution(unittest.TestCase):
    """The reason the delimiters land in the same commit as the dedent."""

    # A body carrying the enrichment's own heading text. At indent 1 this
    # rendered as "  ## Comments" and could not collide; at indent 0 it is
    # byte-identical to the real heading.
    COLLIDING = probe({ROW: [heading("f" * 32, "Comments"), para("0f" * 16, "body prose")]},
                      {ROW: [comment("the real comment")]})

    def test_a_body_heading_cannot_hijack_the_comment_slice(self):
        self.assertIn("\n## Comments\n", self.COLLIDING.split(refresh.COMMENTS_OPEN)[0])
        got = refresh.extract_comments_body(self.COLLIDING)
        self.assertIn("the real comment", got)
        self.assertNotIn("body prose", got)

    def test_the_pre_delimiter_regex_would_have_got_it_wrong(self):
        # Pins the bug rather than trusting the fix: the old heading-first rule,
        # run on this exact text, swallows the body.
        import re
        m = re.search(r"^## Comments\n(.*)\Z", self.COLLIDING, re.S | re.M)
        self.assertIn("body prose", m.group(1))

    def test_has_comments_is_not_fooled_either(self):
        body_only = probe({ROW: [heading("f" * 32, "Comments"), para("0f" * 16, "x")]})
        self.assertFalse(refresh.has_comments(body_only))
        self.assertTrue(refresh.has_comments(self.COLLIDING))

    def test_legacy_undelimited_files_still_resolve(self):
        legacy = "\n\n## Body\n\n  a stored line\n\n## Comments\n\n- _A (2026-01-01):_ hi\n"
        self.assertTrue(refresh.has_comments(legacy))
        self.assertEqual(refresh.extract_comments_body(legacy).strip(),
                         "- _A (2026-01-01):_ hi")
        self.assertTrue(refresh.has_enrichment(legacy))

    def test_the_repair_scripts_rebuild_a_delimited_file_to_the_renderer_shape(self):
        # merge_backfill and repair_backfill_attribution both rebuild the comment
        # region as `head + MARKER + strip_comments_section(tail) + comments_section(...)`.
        # On a delimited file a heading-only strip leaves the close delimiter
        # behind and the next read runs past the region.
        src = ("| Name | x |\n" + refresh.MARKER + refresh.body_section("- a body line")
               + refresh.comments_section(["- _A (2026-01-01):_ hi"]) + "\n")
        head, _, tail = src.partition(refresh.MARKER)
        merged = ["- _A (2026-01-01):_ hi", "- _B (2026-02-02):_ new"]
        out = (head + refresh.MARKER + refresh.strip_comments_section(tail).rstrip("\n")
               + refresh.comments_section(merged) + "\n")
        self.assertNotIn(refresh.COMMENTS_CLOSE, refresh.strip_comments_section(tail))
        self.assertIn(refresh.BODY_CLOSE, out)
        self.assertEqual((out.count(refresh.COMMENTS_OPEN), out.count("## Comments")), (1, 1))
        self.assertEqual(refresh.split_bullets(refresh.extract_comments_body(out),
                                               prefix="- _"), merged)

    def test_strip_and_rebuild_round_trip(self):
        # What the two backfill repair scripts do to a file.
        for src in (self.COLLIDING,
                    "\n\n## Body\n\n  a stored line\n\n## Comments\n\n- _A (2026-01-01):_ hi\n"):
            with self.subTest(src=src[:24]):
                bullets = refresh.split_bullets(refresh.extract_comments_body(src), prefix="- _")
                rebuilt = (refresh.strip_comments_section(src).rstrip("\n")
                           + refresh.comments_section(bullets) + "\n")
                self.assertEqual(refresh.split_bullets(
                    refresh.extract_comments_body(rebuilt), prefix="- _"), bullets)
                self.assertNotIn(refresh.COMMENTS_CLOSE,
                                 refresh.strip_comments_section(src))


class ProbePolicyStillClassifies(unittest.TestCase):
    """`db_probe_policy` misreading a big DB flips it to enriched-only probing,
    silently, cached seven days. The canonical Tasks DB is 49% enriched over
    2,565 rows, so it must stay "all" — through the format change and on a mix
    of migrated and not-yet-migrated files."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="a3-policy-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def write(self, n, enrichment):
        with open(os.path.join(self.dir, f"Row {n} {n:032x}.md"), "w") as f:
            f.write("| Name | x |\n" + refresh.MARKER + enrichment)

    def test_a_tasks_shaped_directory_stays_all(self):
        delimited = refresh.body_section("a line") + "\n"
        legacy = "\n\n## Body\n\n  a line\n"
        for n in range(2565):
            self.write(n, "" if n % 2 else (delimited if n % 4 == 0 else legacy))
        self.assertEqual(
            refresh.db_probe_policy(self.dir, 2565, {"probe_policy": {}}, DB_ID), "all")

    def test_a_feed_shaped_directory_stays_enriched(self):
        for n in range(400):
            self.write(n, refresh.body_section("a line") + "\n" if n < 5 else "")
        self.assertEqual(
            refresh.db_probe_policy(self.dir, 400, {"probe_policy": {}}, DB_ID), "enriched")


if __name__ == "__main__":
    unittest.main()
