"""A database with more than one data source is mirrored, not refused.

Its schema block is the database container with each source's
`GET /data_sources/{id}` properties merged, and `_schema.md` names the sources;
a single-source database's block is its one data source and names none.

Separately: a database whose object answers but whose rows 404 (its data source
is not shared with the integration) was captured as new and deleted again after
two 404s, every night. It now goes into the `unshared` bucket instead.
"""
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import refresh  # noqa: E402

DB_ID = "5e" * 16
DS1, DS2 = "00000000-0000-0000-0000-0000000011bf", "00000000-0000-0000-0000-000000006e45"
MULTI = ('{"object":"error","status":400,"code":"validation_error","message":"Databases with '
         'multiple data sources are not supported in this API version."}')


def prop(name, ptype="rich_text"):
    return {name: {"id": name[:4], "name": name, "type": ptype, ptype: {}}}


class MultiApi:
    def __init__(self, multi=True, rows_404=False):
        self.multi, self.rows_404 = multi, rows_404
        self.calls = []

    def get(self, path, params=None, ver=None):
        self.calls.append((path, ver))
        if path.startswith("/databases/"):
            srcs = [{"id": DS1, "name": "Publications"}]
            if self.multi:
                srcs.append({"id": DS2, "name": "New data source"})
            return {"object": "database", "id": refresh.dashed(DB_ID),
                    "title": [{"plain_text": "Publications"}], "request_id": "r", "data_sources": srcs}
        if path == f"/data_sources/{DS1}":
            return {"object": "data_source", "title": [{"plain_text": "Publications"}],
                    "properties": dict(prop("Name", "title"), **prop("URL", "url"))}
        if path == f"/data_sources/{DS2}":
            return {"object": "data_source", "properties": dict(prop("Name", "title"), **prop("Status", "select"))}
        raise AssertionError(f"unexpected GET {path}")

    def query_rows(self, path, body=None, ver=None):
        self.calls.append((path, ver))
        if self.rows_404:
            raise refresh.ApiError(404, "object_not_found")
        return []


class MultiSource(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="multi-ds-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.dbs = os.path.join(self.root, "_databases")
        self.dirpath = os.path.join(self.dbs, f"Publications {DB_ID}")
        os.makedirs(self.dirpath)
        p = mock.patch.object(refresh, "DBS", self.dbs)
        p.start()
        self.addCleanup(p.stop)
        self.report = refresh.new_report("daily")

    def test_a_single_source_database_has_no_sources_line(self):
        api = MultiApi(multi=False)
        d, sources = refresh.get_database(api, DB_ID)
        self.assertEqual(sources, [{"id": DS1, "name": "Publications"}])
        self.assertEqual([p for p, _v in api.calls],
                         [f"/databases/{refresh.dashed(DB_ID)}", f"/data_sources/{DS1}"])
        self.assertNotIn("Data sources", refresh.schema_md("P", DB_ID, 1, d["properties"], sources))

    def test_a_multi_source_schema_merges_every_source(self):
        props, title = refresh.refresh_schema_files(MultiApi(), self.dirpath, DB_ID, "Publications",
                                                    6, self.report)
        self.assertEqual(list(props), ["Name", "URL", "Status"])
        self.assertEqual(self.report["dbs"]["errors"], [])
        stored = refresh.jload(os.path.join(self.dirpath, "_schema.json"), {})
        self.assertEqual(stored["data_sources"], [{"id": DS1}, {"id": DS2}])
        self.assertEqual(list(stored["database"]["properties"]), ["Name", "URL", "Status"])
        md = open(os.path.join(self.dirpath, "_schema.md")).read()
        self.assertIn("Data sources: Publications `000000000000000000000000000011bf`, "
                      "New data source `00000000000000000000000000006e45`", md)
        self.assertIn("| Status | select |", md)

    def test_a_database_listing_no_source_keeps_its_stored_schema(self):
        class Empty(MultiApi):
            def get(self, path, params=None, ver=None):
                return {"object": "database", "title": [{"plain_text": "P"}], "data_sources": []}
        props, _t = refresh.refresh_schema_files(Empty(), self.dirpath, DB_ID, "P", 1, self.report)
        self.assertEqual(props, {})
        self.assertIn("no data source", self.report["dbs"]["errors"][0]["error"])
        self.assertFalse(os.path.exists(os.path.join(self.dirpath, "_schema.json")))

    def test_another_400_is_still_an_error(self):
        class Refusing(MultiApi):
            def get(self, path, params=None, ver=refresh.VER):
                raise refresh.ApiError(400, '{"message":"something else"}')
        refresh.refresh_schema_files(Refusing(), self.dirpath, DB_ID, "P", 1, self.report)
        self.assertEqual(len(self.report["dbs"]["errors"]), 1)

    def state(self):
        return {"not_a_db": {}, "db404": {DB_ID: 63}, "unshared": {}, "rows": {},
                "probe_policy": {}, "comment_rows": {}}

    def test_a_new_multi_source_database_is_captured_not_flagged(self):
        st = self.state()
        with mock.patch.object(refresh, "refresh_db"):
            refresh.capture_new_db(MultiApi(), None, "77" * 16, st, self.report,
                                   types.SimpleNamespace(dry_run=False))
        self.assertEqual(st["not_a_db"], {})
        self.assertEqual(self.report["dbs"]["new"][0]["title"], "Publications")

    def test_a_database_whose_rows_are_not_shared_goes_unshared_not_new(self):
        st = self.state()
        refresh.capture_new_db(MultiApi(multi=False, rows_404=True), None, DB_ID, st, self.report,
                               types.SimpleNamespace(dry_run=False))
        self.assertIn(DB_ID, st["unshared"])
        self.assertNotIn(DB_ID, st["db404"])
        self.assertEqual(self.report["dbs"]["new"], [])
        self.assertEqual(sorted(os.listdir(self.dbs)), [f"Publications {DB_ID}"], "no new directory")
        self.assertTrue(refresh.skip_discovery(DB_ID, set(), st))


if __name__ == "__main__":
    unittest.main()
