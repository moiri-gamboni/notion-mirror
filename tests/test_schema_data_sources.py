"""What `_schema.json` records about a database's data sources.

The `database` block in that file is a fresh `GET /databases/{id}` on every
schema pass. The `data_sources` block beside it was not: the merge read

    "data_sources": old.get("data_sources") or d.get("data_sources") or []

and the engine fetches databases at 2022-06-28 — a version whose response
carries no `data_sources` at all, the field having arrived with 2025-09-03 — so
the second arm was dead and the first carried one snapshot forward forever. On the
mirror this was measured against (2026-09-22), 401 of 754 schema files carried
a block; 100 of those held a property map that no longer matched the `database`
block beside it, the furthest 208 days behind that database's own last edit.
One database showed 54 properties there against 91 in `database.properties`,
which reads as a mirror that has lost half a schema — and cost a reader a live
API call to find out it had not.

The id in that block is not the same kind of thing as the schema in it: a data
source id is fixed for the life of the data source, and it is what the block is
read for, since the 2025-09-03 endpoints address rows by data source and not by
database. So the id is carried and the schema snapshot is dropped — and nothing
mutable joins it, because the two phases that write this file in one run cannot
both supply the same fields.

No network: the API is a stub that serves one payload and counts its calls.
"""
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import refresh  # noqa: E402  (notion-mirror is not a package; discover's top dir is tests/)

DB_ID = "cd" * 16
ROW_ID = "11" * 16
DS_ID = "4c000000-0000-0000-0000-000000000000"
OTHER_DS_ID = "4d000000-0000-0000-0000-000000000000"


def prop(name, ptype="rich_text", description=""):
    return {name: {"id": name[:4], "name": name, "type": ptype, ptype: {},
                   "description": description}}


def database(props, title="Contacts"):
    """A `GET /databases/{id}` response at 2022-06-28: properties, no data_sources."""
    return {"object": "database", "id": refresh.dashed(DB_ID),
            "title": [{"plain_text": title}],
            "last_edited_time": "2026-09-22T09:08:00.000Z",
            "properties": props}


def stored_data_source(props):
    """The shape actually found on disk: a whole `GET /v1/data_sources/{id}`
    response, `request_id` and all, from tooling that predates this engine."""
    return {"object": "data_source", "id": DS_ID, "request_id": "req-0",
            "title": [{"plain_text": "Contacts"}],
            "last_edited_time": "2026-06-27T06:26:00.000Z",
            "properties": props}


class FakeApi:
    """Serves one `/databases/{id}` payload. Any other request is a test bug."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, path, params=None, ver=refresh.VER):
        self.calls.append((path, ver))
        if path.startswith("/databases/"):
            return dict(self.payload, request_id="req-1")
        raise AssertionError(f"unexpected request: {path}")


class SchemaDataSourcesTest(unittest.TestCase):
    """What `refresh_schema_files` writes into `data_sources`."""

    OLD_PROPS = dict(prop("Name", "title"), **prop("Retired Field", "select"))
    NEW_PROPS = dict(prop("Name", "title"), **prop("Added Field", "select"))

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="schema-ds-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.dbs = os.path.join(self.root, "_databases")
        self.dirpath = os.path.join(self.dbs, f"Contacts {DB_ID}")
        os.makedirs(self.dirpath)
        p = mock.patch.object(refresh, "DBS", self.dbs)
        p.start()
        self.addCleanup(p.stop)
        self.report = refresh.new_report("daily")

    def seed(self, db_block, data_sources):
        refresh.jsave(os.path.join(self.dirpath, "_schema.json"),
                      {"id": DB_ID, "title": "Contacts", "database": db_block,
                       "data_sources": data_sources})

    def run_schema(self, api, **kw):
        refresh.refresh_schema_files(api, self.dirpath, DB_ID, "Contacts", 3, self.report, **kw)
        return refresh.jload(os.path.join(self.dirpath, "_schema.json"), {})

    def test_the_schema_snapshot_in_a_carried_block_is_dropped(self):
        """The whole reason this is a bug: the snapshot sits beside a freshly
        fetched `database.properties` with nothing marking it as months old."""
        self.seed(database(self.OLD_PROPS), [stored_data_source(self.OLD_PROPS)])
        written = self.run_schema(FakeApi(database(self.NEW_PROPS)))
        self.assertEqual(written["data_sources"], [{"id": DS_ID}])

    def test_the_data_source_id_survives(self):
        """The half of the block that is not stale. A data source id is fixed for
        the life of the data source, it is what the 2025-09-03 query endpoint
        needs, and nothing in the engine can re-fetch it — dropping it would cost
        a live request every time someone wants to query the database."""
        self.seed(database(self.OLD_PROPS), [stored_data_source(self.OLD_PROPS)])
        written = self.run_schema(FakeApi(database(self.NEW_PROPS)))
        self.assertEqual([e["id"] for e in written["data_sources"]], [DS_ID])

    def test_a_stale_block_is_rewritten_when_nothing_else_changed(self):
        """The rewrite predicate compared the `database` block and the title and
        nothing else, so a corrected `data_sources` would have been computed and
        then thrown away on every database whose schema happened to be steady."""
        self.seed(database(self.NEW_PROPS), [stored_data_source(self.OLD_PROPS)])
        written = self.run_schema(FakeApi(database(self.NEW_PROPS)))
        self.assertEqual(written["data_sources"], [{"id": DS_ID}])

    def test_a_second_run_over_a_corrected_file_writes_nothing_new(self):
        """It has to converge, or every schema file churns in git nightly."""
        self.seed(database(self.NEW_PROPS), [{"id": DS_ID}])
        path = os.path.join(self.dirpath, "_schema.json")
        with open(path) as f:
            before = f.read()
        self.run_schema(FakeApi(database(self.NEW_PROPS)))
        with open(path) as f:
            self.assertEqual(f.read(), before)

    def test_freshly_fetched_data_sources_replace_the_carried_ids(self):
        """A caller that actually holds a live list — `query_db_rows` fetches one
        for every multi-source database — overrides what is on disk, so an added
        or removed data source lands."""
        self.seed(database(self.NEW_PROPS), [stored_data_source(self.OLD_PROPS)])
        written = self.run_schema(
            FakeApi(database(self.NEW_PROPS)),
            data_sources=[{"id": DS_ID, "name": "Contacts"},
                          {"id": OTHER_DS_ID, "name": "Contacts (archive)"}])
        self.assertEqual(written["data_sources"], [{"id": DS_ID}, {"id": OTHER_DS_ID}])

    def test_the_block_is_the_same_whichever_caller_wrote_it(self):
        """`phase_dbs` (which can hold a live list) runs before
        `phase_schema_sweep` (which never does) in the same process, and the run
        commits once, at the end. A field only the first can supply would be
        written and then stripped again — dead on arrival, and two rewrites of
        every such file per night."""
        self.seed(database(self.NEW_PROPS), [])
        self.run_schema(FakeApi(database(self.NEW_PROPS)),
                        data_sources=[{"id": DS_ID, "name": "Contacts"}])
        path = os.path.join(self.dirpath, "_schema.json")
        with open(path) as f:
            after_first_pass = f.read()
        self.run_schema(FakeApi(database(self.NEW_PROPS)))
        with open(path) as f:
            self.assertEqual(f.read(), after_first_pass)

    def test_an_empty_live_list_does_not_erase_the_recorded_ids(self):
        """Every Notion database has at least one data source, so an empty live
        list is a bad answer rather than news — and nothing else in the engine
        can re-fetch an id it erased."""
        self.seed(database(self.NEW_PROPS), [{"id": DS_ID}])
        written = self.run_schema(FakeApi(database(self.NEW_PROPS)), data_sources=[])
        self.assertEqual(written["data_sources"], [{"id": DS_ID}])

    def test_a_database_with_no_recorded_data_sources_stays_empty(self):
        """Nothing is invented for the 353 files whose block the engine's own
        capture path wrote as `[]` — that gap is a missing fetch, not a value to
        guess at."""
        self.seed(database(self.OLD_PROPS), [])
        written = self.run_schema(FakeApi(database(self.NEW_PROPS)))
        self.assertEqual(written["data_sources"], [])

    def test_the_schema_pass_still_costs_one_request_per_database(self):
        """The cost ceiling on any fix here: the nightly schema phase is one GET
        per database — 750 of ~11,200 requests on the 2026-09-22 run — and a
        second one per database to refresh this block would double the phase
        against a budget the run already spends 75% of."""
        self.seed(database(self.OLD_PROPS), [stored_data_source(self.OLD_PROPS)])
        api = FakeApi(database(self.NEW_PROPS))
        self.run_schema(api)
        self.assertEqual([ver for _p, ver in api.calls], [refresh.VER])


class MultiSourceApi:
    """A multi-source database: the 2022-06-28 row query refuses it, and the
    2025-09-03 database object carries the data sources to query instead."""

    def __init__(self):
        self.n = 0
        self.gets = []

    def query_rows(self, path, body=None, ver=refresh.VER):
        if path.startswith("/databases/"):
            raise refresh.ApiError(400, "Your integration must specify a data source to query")
        assert path == f"/data_sources/{DS_ID}/query", path
        return [{"id": refresh.dashed(ROW_ID),
                 "properties": {"Name": {"id": "title", "type": "title",
                                         "title": [{"plain_text": "Row One"}]}},
                 "parent": {"type": "database_id", "database_id": refresh.dashed(DB_ID)},
                 "last_edited_time": "2026-09-22T09:08:00.000Z"}]

    def get(self, path, params=None, ver=refresh.VER):
        self.gets.append((path, ver))
        assert ver == refresh.VER_DS, ver
        return {"object": "database", "id": refresh.dashed(DB_ID),
                "data_sources": [{"id": DS_ID, "name": "Contacts"}]}


class MultiSourceForwardingTest(unittest.TestCase):
    """`query_db_rows` pays for a live data-source list whenever a database has
    more than one, and `refresh_db` dropped it: `rows, ds_extra = ...` and then
    `ds_extra` was never read again. It is the only fresh `data_sources` value
    the engine ever holds."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="schema-ds-multi-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.dbs = os.path.join(self.root, "_databases")
        self.dirname = f"Contacts {DB_ID}"
        self.dirpath = os.path.join(self.dbs, self.dirname)
        os.makedirs(self.dirpath)
        p = mock.patch.object(refresh, "DBS", self.dbs)
        p.start()
        self.addCleanup(p.stop)
        refresh.jsave(os.path.join(self.dirpath, "_schema.json"),
                      {"id": DB_ID, "title": "Contacts", "database": {"properties": {}},
                       "data_sources": []})
        # a column no row carries any more is what sends `refresh_db` to the
        # schema files mid-sweep, to check whether it was really removed
        with open(os.path.join(self.dirpath, f"Contacts {DB_ID}.csv"), "w") as f:
            f.write(f"_row_id,Name,Ghost\n{ROW_ID},Row One,\n")
        self.seen = []
        real = refresh.refresh_schema_files
        self.addCleanup(setattr, refresh, "refresh_schema_files", real)

        def spy(api, dirpath, db_id, title, nrows, report, force=False, data_sources=None):
            self.seen.append(data_sources)
            return {}, title
        refresh.refresh_schema_files = spy

    def test_the_data_sources_the_row_query_fetched_reach_the_schema_file(self):
        state = {"rows": {}, "db404": {}, "queue": [], "comment_rows": {}, "probe_policy": {}}
        refresh.refresh_db(MultiSourceApi(), None, DB_ID, self.dirname, state,
                           refresh.new_report("daily"),
                           types.SimpleNamespace(dry_run=True), set())
        self.assertEqual(self.seen, [[{"id": DS_ID, "name": "Contacts"}]])


if __name__ == "__main__":
    unittest.main()
