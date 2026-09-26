"""One data-source search feeds the row sweep, the schema sweep and discovery.

At 2026-03-11 a search filtered to data sources returns every shared source as
the full object `GET /data_sources/{id}` returns (checked field for field on
2026-09-26), each naming its database in `parent`. The nightly reads it once,
about 8 requests, and then needs neither the per-database `GET /databases/{id}`
the row sweep made to learn source ids nor the two GETs per database the schema
sweep made: together about 2,200 requests a night. A database the search does
not list, or lists with more than one source, is read directly, as before.
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

DB1, DS1 = "a1" * 16, "d1000000-0000-0000-0000-000000000001"
DB2, DS2A, DS2B = "b2" * 16, "d2000000-0000-0000-0000-00000000000a", "d2000000-0000-0000-0000-00000000000b"
DB3, DS3 = "c3" * 16, "d3000000-0000-0000-0000-000000000003"
PAGE = "9f" * 16
PROPS = {"Name": {"id": "title", "name": "Name", "type": "title", "title": {}}}


def source(ds_id, db_id, title):
    return {"object": "data_source", "id": ds_id, "cover": None, "icon": None,
            "created_time": "2025-01-01T00:00:00.000Z", "last_edited_time": "2026-09-01T00:00:00.000Z",
            "title": [{"plain_text": title}], "description": [], "is_inline": False,
            "database_type": None, "properties": PROPS,
            "parent": {"type": "database_id", "database_id": refresh.dashed(db_id)},
            "database_parent": {"type": "page_id", "page_id": refresh.dashed(PAGE)},
            "url": "u", "public_url": None, "in_trash": False}


class Api:
    """The search lists DB1 (one source) and DB2 (two); DB3 is not in it."""

    def __init__(self, search_error=None):
        self.search_error = search_error
        self.calls = []
        self.n = 0

    def paginate(self, method, path, body=None, params=None, ver=None):
        self.calls.append((method, path, ((body or {}).get("filter") or {}).get("value")))
        if self.search_error:
            raise self.search_error
        yield source(DS1, DB1, "One")
        yield source(DS2A, DB2, "Two A")
        yield source(DS2B, DB2, "Two B")

    def get(self, path, params=None, ver=None):
        self.calls.append(("GET", path, None))
        if path == f"/databases/{refresh.dashed(DB3)}":
            return {"object": "database", "id": refresh.dashed(DB3), "title": [],
                    "data_sources": [{"id": DS3, "name": "Three"}]}
        if path == f"/data_sources/{DS3}":
            return source(DS3, DB3, "Three")
        if path == f"/databases/{refresh.dashed(DB2)}":
            return {"object": "database", "id": refresh.dashed(DB2), "title": [{"plain_text": "Two"}],
                    "data_sources": [{"id": DS2A, "name": "Two A"}, {"id": DS2B, "name": "Two B"}]}
        if path in (f"/data_sources/{DS2A}", f"/data_sources/{DS2B}"):
            return source(path.rsplit("/", 1)[1], DB2, "Two")
        raise AssertionError(f"unexpected GET {path}")

    def query_rows(self, path, body=None, ver=None):
        self.calls.append(("POST", path, None))
        return []


class Catalog(unittest.TestCase):
    def setUp(self):
        self.report = refresh.new_report("daily")

    def loaded(self, **kw):
        api = Api(**kw)
        refresh.load_source_catalog(api, self.report)
        api.calls.clear()
        return api

    def test_the_catalog_groups_sources_by_database(self):
        api = Api()
        refresh.load_source_catalog(api, self.report)
        self.assertEqual(api.calls, [("POST", "/search", "data_source")])
        self.assertEqual(sorted(api.catalog), sorted([DB1, DB2]))
        self.assertEqual([s["id"] for s in api.catalog[DB2]], [DS2A, DS2B])

    def test_a_failed_search_leaves_no_catalog(self):
        api = Api(search_error=refresh.ApiError(500, "boom"))
        refresh.load_source_catalog(api, self.report)
        self.assertIsNone(getattr(api, "catalog", None))
        self.assertTrue(any("data-source search failed" in n for n in self.report["notes"]))

    def test_a_single_source_database_is_queried_without_a_get(self):
        api = self.loaded()
        refresh.query_db_rows(api, DB1)
        self.assertEqual(api.calls, [("POST", f"/data_sources/{DS1}/query", None)])

    def test_its_schema_costs_nothing(self):
        api = self.loaded()
        d, sources = refresh.get_database(api, DB1)
        self.assertEqual(api.calls, [])
        self.assertEqual(d["id"], refresh.dashed(DB1))
        self.assertEqual(d["object"], "database")
        self.assertEqual(d["parent"], {"type": "page_id", "page_id": refresh.dashed(PAGE)})
        self.assertEqual(refresh.plain(d["title"]), "One")
        self.assertEqual([s["id"] for s in sources], [DS1])

    def test_the_block_is_the_one_a_direct_read_writes(self):
        cached, _ = refresh.get_database(self.loaded(), DB1)
        direct = Api()
        direct.get = lambda path, params=None, ver=None: (
            {"object": "database", "id": refresh.dashed(DB1), "data_sources": [{"id": DS1}]}
            if path.startswith("/databases/") else source(DS1, DB1, "One"))
        read, _ = refresh.get_database(direct, DB1)
        self.assertEqual(list(cached.items()), list(read.items()))

    def test_a_multi_source_database_is_read_directly(self):
        api = self.loaded()
        refresh.get_database(api, DB2)
        refresh.query_db_rows(api, DB2)
        self.assertIn(("GET", f"/databases/{refresh.dashed(DB2)}", None), api.calls)

    def test_a_database_the_search_misses_is_read_directly(self):
        api = self.loaded()
        refresh.get_database(api, DB3)
        refresh.query_db_rows(api, DB3)
        gets = [c for c in api.calls if c[0] == "GET"]
        self.assertEqual(gets[0], ("GET", f"/databases/{refresh.dashed(DB3)}", None))
        self.assertIn(("POST", f"/data_sources/{DS3}/query", None), api.calls)

    def test_without_a_catalog_nothing_changes(self):
        api = Api()
        refresh.query_db_rows(api, DB3)
        self.assertEqual(api.calls[0], ("GET", f"/databases/{refresh.dashed(DB3)}", None))


class Discovery(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="catalog-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        dbs = os.path.join(self.root, "_databases")
        os.makedirs(os.path.join(dbs, f"One {DB1}"))
        p = mock.patch.object(refresh, "DBS", dbs)
        p.start()
        self.addCleanup(p.stop)
        self.report = refresh.new_report("daily")
        self.state = {"not_a_db": {}, "db404": {}, "unshared": {}}

    def test_discovery_reads_the_catalog_not_a_second_search(self):
        api = Api()
        refresh.load_source_catalog(api, self.report)
        api.calls.clear()
        with mock.patch.object(refresh, "capture_new_db") as cap:
            refresh.phase_discovery(api, None, self.state, self.report,
                                    types.SimpleNamespace(dry_run=False), set())
        self.assertEqual(api.calls, [])
        self.assertEqual([c.args[2] for c in cap.call_args_list], [DB2])

    def test_without_a_catalog_discovery_searches(self):
        api = Api()
        with mock.patch.object(refresh, "capture_new_db") as cap:
            refresh.phase_discovery(api, None, self.state, self.report,
                                    types.SimpleNamespace(dry_run=False), set())
        self.assertEqual(api.calls, [("POST", "/search", "data_source")])
        self.assertEqual([c.args[2] for c in cap.call_args_list], [DB2])


if __name__ == "__main__":
    unittest.main()
