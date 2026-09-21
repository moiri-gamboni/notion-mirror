"""`--mode rows` refreshes exactly the rows it is given, and nothing else.

Fully offline: the API is a fake that serves pages from a dict, and
`refresh.probe_row` is stubbed on every path, so nothing here can reach Notion.
"""
import csv
import io
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import refresh  # noqa: E402  (_tools is not a package; discover's top dir is tests/)


class FakeApi:
    """Serves /pages/{id} from a dict and counts requests, like refresh.Api."""

    def __init__(self, pages, budget=10_000):
        self.pages = {refresh.undash(k): v for k, v in pages.items()}
        self.budget = budget
        self.n = 0
        self.r429 = 0
        self.calls = []

    def get(self, path, params=None, ver=None):
        self.calls.append(path)
        if self.n >= self.budget:
            raise refresh.Budget()
        self.n += 1
        if path.startswith("/pages/"):
            rid = refresh.undash(path.split("/pages/", 1)[1])
            if rid not in self.pages:
                raise refresh.ApiError(404, "not found")
            return self.pages[rid]
        raise AssertionError(f"unexpected request: {path}")

    def paginate(self, method, path, body=None, params=None, ver=None):
        raise AssertionError(f"unexpected pagination: {path}")


def page(rid, db_id, title="Row One", **props):
    p = {"Name": {"id": "title", "type": "title", "title": [{"plain_text": title}]}}
    for k, v in props.items():
        p[k] = {"id": k, "type": "rich_text", "rich_text": [{"plain_text": v}]}
    return {"id": refresh.dashed(rid), "properties": p,
            "parent": {"type": "database_id", "database_id": refresh.dashed(db_id)},
            "last_edited_time": "2026-08-06T00:00:00.000Z"}


class MirrorTestCase(unittest.TestCase):
    """A temp mirror: one DB directory with a CSV, a schema and some rows."""

    DB_ID = "bb" * 16
    DB_TITLE = "Test DB"
    COLS = ["Name", "Status"]

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="a14-rows-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.dbs = os.path.join(self.root, "_databases")
        self.state = os.path.join(self.root, "state")
        os.makedirs(self.state)
        self.dirname = f"{self.DB_TITLE} {self.DB_ID}"
        self.dirpath = os.path.join(self.dbs, self.dirname)
        os.makedirs(self.dirpath)
        for name, value in (("DBS", self.dbs), ("STATE", self.state)):
            p = mock.patch.object(refresh, name, value)
            p.start()
            self.addCleanup(p.stop)
        refresh.jsave(os.path.join(self.dirpath, "_schema.json"),
                      {"id": self.DB_ID, "title": self.DB_TITLE, "database": {"properties": {}}})
        self.csv_path = os.path.join(self.dirpath, f"{self.DB_TITLE} {self.DB_ID}.csv")
        self.real_probe = refresh.probe_row
        self.addCleanup(setattr, refresh, "probe_row", self.real_probe)
        self.probed = []
        self.stub_probe()
        self.state_dict = {"queue": [], "comment_rows": {}, "probe_policy": {},
                           "rows": {}, "not_a_db": {}}
        self.report = refresh.new_report("rows")
        self.args = types.SimpleNamespace(dry_run=False)

    # -- fixtures -----------------------------------------------------------

    def stub_probe(self, enrichment="\n\n## Body\n\n  fresh body\n", capped=False):
        def _stub(api, users, page_id, dest_dir, report, old_comments_body="",
                  max_blocks=800, block_comment_cap=25, discovered=None):
            self.probed.append(refresh.undash(page_id))
            return enrichment, capped
        refresh.probe_row = _stub

    def write_csv(self, rows):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["_row_id"] + self.COLS)
        for r in rows:
            w.writerow(r)
        with open(self.csv_path, "wb") as f:
            f.write(buf.getvalue().encode())

    def seed_row(self, rid, title="Row One", enrichment="", status="old"):
        p = page(rid, self.DB_ID, title, Status=status)
        txt = refresh.render_row_md(p, self.DB_ID, self.DB_TITLE, self.COLS, None, enrichment)
        path = os.path.join(self.dirpath, f"{refresh.sanitize(title)} {rid}.md")
        with open(path, "w") as f:
            f.write(txt)
        return path

    def row_text(self, rid, title="Row One"):
        with open(os.path.join(self.dirpath, f"{refresh.sanitize(title)} {rid}.md")) as f:
            return f.read()

    def spath(self, name):
        return os.path.join(self.state, name)

    def run_rows(self, api, ids, discovered=None):
        refresh.phase_rows(api, None, self.state_dict, self.report, self.args,
                           ids, discovered if discovered is not None else set())


class ScopeTest(MirrorTestCase):
    """"Exactly the given ids and nothing else" — the whole point of the phase."""

    A, B, C = "a" * 32, "b" * 32, "c" * 32

    def setUp(self):
        super().setUp()
        for rid in (self.A, self.B, self.C):
            self.seed_row(rid, f"Row {rid[0]}")
        self.write_csv([[rid, f"Row {rid[0]}", "old"] for rid in (self.A, self.B, self.C)])
        self.api = FakeApi({rid: page(rid, self.DB_ID, f"Row {rid[0]}", Status="new")
                            for rid in (self.A, self.B, self.C)})

    def test_probes_only_the_named_rows(self):
        self.run_rows(self.api, [self.A, self.C])
        self.assertEqual(sorted(self.probed), sorted([self.A, self.C]))
        self.assertEqual(self.report["rows"]["refreshed"], [self.A, self.C])

    def test_the_shared_probe_queue_and_priority_file_are_never_touched(self):
        # phase_queue folds webhook-priority-pages.json into the persisted queue
        # and jsaves the shrunken file before draining. A rows tick that did the
        # same would drain up to 653 rows on a budget of 10-30, and would rewrite
        # two files the receiver appends to between our load and our save.
        queue = self.spath("probe-queue.json")
        prio = self.spath("webhook-priority-pages.json")
        entry = {"kind": "row_probe", "db": self.DB_ID, "row": self.B}
        refresh.jsave(queue, [entry])
        refresh.jsave(prio, [self.B])
        self.state_dict["queue"] = refresh.jload(queue, [])  # as main() loads it
        before = (open(queue, "rb").read(), open(prio, "rb").read())

        self.run_rows(self.api, [self.A])

        self.assertEqual((open(queue, "rb").read(), open(prio, "rb").read()), before)
        self.assertEqual(self.probed, [self.A], "the queued row must not be drained")
        self.assertEqual(self.state_dict["queue"], [entry], "nor dropped from memory")

    def test_webhook_db_events_are_not_consumed(self):
        # consume_db_events *clears* this file. A phase with no discovery of its
        # own that ate the signal would lose same-day capture for every new DB.
        ev = self.spath("webhook-db-events.json")
        refresh.jsave(ev, {"dd" * 16: {"type": "database.created", "parent": "", "parent_type": ""}})
        before = open(ev, "rb").read()
        self.run_rows(self.api, [self.A])
        self.assertEqual(open(ev, "rb").read(), before)

    def test_a_row_whose_database_is_not_mirrored_is_recorded_for_discovery(self):
        other_db = "ee" * 16
        api = FakeApi({self.A: page(self.A, other_db)})
        discovered = set()
        self.run_rows(api, [self.A], discovered)
        self.assertEqual(self.report["rows"]["refreshed"], [])
        self.assertEqual(len(self.report["rows"]["skipped"]), 1)
        self.assertIn("db:" + other_db, discovered)

    def test_a_deleted_row_is_reported_not_fatal(self):
        self.run_rows(self.api, ["f" * 32, self.A])
        self.assertEqual(self.report["rows"]["errors"][0]["row"], "f" * 32)
        self.assertEqual(self.probed, [self.A], "the run continues past a dead id")

    def test_a_budget_wall_inside_the_probe_is_not_counted_as_refreshed(self):
        # upsert_row_md absorbs a mid-probe Budget by queueing the row; a rows run
        # discards that queue, so nothing would ever come back for it.
        def budget_probe(*a, **kw):
            raise refresh.Budget()
        refresh.probe_row = budget_probe
        self.run_rows(self.api, [self.A])
        self.assertEqual(self.report["rows"]["refreshed"], [])
        self.assertTrue(self.report["budget_exhausted"])
        self.assertIn("budget", self.report["rows"]["errors"][0]["error"])
        self.assertEqual(self.state_dict["queue"], [])

    def test_budget_exhaustion_stops_cleanly(self):
        api = FakeApi({rid: page(rid, self.DB_ID, f"Row {rid[0]}")
                       for rid in (self.A, self.B, self.C)}, budget=2)
        self.run_rows(api, [self.A, self.B, self.C])
        self.assertTrue(self.report["budget_exhausted"])
        self.assertEqual(len(self.report["rows"]["refreshed"]), 2)


class ProbePolicyOverrideTest(MirrorTestCase):
    """A named row is probed whatever db_probe_policy would say about its DB."""

    RID = "a" * 32

    def setUp(self):
        super().setUp()
        # 300 bare rows: db_probe_policy would return "enriched" for this DB, and
        # cache that verdict for 7 days.
        for n in range(300):
            self.seed_row(f"{n:032x}", f"Row {n}")
        self.seed_row(self.RID, "Row One")  # bare too: no stored enrichment
        self.write_csv([[self.RID, "Row One", "old"]])
        self.api = FakeApi({self.RID: page(self.RID, self.DB_ID, "Row One", Status="new")})

    def test_a_bare_row_in_a_sparse_db_is_still_probed(self):
        self.assertEqual(
            refresh.db_probe_policy(self.dirpath, 301, {"probe_policy": {}}, self.DB_ID),
            "enriched", "fixture no longer reproduces the policy this test is about")
        self.run_rows(self.api, [self.RID])
        self.assertEqual(self.probed, [self.RID])

    def test_the_policy_cache_is_neither_read_nor_written(self):
        # Reading it would let a stale 7-day verdict make the no-op sticky.
        with mock.patch.object(refresh, "db_probe_policy",
                               mock.Mock(side_effect=AssertionError("policy consulted"))):
            self.run_rows(self.api, [self.RID])
        self.assertEqual(self.state_dict["probe_policy"], {})


class RenderTest(MirrorTestCase):
    RID = "a" * 32

    def setUp(self):
        super().setUp()
        self.seed_row(self.RID, "Row One", status="old")
        self.write_csv([[self.RID, "Row One", "old"]])
        self.api = FakeApi({self.RID: page(self.RID, self.DB_ID, "Row One", Status="new")})

    def test_the_property_table_and_the_csv_line_move_together(self):
        self.run_rows(self.api, [self.RID])
        self.assertIn("| Status | new |", self.row_text(self.RID))
        _header, rows = refresh.read_csv(self.csv_path)
        self.assertEqual(rows[self.RID], [self.RID, "Row One", "new"])

    def test_a_missing_csv_skips_the_row_rather_than_blanking_its_table(self):
        # render_row_md builds the property table from the CSV's columns, so
        # rendering with none would empty the table of every row it touched.
        os.remove(self.csv_path)
        before = self.row_text(self.RID)
        self.run_rows(self.api, [self.RID])
        self.assertEqual(self.row_text(self.RID), before)
        self.assertEqual(self.report["rows"]["refreshed"], [])
        self.assertIn("no CSV header", self.report["rows"]["skipped"][0]["why"])

    def test_a_renamed_row_moves_its_file(self):
        api = FakeApi({self.RID: page(self.RID, self.DB_ID, "Row Renamed", Status="new")})
        self.run_rows(api, [self.RID])
        self.assertFalse(os.path.exists(os.path.join(self.dirpath, f"Row One {self.RID}.md")))
        self.assertIn("# Row Renamed", self.row_text(self.RID, "Row Renamed"))


class DiscoveryThreadingTest(MirrorTestCase):
    """A database first seen inside a row body must outlive the run.

    A rows run has no discovery phase — that is a full /search, a nightly cost —
    and an inline child database is precisely what /search does not return. Drop
    the id at function exit and the DB stays unmirrored with nothing recording
    that it was ever seen.
    """

    RID = "a" * 32
    CHILD = "cd" * 16

    def setUp(self):
        super().setUp()
        self.seed_row(self.RID)
        self.write_csv([[self.RID, "Row One", "old"]])
        self.api = FakeApi({self.RID: page(self.RID, self.DB_ID, "Row One", Status="new")})
        self.stub_probe(
            "\n\n## Body\n\n  - 🗄️ **Inline** — database `%s` (rows in workspace/_databases/)\n"
            % self.CHILD)

    def test_a_child_database_in_the_body_reaches_discovered_and_disk(self):
        discovered = set()
        self.run_rows(self.api, [self.RID], discovered)
        self.assertIn("db:" + self.CHILD, discovered)
        self.assertEqual(refresh.jload(self.spath("pending-discovery.json"), []),
                         ["db:" + self.CHILD])

    def test_persisting_is_additive(self):
        refresh.jsave(self.spath("pending-discovery.json"), ["db:" + "ff" * 16])
        self.run_rows(self.api, [self.RID], set())
        self.assertEqual(refresh.jload(self.spath("pending-discovery.json"), []),
                         sorted(["db:" + "ff" * 16, "db:" + self.CHILD]))


class ReportStemTest(unittest.TestCase):
    """The nightly's machine report must survive an hourly rows run.

    refresh.sh builds its commit summary from last-run-report.json and cats
    last-run-report.md into the stub note; `reanalyze` reads the same pair.
    """

    def test_rows_mode_writes_its_own_stem(self):
        self.assertEqual(refresh.report_stem("rows", False), "last-run-report.rows")
        self.assertEqual(refresh.report_stem("rows", True), "last-run-report.rows.dry-run")

    def test_every_other_mode_keeps_the_nightly_stem(self):
        for mode in ("daily", "full-comments", "place"):
            self.assertEqual(refresh.report_stem(mode, False), "last-run-report")
            self.assertEqual(refresh.report_stem(mode, True), "last-run-report.dry-run")


class ModeWiringTest(unittest.TestCase):

    def test_rows_is_an_accepted_mode_with_a_budget(self):
        # :budget is a bare dict index, so a missing key is a KeyError at startup
        # rather than a mode that runs unbudgeted.
        self.assertIn("rows", refresh.MODE_BUDGETS)
        self.assertGreater(refresh.MODE_BUDGETS["rows"], 0)

    def test_row_ids_are_parsed_and_validated(self):
        a, b = "a" * 32, "b" * 32
        self.assertEqual(refresh.parse_row_ids(f"{a},{refresh.dashed(b)}"), [a, b])
        self.assertEqual(refresh.parse_row_ids(f" {a}  {b} "), [a, b])
        with self.assertRaises(ValueError):
            refresh.parse_row_ids("not-an-id")
        with self.assertRaises(ValueError):
            refresh.parse_row_ids("")


if __name__ == "__main__":
    unittest.main()
