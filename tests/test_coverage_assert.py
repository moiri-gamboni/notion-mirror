"""Coverage assert: the nightly re-measures the referenced-but-absent set and
reports anything the exclusion file does not excuse.

The backfill drained that set to zero, so the assert's expected output is *no
findings* and any finding is a real new gap — which is what filling the hole
bought over baselining it.

No network, no corpus: every test builds a two-file mirror in a temp tree.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coverage_census  # noqa: E402  (_tools is not a package; discover's top dir is tests/)
import refresh  # noqa: E402


def rid(n):
    """A distinct 32-hex id per fixture, readable in failure output."""
    return f"{n:032x}"


class MirrorCase(unittest.TestCase):
    """A temp corpus plus a temp exclusion file, with `refresh.WS` rebound so
    the assert's own default root is the one under test."""

    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="a9-coverage-")
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        self.dbs = os.path.join(self.ws, "_databases")
        self.state = os.path.join(self.ws, "state")
        os.makedirs(self.dbs)
        os.makedirs(self.state)
        # STATE too: the referenced-count floor defaults to a file under it, and a
        # test corpus writing its count into the live nightly's state would make
        # the next real run report INCONCLUSIVE against a 14-page fixture.
        for name, value in (("WS", self.ws), ("DBS", self.dbs), ("STATE", self.state)):
            orig = getattr(refresh, name)
            setattr(refresh, name, value)
            self.addCleanup(setattr, refresh, name, orig)
        self.exclusions = os.path.join(self.ws, "exclusions.json")

    # --- corpus construction -------------------------------------------------

    def page(self, name, *lines):
        """A mirrored content page carrying stand-in lines."""
        path = os.path.join(self.ws, f"{name}.md")
        with open(path, "w") as f:
            f.write(f"# {name}\n\n" + "\n".join(lines) + "\n")
        return path

    def db_standin(self, id32, title="High-Prio Funding Tasks"):
        """The short form — 1,471 of the corpus's 2,080 database stand-ins, and
        the shape that carried the instance this work exists to close."""
        return f"- 🗄️ {title} `{id32}`"

    def page_standin(self, id32, title="A Sub-page"):
        return f"- 📄 **{title}** — sub-page `{id32}`"

    def captured_db(self, id32, title="High-Prio Funding Tasks"):
        """What a capture leaves behind: the row directory is the artifact the
        census reads as presence."""
        d = os.path.join(self.dbs, f"{title} {id32}")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "_schema.md"), "w") as f:
            f.write(f"# {title}\n")
        return d

    def captured_page(self, id32, title="A Sub-page"):
        path = os.path.join(self.ws, f"{title} {id32}.md")
        with open(path, "w") as f:
            f.write(f"# {title}\n")
        return path

    def exclude(self, id32, reason="not_a_db", note="checked live"):
        return coverage_census.add_exclusion(id32, reason, note, path=self.exclusions)

    # --- the assert ----------------------------------------------------------

    def findings(self, state=None):
        cen = coverage_census.census(self.ws)
        excl = coverage_census.load_exclusions(self.exclusions)
        return refresh.coverage_findings(cen, excl, state)


class ThreeCaseTest(MirrorCase):
    """The plan's own three cases: a synthetic referenced-but-absent id produces
    exactly one finding; excluded produces none; already captured produces none."""

    def test_a_referenced_absent_database_is_exactly_one_finding(self):
        self.page("Example Emergency Plan", self.db_standin(rid(1)))
        found = self.findings()
        self.assertEqual([(f["id"], f["kind"]) for f in found],
                         [(rid(1), "child_database")])

    def test_the_same_id_in_the_exclusion_file_produces_none(self):
        self.page("Example Emergency Plan", self.db_standin(rid(1)))
        self.exclude(rid(1), "not_a_db", "linked view; Notion refuses it as a database")
        self.assertEqual(self.findings(), [])

    def test_an_id_already_backfilled_produces_none(self):
        self.page("Example Emergency Plan", self.db_standin(rid(1)))
        self.captured_db(rid(1))
        self.assertEqual(self.findings(), [])


class FindingShapeTest(MirrorCase):
    """What a finding carries, so an operator can act on it without re-deriving it."""

    def test_a_finding_names_its_title_and_where_it_was_referenced(self):
        self.page("Example Emergency Plan", "", self.db_standin(rid(7), "Funding Tasks"))
        f = self.findings()[0]
        self.assertEqual(f["title"], "Funding Tasks")
        self.assertEqual(f["occurrences"][0]["file"], "Example Emergency Plan.md")
        self.assertEqual(f["occurrences"][0]["line"], 4)
        self.assertEqual(f["occurrences"][0]["shape"], "db_short")

    def test_one_id_referenced_from_many_pages_is_one_finding(self):
        for n in range(3):
            self.page(f"Page {n}", self.db_standin(rid(1)))
        self.assertEqual(len(self.findings()), 1)
        self.assertEqual(len(self.findings()[0]["occurrences"]), 3)

    def test_occurrences_are_capped_so_one_id_cannot_flood_the_report(self):
        for n in range(9):
            self.page(f"Page {n}", self.db_standin(rid(1)))
        self.assertEqual(len(self.findings()[0]["occurrences"]), 3)

    def test_an_absent_sub_page_is_a_finding_too(self):
        self.page("Parent", self.page_standin(rid(2)))
        self.assertEqual([(f["id"], f["kind"]) for f in self.findings()],
                         [(rid(2), "sub_page")])

    def test_a_captured_sub_page_is_not(self):
        self.page("Parent", self.page_standin(rid(2)))
        self.captured_page(rid(2))
        self.assertEqual(self.findings(), [])

    def test_a_page_md_does_not_satisfy_a_database_reference(self):
        """Presence is kind-specific: 112 corpus ids are referenced as databases
        and exist only as some page's .md. Counting those present would report a
        database as captured while every one of its rows is still missing."""
        self.page("Parent", self.db_standin(rid(3)))
        self.captured_page(rid(3), "Looks Like It")
        self.assertEqual([f["id"] for f in self.findings()], [rid(3)])


class NightlyFlagTest(MirrorCase):
    """The nightly's own verdict travels with the finding.

    `capture_new_db` records `not_a_db` in `_meta/state/db-flags.json` and skips
    the id forever after, but writes no exclusion — so such an id is absent,
    unexcused, and reported every night until a human triages it. Carrying the
    flag turns each of those from an investigation into a decision."""

    def test_a_flagged_id_is_still_a_finding(self):
        self.page("Parent", self.db_standin(rid(4)))
        found = self.findings({"not_a_db": {rid(4): "2026-08-07T03:00:00Z"}, "db404": {}})
        self.assertEqual(len(found), 1)

    def test_the_flag_is_named_on_the_finding(self):
        self.page("Parent", self.db_standin(rid(4)))
        found = self.findings({"not_a_db": {rid(4): "2026-08-07T03:00:00Z"}, "db404": {}})
        self.assertEqual(found[0]["flagged"], "not_a_db")

    def test_db404_is_named_too(self):
        self.page("Parent", self.db_standin(rid(5)))
        found = self.findings({"not_a_db": {}, "db404": {rid(5): "2026-08-07T03:00:00Z"}})
        self.assertEqual(found[0]["flagged"], "db404")

    def test_an_unflagged_finding_says_so_rather_than_omitting_the_key(self):
        self.page("Parent", self.db_standin(rid(6)))
        self.assertIsNone(self.findings({"not_a_db": {}, "db404": {}})[0]["flagged"])

    def test_no_state_at_all_is_not_an_error(self):
        self.page("Parent", self.db_standin(rid(6)))
        self.assertIsNone(self.findings(None)[0]["flagged"])


class FreshnessTest(MirrorCase):
    """The assert re-scans; it does not read the committed census.

    `coverage_census.py --report` splits the *frozen* `census.json`. An assert
    built on that would report how stale that file is, which is not the
    question — so this pins that a census on disk claiming something else
    changes nothing."""

    def test_a_stale_census_file_does_not_supply_the_findings(self):
        self.page("Parent", self.db_standin(rid(1)))
        stale = os.path.join(self.ws, "census.json")
        with open(stale, "w") as f:
            json.dump({"generated": "2020-01-01T00:00:00Z", "corpus_root": "notion/workspace",
                       "totals": {}, "blind_spots": {},
                       "absent": [{"id": rid(99), "kind": "child_database", "title": "Ghost",
                                   "disposition": "unreviewed", "occurrences": []}]}, f)
        report = refresh.new_report("daily")
        refresh.record_coverage_check(report, root=self.ws, exclusions_path=self.exclusions)
        ids = [f["id"] for f in report["coverage"]["findings"]]
        self.assertEqual(ids, [rid(1)])
        self.assertNotIn(rid(99), ids)

    def test_a_capture_between_two_scans_clears_the_finding(self):
        """The property a frozen file cannot have: the second scan sees the
        first scan's gap closed."""
        self.page("Parent", self.db_standin(rid(1)))
        self.assertEqual(len(self.findings()), 1)
        self.captured_db(rid(1))
        self.assertEqual(self.findings(), [])


class RecordTest(MirrorCase):
    """What lands in the run report — it reports, it does not fail."""

    def check(self, state=None):
        report = refresh.new_report("daily")
        found = refresh.record_coverage_check(report, root=self.ws,
                                              exclusions_path=self.exclusions, state=state)
        return report, found

    def test_a_clean_mirror_records_zero_and_says_so(self):
        self.page("Parent", self.db_standin(rid(1)))
        self.captured_db(rid(1))
        report, found = self.check()
        self.assertEqual(found, [])
        self.assertEqual(report["coverage"]["findings"], [])
        self.assertTrue(any("coverage assert: no new gaps" in n for n in report["notes"]))

    def test_the_clean_note_carries_the_absent_and_excluded_counts(self):
        """A zero-finding line must not be confusable with 'nothing was
        measured': it states the size of the set it just excused."""
        self.page("Parent", self.db_standin(rid(1)), self.db_standin(rid(2)))
        self.exclude(rid(1), "db404", "gone 2026-08-06")
        self.exclude(rid(2), "not_a_db", "linked view")
        report, _ = self.check()
        self.assertEqual((report["coverage"]["absent"], report["coverage"]["excluded"]), (2, 2))
        note = next(n for n in report["notes"] if n.startswith("coverage assert"))
        self.assertIn("2 referenced-but-absent", note)

    def test_a_finding_is_counted_and_noted(self):
        self.page("Parent", self.db_standin(rid(1), "Funding Tasks"))
        report, found = self.check()
        self.assertEqual(len(found), 1)
        note = next(n for n in report["notes"] if n.startswith("coverage assert"))
        self.assertIn("1 NEW referenced-but-absent", note)
        self.assertIn(rid(1)[:8], note)
        self.assertIn("Funding Tasks", note)

    def test_the_note_says_a_flagged_finding_needs_a_decision_not_a_fetch(self):
        self.page("Parent", self.db_standin(rid(1)))
        report, _ = self.check({"not_a_db": {rid(1): "2026-08-07T03:00:00Z"}, "db404": {}})
        note = next(n for n in report["notes"] if n.startswith("coverage assert"))
        self.assertIn("not_a_db", note)
        self.assertIn("--exclude", note)

    def test_a_sub_page_finding_says_which_tool_closes_it(self):
        """No phase of the nightly can close one: probe_row harvests child_dbs
        into `discovered` and never child_pages, so a new sub_page gap is
        detectable here but recurs every night until someone runs the backfill by
        hand. The note has to say so, or it is nightly noise with no next step."""
        self.page("Parent", self.page_standin(rid(1)))
        report, found = self.check()
        self.assertEqual([f["kind"] for f in found], ["sub_page"])
        note = next(n for n in report["notes"] if n.startswith("coverage assert"))
        self.assertIn("coverage_backfill.py", note)

    def test_a_database_only_finding_does_not_mention_the_backfill(self):
        """Discovery does close those on its own, so pointing at a hand-run tool
        would send the operator to do work the next nightly already does."""
        self.page("Parent", self.db_standin(rid(1)))
        report, _ = self.check()
        note = next(n for n in report["notes"] if n.startswith("coverage assert"))
        self.assertNotIn("coverage_backfill.py", note)

    def test_the_note_is_capped_and_says_how_many_it_hid(self):
        for n in range(refresh.COVERAGE_SHOWN + 4):
            self.page(f"Page {n}", self.db_standin(rid(n + 1)))
        report, found = self.check()
        self.assertEqual(len(found), refresh.COVERAGE_SHOWN + 4)
        note = next(n for n in report["notes"] if n.startswith("coverage assert"))
        self.assertIn("+4 more", note)

    def test_every_finding_survives_into_the_json_even_when_the_note_is_capped(self):
        """The note is for reading; the report is for acting. Truncating both
        would make `len(report["coverage"]["findings"])` a lie."""
        for n in range(refresh.COVERAGE_SHOWN + 4):
            self.page(f"Page {n}", self.db_standin(rid(n + 1)))
        report, _ = self.check()
        self.assertEqual(len(report["coverage"]["findings"]), refresh.COVERAGE_SHOWN + 4)

    def test_the_blind_spots_ride_along_so_the_line_cannot_overclaim(self):
        """The assert sees referenced-but-absent over probed bodies only. The
        counts it cannot enumerate travel with it rather than being implied
        away by a zero."""
        self.page("Parent", "- 🗄️ **No id here** — database", "<!-- unhandled block type: column_list -->")
        report, _ = self.check()
        self.assertEqual(report["coverage"]["blind_spots"]["idless_child_database"], 1)
        self.assertEqual(report["coverage"]["blind_spots"]["unhandled_block_type"], 1)

    def test_the_check_returns_rather_than_raising_on_a_finding(self):
        """It reports; it does not fail the run. A new inline database appearing
        overnight is information, not a reason to abort a mirror refresh."""
        self.page("Parent", self.db_standin(rid(1)))
        report, found = self.check()
        self.assertEqual(len(found), 1)
        self.assertFalse(report["budget_exhausted"])

    def test_a_missing_exclusion_file_is_not_an_error(self):
        self.page("Parent", self.db_standin(rid(1)))
        report = refresh.new_report("daily")
        refresh.record_coverage_check(report, root=self.ws,
                                      exclusions_path=os.path.join(self.ws, "nope.json"))
        self.assertEqual(len(report["coverage"]["findings"]), 1)


class BudgetSkipTest(MirrorCase):
    """`run_coverage_assert` is the nightly's entry point, and the arm it takes
    is decided by one flag inside a 3,600-line `main()` — the one place nothing
    else is tested. Both arms are pinned here."""

    def call(self, budget_exhausted):
        report = refresh.new_report("daily")
        report["budget_exhausted"] = budget_exhausted
        found = refresh.run_coverage_assert(report, None, root=self.ws,
                                            exclusions_path=self.exclusions)
        return report, found

    def test_a_healthy_run_asserts(self):
        self.page("Parent", self.db_standin(rid(1)))
        report, found = self.call(False)
        self.assertEqual(len(found), 1)
        self.assertTrue(report["coverage"])

    def test_a_budget_stopped_run_does_not(self):
        self.page("Parent", self.db_standin(rid(1)))
        report, found = self.call(True)
        self.assertIsNone(found)
        self.assertEqual(report["coverage"], {})

    def test_the_skip_is_stated_not_silent(self):
        """A missing coverage line must not be readable as a clean assert."""
        report, _ = self.call(True)
        self.assertTrue(any("coverage assert skipped" in n for n in report["notes"]))
        self.assertNotIn("## Coverage", refresh.report_md(report))


class NeverFailsTheRunTest(MirrorCase):
    """"It reports; it does not fail the run" has to hold for the assert's own
    failures too. By the time it runs the mirror is fetched and the tree is
    written, and an exception here would abort `refresh.sh` before `git add`."""

    def test_a_malformed_exclusion_file_is_noted_not_raised(self):
        self.page("Parent", self.db_standin(rid(1)))
        with open(self.exclusions, "w") as f:
            f.write("{not json at all")
        report = refresh.new_report("daily")
        found = refresh.run_coverage_assert(report, None, root=self.ws,
                                            exclusions_path=self.exclusions)
        self.assertIsNone(found)
        self.assertTrue(any("coverage assert failed to run" in n for n in report["notes"]))

    def test_a_corpus_root_that_does_not_exist_is_noted_not_raised(self):
        """os.walk over a missing directory yields nothing rather than raising,
        so this lands on the inconclusive guard rather than the except."""
        report = refresh.new_report("daily")
        refresh.run_coverage_assert(report, None, root=os.path.join(self.ws, "gone"),
                                    exclusions_path=self.exclusions)
        self.assertIn("INCONCLUSIVE", next(n for n in report["notes"]
                                           if n.startswith("coverage assert")))


class FalseCleanTest(MirrorCase):
    """An empty scan is not a clean mirror.

    If the corpus root moves or the sparse checkout omits it, every count is
    zero and the honest-looking output is "no new gaps" — the assert's own
    version of the failure it exists to catch."""

    def test_a_corpus_with_no_standins_reports_inconclusive_not_clean(self):
        report = refresh.new_report("daily")
        refresh.record_coverage_check(report, root=self.ws, exclusions_path=self.exclusions)
        note = next(n for n in report["notes"] if n.startswith("coverage assert"))
        self.assertIn("INCONCLUSIVE", note)
        self.assertNotIn("no new gaps", note)

    def test_a_real_corpus_with_everything_captured_still_reports_clean(self):
        """The guard must key on 'nothing was scanned', not on 'nothing was
        missing' — otherwise a genuinely complete mirror reads as broken."""
        self.page("Parent", self.db_standin(rid(1)))
        self.captured_db(rid(1))
        report = refresh.new_report("daily")
        refresh.record_coverage_check(report, root=self.ws, exclusions_path=self.exclusions)
        note = next(n for n in report["notes"] if n.startswith("coverage assert"))
        self.assertIn("no new gaps", note)
        self.assertEqual(report["coverage"]["referenced"], 1)


class ReferencedFloorTest(MirrorCase):
    """The `referenced == 0` sentinel catches a wholly-missing corpus. It cannot
    catch one missing a *subset*, because references and their targets disappear
    together — the gap silently shrinks instead of growing, and the assert then
    reports the remaining half as clean.

    The floor is the previous run's `referenced` count: the corpus only grows in
    normal operation, so a drop is a scan that looked in the wrong place — a
    truncated checkout, a mid-sync tree, a bad --root."""

    def setUp(self):
        super().setUp()
        self.floor = os.path.join(self.ws, "coverage-floor.json")

    def check(self):
        report = refresh.new_report("daily")
        refresh.record_coverage_check(report, root=self.ws, exclusions_path=self.exclusions,
                                      floor_path=self.floor)
        return report, next(n for n in report["notes"] if n.startswith("coverage assert"))

    def corpus(self, n):
        for i in range(n):
            self.page(f"Page {i}", self.db_standin(rid(i + 1)))
            self.captured_db(rid(i + 1))

    def test_the_first_run_records_the_count_it_measured(self):
        self.corpus(10)
        report, _ = self.check()
        self.assertEqual(report["coverage"]["referenced"], 10)
        with open(self.floor) as f:
            self.assertEqual(json.load(f)["referenced"], 10)

    def test_a_steady_corpus_reports_clean(self):
        self.corpus(10)
        self.check()
        _report, note = self.check()
        self.assertIn("no new gaps", note)

    def test_a_growing_corpus_reports_clean_and_raises_the_floor(self):
        self.corpus(10)
        self.check()
        self.corpus(20)
        _report, note = self.check()
        self.assertIn("no new gaps", note)
        with open(self.floor) as f:
            self.assertEqual(json.load(f)["referenced"], 20)

    def test_a_corpus_that_lost_a_fifth_of_its_references_is_inconclusive(self):
        self.corpus(10)
        self.check()
        for i in range(2):
            os.remove(os.path.join(self.ws, f"Page {i}.md"))
        _report, note = self.check()
        self.assertIn("INCONCLUSIVE", note)
        self.assertNotIn("no new gaps", note)

    def test_the_inconclusive_note_names_the_way_out(self):
        """The floor never falls, so a corpus that genuinely shrank past the band
        — a Notion cleanup, which is planned work here — reports INCONCLUSIVE on
        every run forever. Recovery is deleting a gitignored file, so the note has
        to name it or the finding is permanent noise with no next step, which is
        the defect the sub_page note exists to avoid one function away."""
        self.corpus(10)
        self.check()
        for i in range(5):
            os.remove(os.path.join(self.ws, f"Page {i}.md"))
        _report, note = self.check()
        self.assertIn("coverage-floor.json", note)

    def test_the_inconclusive_note_names_both_counts(self):
        self.corpus(10)
        self.check()
        for i in range(5):
            os.remove(os.path.join(self.ws, f"Page {i}.md"))
        _report, note = self.check()
        self.assertIn("10", note)
        self.assertIn("5", note)

    def test_a_small_dip_is_tolerated(self):
        """Rows do get deleted in Notion. The floor is a 10% band, not a ratchet
        — otherwise the assert cries wolf on ordinary attrition."""
        self.corpus(100)
        self.check()
        os.remove(os.path.join(self.ws, "Page 0.md"))
        _report, note = self.check()
        self.assertIn("no new gaps", note)

    def test_an_inconclusive_run_does_not_lower_the_floor(self):
        """Otherwise a truncated checkout ratchets the floor down to its own size
        and the next equally-truncated run reads as clean."""
        self.corpus(10)
        self.check()
        for i in range(5):
            os.remove(os.path.join(self.ws, f"Page {i}.md"))
        self.check()
        with open(self.floor) as f:
            self.assertEqual(json.load(f)["referenced"], 10)

    def test_an_empty_corpus_still_reports_the_hard_arm(self):
        report = refresh.new_report("daily")
        refresh.record_coverage_check(report, root=self.ws, exclusions_path=self.exclusions,
                                      floor_path=self.floor)
        note = next(n for n in report["notes"] if n.startswith("coverage assert"))
        self.assertIn("INCONCLUSIVE", note)
        self.assertIn("no stand-in references at all", note)

    def test_a_missing_floor_file_is_not_an_error(self):
        self.corpus(10)
        _report, note = self.check()
        self.assertIn("no new gaps", note)

    def test_a_malformed_floor_file_does_not_fail_the_run(self):
        self.corpus(10)
        with open(self.floor, "w") as f:
            f.write("{not json")
        _report, note = self.check()
        self.assertIn("no new gaps", note)


class ReportMdTest(MirrorCase):
    """The rendered report is what the changelog analysis reads."""

    def render(self, state=None):
        report = refresh.new_report("daily")
        refresh.record_coverage_check(report, root=self.ws,
                                      exclusions_path=self.exclusions, state=state)
        return refresh.report_md(report)

    def test_a_run_without_the_assert_renders_no_coverage_section(self):
        self.assertNotIn("## Coverage", refresh.report_md(refresh.new_report("daily")))

    def test_a_clean_run_still_renders_the_section(self):
        """Silence is indistinguishable from 'the assert never ran' — the whole
        failure class this plan keeps meeting. A zero is stated."""
        self.page("Parent", self.db_standin(rid(1)))
        self.captured_db(rid(1))
        self.assertIn("## Coverage — 0 new gap(s)", self.render())

    def test_a_finding_is_enumerated_once_with_what_acting_on_it_needs(self):
        """The note is the single enumeration, so it carries the full id the
        `--exclude` command takes and the file:line the reference sits on."""
        self.page("Parent", self.db_standin(rid(1), "Funding Tasks"))
        report = refresh.new_report("daily")
        refresh.record_coverage_check(report, root=self.ws,
                                      exclusions_path=self.exclusions)
        note = next(n for n in report["notes"] if n.startswith("coverage assert"))
        self.assertIn(rid(1), note)
        self.assertIn("Funding Tasks", note)
        self.assertIn("Parent.md:3", note)
        md = refresh.report_md(report)
        self.assertIn("## Coverage — 1 new gap(s)", md)
        self.assertEqual(md.count(rid(1)), 1)

    def test_the_nightly_flag_is_rendered(self):
        self.page("Parent", self.db_standin(rid(1)))
        self.assertIn("not_a_db", self.render({"not_a_db": {rid(1): "x"}, "db404": {}}))


if __name__ == "__main__":
    unittest.main()
