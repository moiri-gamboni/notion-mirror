"""Comment-contamination assert: one comment text may not be attributed to
more than `MAX_COMMENT_PAGES` pages across the whole mirror.

Closes the "still open" item of the 2026-07-27 backfill-misattribution
incident, where one discussion was copied onto up to 2,287 pages because
`loadPageChunk` returns ancestor context and nothing filtered it out.

No network: every test builds a synthetic index or a temp mirror on disk.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import refresh  # noqa: E402  (_tools is not a package; discover's top dir is tests/)


def index(**spec):
    """{"text": n_pages} -> the text -> {page ids} shape the assert consumes."""
    return {text: {f"{i:032x}" for i in range(n)} for text, n in spec.items()}


class ThresholdTest(unittest.TestCase):
    """The constant separates genuine repetition from the incident's signature."""

    def test_three_pages_passes(self):
        self.assertEqual(refresh.contamination_breaches(index(**{"a real comment": 3})), [])

    def test_the_observed_contamination_floor_fails(self):
        """62 pages is the lowest spread of the 28 texts the 2026-07-27 incident
        left behind — the bottom of the range this assert has to catch."""
        breaches = refresh.contamination_breaches(index(**{"a foreign thread": 62}))
        self.assertEqual([(b.text, b.pages) for b in breaches], [("a foreign thread", 62)])

    def test_benign_twentysix_page_boilerplate_passes(self):
        """The measured benign maximum: judging-criteria boilerplate posted on
        26 hackathon project rows of one database."""
        self.assertEqual(refresh.contamination_breaches(index(**{"Submissions will be judged": 26})), [])

    def test_the_next_hackathons_judging_boilerplate_fires(self):
        """The known cost of running at 30 rather than 45, pinned so it stays a
        decision rather than a surprise. The benign top case is judging
        boilerplate on 26 project rows, and events vary in size — one had 106
        project rows — so the next one posting the same comment onto 40 rows
        fires, stops the commit, and wedges the mirror on its own dirty-tree
        preflight until a human clears it. A baseline cannot pre-accept it: the
        offending text is a future, different one. The remedy when it happens is
        to raise the constant back to 45, not to baseline the text."""
        self.assertEqual(len(refresh.contamination_breaches(index(**{"Submissions will be judged": 40}))), 1)

    def test_boundary_is_the_constant_itself(self):
        m = refresh.MAX_COMMENT_PAGES
        self.assertEqual(refresh.contamination_breaches(index(**{"x": m})), [])
        self.assertEqual(len(refresh.contamination_breaches(index(**{"x": m + 1}))), 1)

    def test_constant_clears_measured_benign_repetition(self):
        """Guard the calibration itself. Genuine repetition tops out at 26 pages
        (judging boilerplate on 26 rows of one database) and, since the
        2026-08-17 un-merge of the 28 contaminated texts, nothing in the corpus
        lands above it at all. The constant has to clear that benign maximum —
        that is the side whose false positive stops the mirror — and stay below
        the 62 floor the incident's own mechanism produced, so a recurrence is
        still caught."""
        self.assertGreater(refresh.MAX_COMMENT_PAGES, 26)
        self.assertLess(refresh.MAX_COMMENT_PAGES, 62)

    def test_message_names_the_text_prefix_and_the_page_count(self):
        text = "a foreign thread that was copied everywhere " * 4
        msg = refresh.contamination_message(refresh.contamination_breaches(index(**{text: 99})))
        self.assertIn("99", msg)
        self.assertIn(text[:40], msg)

    def test_breaches_are_reported_worst_first(self):
        breaches = refresh.contamination_breaches(index(**{"small": 62, "big": 300, "mid": 80}))
        self.assertEqual([b.text for b in breaches], ["big", "mid", "small"])


class KeyTest(unittest.TestCase):
    """Both mirror tiers reduce to the same comparable comment text."""

    def test_row_and_page_bullets_reduce_to_the_same_key(self):
        row = "- _Sam (2024-01-09):_ moved the page and tagged it"
        page = '- **on** "Example Hub" — Sam (2024-01-09): moved the page and tagged it'
        self.assertEqual(refresh.comment_text_key(row), "moved the page and tagged it")
        self.assertEqual(refresh.comment_text_key(page), refresh.comment_text_key(row))

    def test_resolved_annotation_is_not_part_of_the_key(self):
        plain = "- _Sam (2024-01-09):_ moved the page"
        marked = "- _Sam (2024-01-09):_ moved the page _[resolved/deleted ≤2026-07-20]_"
        self.assertEqual(refresh.comment_text_key(marked), refresh.comment_text_key(plain))

    def test_continuation_lines_join_into_one_key(self):
        bullet = "- _Sam (2024-01-09):_ first line\n  second line"
        self.assertEqual(refresh.comment_text_key(bullet), "first line second line")

    def test_mention_only_comments_are_excluded(self):
        """The mirror renders every mention as '‣', so distinct one-mention
        comments collapse to one key — a normalisation artifact, not spread."""
        self.assertEqual(refresh.comment_text_key("- _Sam (2024-01-09):_ ‣"), "")
        self.assertEqual(refresh.comment_text_key("- _Sam (2024-01-09):_ ‣ ‣"), "")

    def test_a_mention_beside_real_words_still_keys(self):
        self.assertEqual(refresh.comment_text_key("- _Sam (2024-01-09):_ moved ‣ to ‣"),
                         "moved ‣ to ‣")


class IndexTest(unittest.TestCase):
    """The index covers the whole mirror: DB rows and content pages alike."""

    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="a4-index-")
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        self.dbs = os.path.join(self.ws, "_databases")
        os.makedirs(self.dbs)
        for name, ws in (("WS", self.ws), ("DBS", self.dbs)):
            patched, orig = ws, getattr(refresh, name)
            setattr(refresh, name, patched)
            self.addCleanup(setattr, refresh, name, orig)

    def row(self, db, rid, bullets):
        d = os.path.join(self.dbs, db)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"Row {rid}.md"), "w") as f:
            f.write("| Name | x |\n" + refresh.MARKER + "\n\n## Comments\n\n" + "\n".join(bullets) + "\n")

    def comments_md(self, sections):
        with open(os.path.join(self.ws, "_comments.md"), "w") as f:
            f.write("# Notion comments (content pages)\n\n")
            for pid, title, bullets in sections:
                f.write(f"## {title}  `{pid}`\n\n" + "\n".join(bullets) + "\n\n")

    def test_row_tier_is_indexed_by_row_id(self):
        self.row("Tasks aa", "1" * 32, ["- _A (2026-01-01):_ hello there"])
        self.row("Tasks aa", "2" * 32, ["- _B (2026-01-02):_ hello there"])
        idx = refresh.build_comment_index()
        self.assertEqual(idx["hello there"], {"1" * 32, "2" * 32})

    def test_page_tier_is_indexed_by_section_id(self):
        self.comments_md([("3" * 32, "Some Page", ['- **on** "(page-level)" — A (2026-01-01): hello there'])])
        self.assertEqual(refresh.build_comment_index()["hello there"], {"3" * 32})

    def test_both_tiers_share_one_index(self):
        self.row("Tasks aa", "1" * 32, ["- _A (2026-01-01):_ shared text"])
        self.comments_md([("3" * 32, "Some Page", ['- **on** "block" — A (2026-01-01): shared text'])])
        self.assertEqual(refresh.build_comment_index()["shared text"], {"1" * 32, "3" * 32})

    def test_repeats_within_one_page_count_once(self):
        """Within-page duplication is a separate, pre-existing wart (a comment
        stored both plain and mention-expanded); it is not misattribution."""
        self.row("Tasks aa", "1" * 32,
                 ["- _A (2026-01-01):_ said twice", "- _A (2026-01-01):_ said twice"])
        self.assertEqual(refresh.build_comment_index()["said twice"], {"1" * 32})

    def test_schema_files_and_bodies_are_not_comments(self):
        d = os.path.join(self.dbs, "Tasks aa")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "_schema.md"), "w") as f:
            f.write(refresh.MARKER + "\n\n## Comments\n\n- _A (2026-01-01):_ not a row\n")
        self.row("Tasks aa", "1" * 32, [])
        with open(os.path.join(d, f"Row {'1' * 32}.md"), "w") as f:
            f.write(refresh.MARKER + "\n\n## Body\n\n- _A (2026-01-01):_ body text not a comment\n")
        idx = refresh.build_comment_index()
        self.assertEqual(idx, {})

    def test_missing_comments_md_is_not_an_error(self):
        self.row("Tasks aa", "1" * 32, ["- _A (2026-01-01):_ only tier"])
        self.assertEqual(refresh.build_comment_index()["only tier"], {"1" * 32})

    def test_end_to_end_incident_shape_is_caught(self):
        """One thread on 50 unrelated content pages — the incident, in miniature."""
        self.comments_md([(f"{i:032x}", f"Unrelated Page {i}",
                           ['- **on** "Example Hub" — Sam (2024-01-09): '
                            'I am cleaning up this page and will post my edits below'])
                          for i in range(50)])
        breaches = refresh.contamination_breaches(refresh.build_comment_index())
        self.assertEqual(len(breaches), 1)
        self.assertEqual(breaches[0].pages, 50)


class BaselineTest(unittest.TestCase):
    """A recorded pre-existing breach does not fail the run; growth does."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="a4-baseline-")
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        orig = refresh.STATE
        refresh.STATE = self.state
        self.addCleanup(setattr, refresh, "STATE", orig)

    def write_baseline(self, mapping):
        with open(os.path.join(self.state, refresh.CONTAMINATION_BASELINE), "w") as f:
            json.dump({refresh.contamination_id(t): {"pages": n, "text": t[:120]}
                       for t, n in mapping.items()}, f)

    def test_absent_baseline_means_strict(self):
        self.assertEqual(refresh.load_contamination_baseline(), {})
        self.assertEqual(len(refresh.contamination_breaches(index(**{"old mess": 500}))), 1)

    def test_recorded_breach_is_allowed(self):
        self.write_baseline({"old mess": 500})
        base = refresh.load_contamination_baseline()
        self.assertEqual(refresh.contamination_breaches(index(**{"old mess": 500}), base), [])

    def test_a_recorded_breach_that_grows_still_fails(self):
        self.write_baseline({"old mess": 500})
        base = refresh.load_contamination_baseline()
        breaches = refresh.contamination_breaches(index(**{"old mess": 501}), base)
        self.assertEqual([(b.pages, b.allowed) for b in breaches], [(501, 500)])

    def test_a_recorded_breach_that_shrinks_passes(self):
        self.write_baseline({"old mess": 500})
        base = refresh.load_contamination_baseline()
        self.assertEqual(refresh.contamination_breaches(index(**{"old mess": 400}), base), [])

    def test_a_new_breach_beside_a_recorded_one_fails(self):
        self.write_baseline({"old mess": 500})
        base = refresh.load_contamination_baseline()
        breaches = refresh.contamination_breaches(index(**{"old mess": 500, "new mess": 200}), base)
        self.assertEqual([b.text for b in breaches], ["new mess"])

    def test_baseline_round_trips_through_the_writer(self):
        refresh.save_contamination_baseline(index(**{"old mess": 500, "fine": 3}))
        base = refresh.load_contamination_baseline()
        self.assertEqual(refresh.contamination_breaches(index(**{"old mess": 500}), base), [])
        self.assertNotIn(refresh.contamination_id("fine"), base)


class CliTest(unittest.TestCase):
    """`--mode contamination-check`: the standalone entry point's exit codes."""

    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="a4-cli-")
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        os.makedirs(os.path.join(self.ws, "_databases"))
        for name, value in (("WS", self.ws), ("DBS", os.path.join(self.ws, "_databases")),
                            ("STATE", self.ws)):
            orig = getattr(refresh, name)
            setattr(refresh, name, value)
            self.addCleanup(setattr, refresh, name, orig)

    def spread(self, n):
        with open(os.path.join(self.ws, "_comments.md"), "w") as f:
            f.write("# c\n\n" + "".join(
                f"## Page {i}  `{i:032x}`\n\n"
                '- **on** "b" — A (2024-01-09): one thread copied everywhere\n\n' for i in range(n)))

    def test_clean_mirror_exits_zero(self):
        self.spread(3)
        self.assertEqual(refresh.contamination_cli(), 0)

    def test_contaminated_mirror_exits_nonzero(self):
        self.spread(90)
        self.assertEqual(refresh.contamination_cli(), 1)

    def test_writing_a_baseline_exits_zero_and_then_the_check_passes(self):
        self.spread(90)
        self.assertEqual(refresh.contamination_cli(write_baseline=True), 0)
        self.assertEqual(refresh.contamination_cli(), 0)

    def test_a_baseline_does_not_excuse_a_second_thread(self):
        self.spread(90)
        refresh.contamination_cli(write_baseline=True)
        with open(os.path.join(self.ws, "_comments.md"), "a") as f:
            f.write("".join(f"## Other {i}  `{i + 500:032x}`\n\n"
                            '- **on** "b" — A (2024-02-09): a second foreign thread\n\n'
                            for i in range(90)))
        self.assertEqual(refresh.contamination_cli(), 1)


class ReportTest(unittest.TestCase):
    """The run records the check, and a breach fails it."""

    def setUp(self):
        self.report = refresh.new_report("daily")

    def test_clean_check_records_a_count_and_returns_no_breaches(self):
        breaches = refresh.record_contamination_check(index(**{"fine": 3}), {}, self.report)
        self.assertEqual(breaches, [])
        self.assertEqual(self.report["comments"]["contamination_breaches"], 0)

    def test_breach_is_counted_and_noted(self):
        breaches = refresh.record_contamination_check(index(**{"bad": 90}), {}, self.report)
        self.assertEqual(len(breaches), 1)
        self.assertEqual(self.report["comments"]["contamination_breaches"], 1)
        self.assertTrue(any("contamination" in n for n in self.report["notes"]))


if __name__ == "__main__":
    unittest.main()
