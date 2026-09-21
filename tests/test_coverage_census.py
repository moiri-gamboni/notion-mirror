"""Coverage census: which referenced ids never became mirrored artifacts, and
the class fix that stops row-body inline databases going missing.

Two halves, both offline:

* `coverage_census` scans mirrored markdown for the stand-in lines four
  generations of renderer left behind. The corpus was not written by one
  renderer, so a single-pattern scan sees a minority of the surface — these
  tests pin every shape that actually occurs, including the one that motivated
  the task.
* `refresh.probe_row` collects a row body's `child_database` blocks into
  `Walker.child_dbs` and, until this task, dropped them on the floor. The
  threading tests use a fake API; nothing here reaches Notion.
"""
import json
import os
import shutil
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coverage_census  # noqa: E402  (_tools is not a package; discover's top dir is tests/)
import refresh  # noqa: E402


# The shape of the line that motivated the whole task: a row body under
# workspace/_databases/Braindump+ <id>/ carrying the short database stand-in.
BRAINDUMP_LINE = "- 🗄️ High-Prio Funding Tasks `1b000000000000000000000000000000`"
BRAINDUMP_DB = "1b000000000000000000000000000000"


def rid(n):
    """A distinct 32-hex id per test fixture, readable in failure output."""
    return f"{n:032x}"


class ScanShapes(unittest.TestCase):
    """Every stand-in form that occurs in the corpus is recognised, and prose
    that merely contains the glyph is not."""

    def refs(self, *lines):
        return coverage_census.scan_text("f.md", "\n".join(lines))

    def one(self, line):
        got = self.refs(line)
        self.assertEqual(len(got), 1, f"expected exactly one ref from {line!r}, got {got}")
        return got[0]

    def test_braindump_short_form_is_matched(self):
        # A `— database \`id\`` pattern alone sees none of this shape, which is
        # 1,471 of the corpus's 2,080 database stand-ins.
        ref = self.one(BRAINDUMP_LINE)
        self.assertEqual(ref.kind, "child_database")
        self.assertEqual(ref.id, BRAINDUMP_DB)
        self.assertEqual(ref.shape, "db_short")
        self.assertEqual(ref.title, "High-Prio Funding Tasks")

    def test_refresh_long_form_is_matched(self):
        ref = self.one("  - 🗄️ **Tasks** — database `%s` (rows in workspace/_databases/)" % rid(1))
        self.assertEqual((ref.kind, ref.shape, ref.id), ("child_database", "db_long", rid(1)))
        self.assertEqual(ref.title, "Tasks")

    def test_notion_walk_long_form_is_matched(self):
        ref = self.one("- 🗄️ **Tasks** — database `%s` (rows in CSV export)" % rid(2))
        self.assertEqual((ref.kind, ref.shape, ref.id), ("child_database", "db_long_csv", rid(2)))

    def test_all_three_database_shapes_in_one_pass(self):
        got = self.refs(
            BRAINDUMP_LINE,
            "- 🗄️ **A** — database `%s` (rows in workspace/_databases/)" % rid(1),
            "    - 🗄️ **B** — database `%s` (rows in CSV export)" % rid(2),
        )
        self.assertEqual([r.shape for r in got], ["db_short", "db_long", "db_long_csv"])
        self.assertEqual({r.kind for r in got}, {"child_database"})

    def test_sub_page_shapes(self):
        got = self.refs(
            "- 📄 **Plan** — sub-page `%s`" % rid(3),
            "- 📄 **Plan** — sub-page `%s` (MISSING) → `Plan %s.md`" % (rid(4), rid(4)),
            "- 📄 Plan `%s`" % rid(5),
        )
        self.assertEqual([r.shape for r in got], ["page_long", "page_long_status", "page_short"])
        self.assertEqual({r.kind for r in got}, {"sub_page"})
        self.assertEqual([r.id for r in got], [rid(3), rid(4), rid(5)])

    def test_link_to_page(self):
        ref = self.one("- 🔗 link to `%s`" % rid(6))
        self.assertEqual((ref.kind, ref.shape, ref.id), ("link_to_page", "link_to_page", rid(6)))

    def test_line_numbers_are_one_based(self):
        got = coverage_census.scan_text("f.md", "intro\n\n" + BRAINDUMP_LINE)
        self.assertEqual(got[0].line, 3)
        self.assertEqual(got[0].file, "f.md")

    def test_prose_carrying_the_glyph_without_an_id_is_not_a_reference(self):
        # Real corpus lines: file listings inside code blocks and README prose.
        self.assertEqual(self.refs("- 📄 deployment.py"), [])
        self.assertEqual(self.refs("see the 🗄️ database for details"), [])
        self.assertEqual(self.refs("- 🗄️ Tasks `not-a-hex-id`"), [])

    def test_uppercase_and_short_hex_are_rejected(self):
        # Notion ids are lowercase 32-hex; anything else is a false positive.
        self.assertEqual(self.refs("- 🗄️ Tasks `%s`" % BRAINDUMP_DB.upper()), [])
        self.assertEqual(self.refs("- 🗄️ Tasks `deadbeef`"), [])


class BlindSpots(unittest.TestCase):
    """Two reference classes carry no id, so no id-diff can see them. They are
    counted, never enumerated — that is the honest report."""

    def test_idless_database_standins_are_counted_not_listed(self):
        text = "- 🗄️ Some Inline DB\n- 🗄️ Another One\n" + BRAINDUMP_LINE
        refs = coverage_census.scan_text("f.md", text)
        self.assertEqual([r.id for r in refs], [BRAINDUMP_DB])
        self.assertEqual(coverage_census.count_blind_spots(text)["idless_child_database"], 2)

    def test_idless_sub_page_standins_are_counted(self):
        text = "- 📄 A Sub Page\n- 📄 Another\n"
        self.assertEqual(coverage_census.scan_text("f.md", text), [])
        self.assertEqual(coverage_census.count_blind_spots(text)["idless_sub_page"], 2)

    def test_unhandled_block_comments_are_counted(self):
        text = "<!-- unhandled block type: ai_block -->\n<!-- unhandled block type: x -->"
        self.assertEqual(coverage_census.count_blind_spots(text)["unhandled_block_type"], 2)


class CorpusCensus(unittest.TestCase):
    """End to end over a synthetic mirror: present ids drop out, absent ids
    become an unreviewed work list."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="a6-census-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def write(self, relpath, text):
        path = os.path.join(self.root, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def mkdb(self, title, db_id):
        os.makedirs(os.path.join(self.root, "_databases", f"{title} {db_id}"), exist_ok=True)

    def test_present_ids_separates_databases_from_pages(self):
        self.mkdb("Tasks", rid(1))
        self.write(f"Some Page {rid(2)}.md", "x")
        got = coverage_census.present_ids(self.root)
        self.assertEqual(got["database"], {rid(1)})
        self.assertEqual(got["page"], {rid(2)})

    def test_a_database_that_exists_only_as_a_page_md_is_still_absent(self):
        # 112 corpus ids sit exactly here. A page carrying the id is not the
        # database: none of its rows were captured, so it is real missing work.
        self.write(f"Tasks {rid(1)}.md", "x")
        self.write(f"Ref {rid(2)}.md", "- 🗄️ **Tasks** — database `%s` (rows in CSV export)" % rid(1))
        out = coverage_census.census(self.root)
        self.assertEqual(out["totals"]["child_database"], {"referenced": 1, "absent": 1})

    def test_a_link_to_page_resolves_against_either_artifact(self):
        # The block carries a page_id *or* a database_id, so both count.
        self.mkdb("Tasks", rid(1))
        self.write(f"Page {rid(2)}.md", "x")
        self.write(f"Ref {rid(3)}.md", "\n".join([
            "- 🔗 link to `%s`" % rid(1),
            "- 🔗 link to `%s`" % rid(2),
            "- 🔗 link to `%s`" % rid(4),
        ]))
        out = coverage_census.census(self.root)
        self.assertEqual(out["totals"]["link_to_page"], {"referenced": 3, "absent": 1})
        self.assertEqual([i["id"] for i in out["absent"]], [rid(4)])

    def test_absent_ids_become_unreviewed_work_items(self):
        self.mkdb("Braindump+", rid(9))
        self.write(f"_databases/Braindump+ {rid(9)}/Example Emergency Plan {rid(8)}.md",
                   "## Body\n\n" + BRAINDUMP_LINE + "\n")
        out = coverage_census.census(self.root)
        self.assertEqual(out["totals"]["child_database"], {"referenced": 1, "absent": 1})
        item, = out["absent"]
        self.assertEqual(item["id"], BRAINDUMP_DB)
        self.assertEqual(item["kind"], "child_database")
        self.assertEqual(item["title"], "High-Prio Funding Tasks")
        self.assertEqual(item["disposition"], "unreviewed")
        occ, = item["occurrences"]
        self.assertEqual(occ["shape"], "db_short")
        self.assertEqual(occ["line"], 3)
        self.assertTrue(occ["file"].startswith("_databases/Braindump+"),
                        f"reference path should be corpus-relative, got {occ['file']!r}")

    def test_a_mirrored_database_is_not_reported_absent(self):
        self.mkdb("Tasks", rid(1))
        self.write(f"Page {rid(2)}.md", "- 🗄️ **Tasks** — database `%s` (rows in CSV export)" % rid(1))
        out = coverage_census.census(self.root)
        self.assertEqual(out["totals"]["child_database"], {"referenced": 1, "absent": 0})
        self.assertEqual(out["absent"], [])

    def test_one_id_referenced_from_several_files_is_one_work_item(self):
        self.write(f"A {rid(1)}.md", BRAINDUMP_LINE)
        self.write(f"B {rid(2)}.md", "- 🗄️ **High-Prio Funding Tasks** — database `%s` (rows in CSV export)"
                   % BRAINDUMP_DB)
        out = coverage_census.census(self.root)
        self.assertEqual(out["totals"]["child_database"], {"referenced": 1, "absent": 1})
        item, = out["absent"]
        self.assertEqual(sorted(o["shape"] for o in item["occurrences"]),
                         ["db_long_csv", "db_short"])

    def test_the_three_kinds_are_counted_separately(self):
        self.write(f"A {rid(1)}.md", "\n".join([
            BRAINDUMP_LINE,
            "- 📄 **Gone** — sub-page `%s`" % rid(4),
            "- 📄 **Here** — sub-page `%s`" % rid(5),
            "- 🔗 link to `%s`" % rid(6),
        ]))
        self.write(f"Here {rid(5)}.md", "x")
        out = coverage_census.census(self.root)
        self.assertEqual(out["totals"], {
            "child_database": {"referenced": 1, "absent": 1},
            "sub_page": {"referenced": 2, "absent": 1},
            "link_to_page": {"referenced": 1, "absent": 1},
        })

    def test_blind_spots_are_reported_alongside_the_totals(self):
        self.write(f"A {rid(1)}.md", "- 🗄️ Nameless Inline DB\n<!-- unhandled block type: ai_block -->")
        out = coverage_census.census(self.root)
        self.assertEqual(out["blind_spots"]["idless_child_database"], 1)
        self.assertEqual(out["blind_spots"]["unhandled_block_type"], 1)

    def test_corpus_root_is_recorded_independently_of_the_callers_cwd(self):
        # The integration run happens from a worktree against the main tree; a
        # relative-to-script path would write ../../../notion/workspace into a
        # committed file.
        self.write(f"A {rid(1)}.md", "x")
        out = coverage_census.census(self.root)
        self.assertEqual(out["corpus_root"],
                         "/".join(os.path.abspath(self.root).split(os.sep)[-2:]))
        self.assertNotIn("..", out["corpus_root"])

    def test_a_partial_capture_directory_is_not_read_as_a_present_database(self):
        # A SIGTERM'd backfill slice leaves `_databases/.partial-…/` behind: the
        # `except BaseException` cleanup covers exceptions, not signals. If the
        # census counted that directory the half-captured database would be
        # "present" forever and never re-enter any work list.
        # Every layout that has been on disk: the id-suffixed name earlier
        # versions wrote (which `ID_IN_NAME` matches outright), its `.tmp`
        # successor, and the current nested container.
        for name in (f".partial-{rid(1)}", f".partial-{rid(1)}.tmp",
                     os.path.join(".partial", f"{rid(1)}.tmp")):
            with self.subTest(name=name):
                shutil.rmtree(os.path.join(self.root, "_databases"), ignore_errors=True)
                os.makedirs(os.path.join(self.root, "_databases", name))
                self.write(f"Ref {rid(2)}.md",
                           "- 🗄️ **Tasks** — database `%s` (rows in CSV export)" % rid(1))
                out = coverage_census.census(self.root)
                self.assertEqual(out["totals"]["child_database"],
                                 {"referenced": 1, "absent": 1})
                self.assertEqual([i["id"] for i in out["absent"]], [rid(1)])

    def test_row_files_inside_a_partial_directory_are_not_present_pages(self):
        # The rows written before the kill are real files with real ids; counting
        # them would silently satisfy sub-page references to rows nobody finished.
        self.write(f"_databases/.partial/{rid(1)}.tmp/Row One {rid(3)}.md", "x")
        self.assertEqual(coverage_census.present_ids(self.root),
                         {"database": set(), "page": set()})

    def test_references_inside_a_partial_directory_are_not_scanned(self):
        # Chain closure over a half-written row would queue work discovered from
        # a file that is about to be deleted and rewritten.
        self.write(f"_databases/.partial/{rid(1)}.tmp/Row One {rid(3)}.md", BRAINDUMP_LINE)
        refs, _blind = coverage_census.scan_corpus(self.root)
        self.assertEqual(refs, [])

    def test_output_is_json_serialisable_and_stably_ordered(self):
        self.write(f"A {rid(1)}.md", "\n".join([
            "- 🗄️ **Z** — database `%s` (rows in CSV export)" % rid(20),
            BRAINDUMP_LINE,
        ]))
        out = coverage_census.census(self.root)
        json.dumps(out)  # must not raise
        self.assertEqual([i["id"] for i in out["absent"]],
                         sorted(i["id"] for i in out["absent"]))


class FakeApi:
    """Serves canned /blocks/{id}/children and /comments pages to the Walker."""

    def __init__(self, children):
        self.children = children
        self.n = 0

    def paginate(self, method, path, params=None, body=None):
        self.n += 1
        if path == "/comments":
            return []
        block_id = path.split("/")[2]
        return list(self.children.get(block_id, []))


def child_database_block(block_id, title):
    return {"id": block_id, "type": "child_database",
            "child_database": {"title": title}, "has_children": False}


class RowBodyDiscovery(unittest.TestCase):
    """An inline database inside a DB-row body must reach `discovered`, the same
    way `phase_content` already harvests one inside a content page. Without
    this, `Walker.child_dbs` is collected on the row path and never read — which
    is the class that produced the census backlog."""

    ROW = refresh.dashed(rid(0x11))
    DB_BLOCK = refresh.dashed(int(BRAINDUMP_DB, 16).to_bytes(16, "big").hex())

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="a6-discovery-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.api = FakeApi({self.ROW: [child_database_block(self.DB_BLOCK, "High-Prio Funding Tasks")]})
        self.users = types.SimpleNamespace(name=lambda u: "Someone")
        self.report = refresh.new_report("test")

    def test_probe_row_harvests_the_inline_database(self):
        discovered = set()
        enrichment, _capped = refresh.probe_row(
            self.api, self.users, self.ROW, self.dir, self.report, discovered=discovered)
        self.assertIn("🗄️", enrichment)
        self.assertEqual(discovered, {"db:" + BRAINDUMP_DB})

    def test_probe_row_without_a_discovered_set_still_works(self):
        # phase_rows and any future caller may not carry one.
        enrichment, _capped = refresh.probe_row(
            self.api, self.users, self.ROW, self.dir, self.report)
        self.assertIn("High-Prio Funding Tasks", enrichment)

    def test_upsert_row_md_threads_discovered_through(self):
        discovered = set()
        page = {"id": self.ROW,
                "properties": {"Name": {"type": "title", "title": [{"plain_text": "Row One"}]}},
                "last_edited_time": "2026-08-01T00:00:00.000Z"}
        refresh.upsert_row_md(self.api, self.users, page, rid(0x22), "Test DB", ["Name"],
                              self.dir, True, {"queue": [], "comment_rows": {}}, self.report,
                              types.SimpleNamespace(dry_run=False), discovered=discovered)
        self.assertEqual(discovered, {"db:" + BRAINDUMP_DB})


if __name__ == "__main__":
    unittest.main()


class TestExclusionTriage(unittest.TestCase):
    """The exclusion machinery — individually reasoned, closed reason set."""

    def _paths(self, tmp):
        census = os.path.join(tmp, "census.json")
        excl = os.path.join(tmp, "exclusions.json")
        with open(census, "w") as f:
            json.dump({"absent": [
                {"id": "a" * 32, "kind": "child_database", "occurrences": []},
                {"id": "b" * 32, "kind": "sub_page", "occurrences": []},
            ]}, f)
        return census, excl

    def test_report_splits_excluded_from_backfill(self):
        import io, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            census, excl = self._paths(tmp)
            coverage_census.add_exclusion("a" * 32, "not_a_db", "test", path=excl)
            split = coverage_census.report(census, excl, out=io.StringIO())
            self.assertEqual([e["id"] for e in split["excluded"]], ["a" * 32])
            self.assertEqual([e["id"] for e in split["backfill"]], ["b" * 32])
            self.assertEqual(split["by_reason"], {"not_a_db": 1})

    def test_no_access_is_a_reason_in_its_own_right(self):
        """`db404` says "deleted, or never shared" and is treated as permanent.
        1,322 of the 1,400 entries are the second half of that sentence — a
        sharing decision, reversible tomorrow — and nothing distinguished them."""
        import io, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            census, excl = self._paths(tmp)
            coverage_census.add_exclusion("a" * 32, "no_access", "unshared", path=excl)
            self.assertEqual(coverage_census.load_exclusions(excl)["a" * 32]["reason"],
                             "no_access")
            split = coverage_census.report(census, excl, out=io.StringIO())
            self.assertEqual(split["by_reason"], {"no_access": 1})

    def test_the_report_names_every_reason_it_counts(self):
        """`report` prints one line per reason in `EXCLUSION_REASONS`; a reason
        missing from that tuple is counted into a line nobody sees."""
        import io, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            census, excl = self._paths(tmp)
            coverage_census.add_exclusion("a" * 32, "no_access", "unshared", path=excl)
            out = io.StringIO()
            coverage_census.report(census, excl, out=out)
            self.assertIn("no_access", out.getvalue())

    def test_exclusion_reason_is_a_closed_set(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            excl = os.path.join(tmp, "exclusions.json")
            with self.assertRaises(ValueError):
                coverage_census.add_exclusion("c" * 32, "because", path=excl)

    def test_deliberate_needs_a_note(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            excl = os.path.join(tmp, "exclusions.json")
            with self.assertRaises(ValueError):
                coverage_census.add_exclusion("c" * 32, "deliberate", path=excl)
            coverage_census.add_exclusion("c" * 32, "deliberate", "automation subtree nobody reads", path=excl)
