"""Api.query_rows survives Notion's per-query result cap (QUERY_CAP).

Notion caps a single data-source/database query at 10,000 results: pagination
stops with has_more=false and only `request_status` (absent from 2022-06-28
responses) marks the result incomplete. Before the windowed sweep, refresh_db
diffed the missing overflow as deletions — several hundred live rows of one
large database were tombstoned that way. These tests pin the recovery (created_time
windows, boundary-tie dedupe), the 2022 no-marker heuristic, the unwindowable-tie
Truncated, and that refresh_db skips rather than diffs on Truncated.

Fully offline: the "server" answers Api.post from an in-memory row list.
"""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import refresh  # noqa: E402
from notion_core import api as api_mod  # noqa: E402

from test_phase_rows import MirrorTestCase, page  # noqa: E402

CAP = 8       # stand-in for QUERY_CAP
PAGE = 3      # server page size (client's page_size is a maximum, not a promise)


def row(i, ts):
    return {"id": f"00000000-0000-0000-0000-{i:012d}", "created_time": ts,
            "last_edited_time": ts, "properties": {}}


class Server:
    """Answers Api.post like Notion's query endpoint: created_time-ascending
    sort required, optional created_time on_or_after window filter, results
    capped at CAP per query, cursor pages of PAGE. marker=True appends the
    documented request_status on a capped final page; marker=False is the
    2022-06-28 endpoint, which never says."""

    def __init__(self, rows, marker=True):
        self.rows = rows
        self.marker = marker
        self.queries = 0

    def post(self, path, body=None, ver=None):
        b = body or {}
        assert b.get("sorts") == [{"timestamp": "created_time", "direction": "ascending"}], \
            "query_rows must impose the created_time ascending sort"
        rows = sorted(self.rows, key=lambda r: (r["created_time"], r["id"]))
        flt = b.get("filter")
        if flt:
            conds = [flt] if flt.get("timestamp") == "created_time" else \
                [f for f in flt.get("and", []) if f.get("timestamp") == "created_time"]
            for w in conds:
                rows = [r for r in rows if r["created_time"] >= w["created_time"]["on_or_after"]]
        capped = rows[:CAP]
        start = int(b.get("start_cursor") or 0)
        if start == 0:
            self.queries += 1
        out = capped[start:start + PAGE]
        has_more = start + PAGE < len(capped)
        d = {"results": out, "has_more": has_more,
             "next_cursor": str(start + PAGE) if has_more else None}
        if self.marker and not has_more and len(rows) > CAP:
            d["request_status"] = {"type": "incomplete",
                                   "incomplete_reason": "query_result_limit_reached"}
        return d


def client(server):
    a = api_mod.Api(token="t", rps=10_000, budget=10**9)
    a.post = server.post
    return a


def ids(rows):
    return [r["id"] for r in rows]


@mock.patch.object(api_mod, "QUERY_CAP", CAP)
class QueryRowsTest(unittest.TestCase):

    def test_complete_small_query_is_one_window(self):
        srv = Server([row(i, f"2026-01-{i + 1:02d}T00:00:00.000Z") for i in range(5)])
        got = client(srv).query_rows("/databases/x/query")
        self.assertEqual(len(got), 5)
        self.assertEqual(srv.queries, 1)

    def test_windows_past_cap_with_marker(self):
        rows = [row(i, f"2026-01-01T00:00:{i:02d}.000Z") for i in range(CAP + 4)]
        srv = Server(rows)
        got = client(srv).query_rows("/databases/x/query")
        self.assertEqual(sorted(ids(got)), sorted(ids(rows)))
        self.assertEqual(len(got), len(set(ids(got))), "boundary tie must be de-duplicated")
        self.assertGreater(srv.queries, 1)

    def test_boundary_tie_dedupes(self):
        # a tie block straddles the cap boundary: rows 5..9 share one timestamp
        rows = [row(i, f"2026-01-01T00:00:{min(i, 5):02d}.000Z") for i in range(CAP + 4)]
        got = client(Server(rows)).query_rows("/databases/x/query")
        self.assertEqual(sorted(ids(got)), sorted(ids(rows)))

    def test_2022_endpoint_no_marker_past_cap(self):
        rows = [row(i, f"2026-01-01T00:00:{i:02d}.000Z") for i in range(CAP + 4)]
        got = client(Server(rows, marker=False)).query_rows("/databases/x/query")
        self.assertEqual(sorted(ids(got)), sorted(ids(rows)))

    def test_2022_endpoint_exactly_at_cap_terminates(self):
        rows = [row(i, f"2026-01-01T00:00:{i:02d}.000Z") for i in range(CAP)]
        srv = Server(rows, marker=False)
        got = client(srv).query_rows("/databases/x/query")
        self.assertEqual(sorted(ids(got)), sorted(ids(rows)))
        self.assertEqual(srv.queries, 2, "one boundary re-query, then done")

    def test_unwindowable_tie_raises_truncated(self):
        rows = [row(i, "2026-01-01T00:00:00.000Z") for i in range(CAP + 1)]
        with self.assertRaises(api_mod.Truncated):
            client(Server(rows)).query_rows("/databases/x/query")

    def test_base_filter_is_preserved_in_windows(self):
        rows = [row(i, f"2026-01-01T00:00:{i:02d}.000Z") for i in range(CAP + 4)]
        srv = Server(rows)
        seen_filters = []
        real = srv.post

        def spy(path, body=None, ver=None):
            seen_filters.append((body or {}).get("filter"))
            return real(path, body, ver)

        srv.post = spy
        base = {"filter": {"timestamp": "created_time",
                           "created_time": {"on_or_after": "2020-01-01T00:00:00.000Z"}}}
        got = client(srv).query_rows("/databases/x/query", body=base)
        self.assertEqual(sorted(ids(got)), sorted(ids(rows)))
        windowed = [f for f in seen_filters if f and "and" in f]
        self.assertTrue(windowed, "window filters must AND with the caller's filter")


@mock.patch.object(api_mod, "QUERY_CAP", CAP)
class RefreshDbTruncatedTest(MirrorTestCase):
    """Truncated reaches refresh_db as report-and-skip, never as deletions."""

    def _refresh(self, api):
        self.state_dict.setdefault("db404", {})
        refresh.refresh_db(api, None, self.DB_ID, self.dirname, self.state_dict,
                           self.report, self.args, set())

    def test_truncated_sweep_diffs_no_deletions(self):
        rids = ["%032x" % i for i in range(3)]
        self.write_csv([[r, f"Row {i}", "old"] for i, r in enumerate(rids)])
        for i, r in enumerate(rids):
            self.seed_row(r, title=f"Row {i}")

        fake = types.SimpleNamespace(
            query_rows=mock.Mock(side_effect=api_mod.Truncated("capped, unwindowable")),
            get=mock.Mock(side_effect=AssertionError("no fallback expected")),
            n=0)
        self._refresh(fake)

        errors = self.report["dbs"]["errors"]
        self.assertTrue(any("truncated" in e["error"].lower() for e in errors), errors)
        header, rows_after = refresh.read_csv(self.csv_path)
        self.assertEqual(len(rows_after), 3, "no CSV rows may be dropped on a truncated sweep")
        for i, r in enumerate(rids):
            self.assertTrue(os.path.exists(
                os.path.join(self.dirpath, f"Row {i} {r}.md")))

    def test_windowed_recovery_keeps_all_rows(self):
        # 10 live rows, cap 8: the sweep must window and keep every row
        rids = ["%032x" % i for i in range(10)]
        self.write_csv([[r, f"Row {i}", "old"] for i, r in enumerate(rids)])
        for i, r in enumerate(rids):
            self.seed_row(r, title=f"Row {i}")
        pages = []
        for i, r in enumerate(rids):
            p = page(r, self.DB_ID, f"Row {i}", Status="old")
            p["created_time"] = f"2026-01-01T00:00:{i:02d}.000Z"
            pages.append(p)
        srv = Server(pages)
        api = client(srv)
        self._refresh(api)

        self.assertEqual(self.report["dbs"].get("errors", []), [])
        changed = self.report["dbs"]["changed"]
        self.assertTrue(all(c.get("deleted_n", 0) == 0 for c in changed), changed)
        header, rows_after = refresh.read_csv(self.csv_path)
        self.assertEqual(len(rows_after), 10)


class EmptyDataSourceListTest(MirrorTestCase):
    """The other road to an empty row set. `query_db_rows`'s multi-source
    fallback read `srcs = d.get("data_sources") or []` and looped: a database
    that refused the 2022-06-28 query for having several data sources, and then
    came back from the 2025-09-03 fetch listing none, queried nothing and
    returned zero rows — which `refresh_db` would diff as "every row deleted"
    and act on, removing the CSV rows and every row .md. Same tombstoning as the
    query cap, reached without a single truncated response."""

    def test_no_data_sources_to_query_is_truncated_not_an_empty_row_set(self):
        rids = ["%032x" % i for i in range(3)]
        self.write_csv([[r, f"Row {i}", "old"] for i, r in enumerate(rids)])
        for i, r in enumerate(rids):
            self.seed_row(r, title=f"Row {i}")

        fake = types.SimpleNamespace(
            query_rows=mock.Mock(side_effect=api_mod.ApiError(
                400, "Your integration must specify a data source to query")),
            get=mock.Mock(return_value={"object": "database", "data_sources": []}),
            n=0)
        self.state_dict.setdefault("db404", {})
        refresh.refresh_db(fake, None, self.DB_ID, self.dirname, self.state_dict,
                           self.report, self.args, set())

        _header, rows_after = refresh.read_csv(self.csv_path)
        self.assertEqual(len(rows_after), 3, "no CSV rows may be dropped")
        for i, r in enumerate(rids):
            self.assertTrue(os.path.exists(os.path.join(self.dirpath, f"Row {i} {r}.md")),
                            f"row {r} was tombstoned on an empty data-source list")
        errors = self.report["dbs"]["errors"]
        self.assertTrue(any("listed no data source" in e["error"] for e in errors), errors)
        self.assertTrue(any("nothing diffed as deleted" in e["error"] for e in errors), errors)


if __name__ == "__main__":
    unittest.main()


class PaginateFollowsTheCursor(unittest.TestCase):
    """`Api.paginate` is what every child listing goes through, and a task body can
    hold more than the 100 blocks one page returns. Nothing tested it against a real
    cursor: every suite in this repo substitutes a fake that yields the whole list at
    once, so a client that dropped the cursor would have looked green while silently
    truncating every long body to its first page."""

    def api(self, pages, method="GET"):
        """An `Api` whose transport serves `pages` — [(results, next_cursor)] — and
        records the cursor it was asked for, so a dropped one is visible."""
        asked = []

        def call(_self, m, path, body=None, params=None, ver=None):
            self.assertEqual(m, method)
            where = (body or {}) if m == "POST" else (params or {})
            asked.append(where.get("start_cursor"))
            results, nxt = pages[len(asked) - 1]
            return {"results": results, "has_more": nxt is not None, "next_cursor": nxt}

        client = api_mod.Api("t", 1000.0, float("inf"))
        client.call = types.MethodType(call, client)
        return client, asked

    def test_it_walks_every_page_and_returns_them_in_order(self):
        pages = [([{"id": n} for n in range(0, 100)], "c1"),
                 ([{"id": n} for n in range(100, 200)], "c2"),
                 ([{"id": n} for n in range(200, 250)], None)]
        client, asked = self.api(pages)
        got = list(client.paginate("GET", "/blocks/x/children"))
        self.assertEqual([b["id"] for b in got], list(range(250)))
        self.assertEqual(asked, [None, "c1", "c2"], "the cursor was not carried forward")

    def test_it_stops_when_has_more_is_false_rather_than_looping(self):
        client, asked = self.api([([{"id": 1}], None)])
        self.assertEqual(len(list(client.paginate("GET", "/blocks/x/children"))), 1)
        self.assertEqual(asked, [None])

    def test_a_post_carries_the_cursor_in_the_body_not_the_params(self):
        pages = [([{"id": 1}], "c1"), ([{"id": 2}], None)]
        client, asked = self.api(pages, method="POST")
        self.assertEqual(len(list(client.paginate("POST", "/x/query"))), 2)
        self.assertEqual(asked, [None, "c1"])
