"""`props_probe` — a property-only probe kind, on its own queue file.

`page.properties_updated` is 77% of webhook traffic. Routing it to the full
body-and-comments probe made 68% of the probe queue no-op body walks and cost
70% of one day's request budget; excluding it made property changes wait for the
nightly. A probe that fetches the page and re-renders only its property table
costs ~1 request, which makes the event affordable again.

Fully offline: the API is `test_phase_rows.FakeApi`, `refresh.probe_row` is
stubbed to raise (this kind must never reach the body), and every ntfy is
stubbed.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import refresh  # noqa: E402
import webhook_receiver as wr  # noqa: E402
from test_phase_rows import FakeApi, MirrorTestCase, page  # noqa: E402

# The shape of a captured event from _meta/state/webhook-events.jsonl (2026-07-28),
# with its ids replaced by placeholders. data.updated_properties carries property
# *ids* and no values — Notion stating the body did not change.
REAL_PROPERTIES_UPDATED = {
    "received_at": "2026-07-28T10:10:06.669789+00:00",
    "type": "page.properties_updated",
    "entity": {"id": "3a000000-0000-0000-0000-000000000000", "type": "page"},
    "timestamp": "2026-07-28T10:09:05.824Z",
    "data": {
        "parent": {"id": "cd000000-0000-0000-0000-000000000000", "type": "database",
                   "data_source_id": "4c000000-0000-0000-0000-000000000000"},
        "updated_properties": ["%40pBE", "Y%3Aqb", "%3EJPs", "AoOa", "HK%3C%7D",
                               "KZBE", "bIks", "%7C%40cI", "~TvQ"],
    },
}
REAL_PAGE_ID = "3a000000000000000000000000000000"


class ReceiverRoutingTest(unittest.TestCase):
    """The specific reversal: this event class must not reach `row_probe`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        for name, path in (("PROPS_QUEUE", os.path.join(d, "props-probe-queue.json")),
                           ("DB_EVENTS", os.path.join(d, "webhook-db-events.json"))):
            p = mock.patch.object(wr, name, path)
            p.start()
            self.addCleanup(p.stop)

    def route(self, event):
        wr.route_event(event.get("type", ""), event.get("entity") or {},
                       event.get("data") or {})

    def queue(self):
        return refresh.jload(wr.PROPS_QUEUE, [])

    def test_a_real_properties_updated_event_enqueues_a_props_probe(self):
        self.route(REAL_PROPERTIES_UPDATED)
        self.assertEqual(self.queue(), [{"kind": "props_probe", "row": REAL_PAGE_ID}])

    def test_it_never_enqueues_a_row_probe(self):
        self.route(REAL_PROPERTIES_UPDATED)
        self.assertEqual(os.listdir(self.tmp.name), ["props-probe-queue.json"])
        for entry in self.queue():
            self.assertEqual(entry["kind"], "props_probe")

    def test_ten_events_for_one_page_produce_one_entry(self):
        for _ in range(10):
            self.route(REAL_PROPERTIES_UPDATED)
        self.assertEqual(len(self.queue()), 1)

    def test_distinct_pages_each_get_an_entry_in_arrival_order(self):
        for i in range(3):
            ev = dict(REAL_PROPERTIES_UPDATED)
            ev["entity"] = {"id": f"{i:032x}", "type": "page"}
            self.route(ev)
        self.assertEqual([e["row"] for e in self.queue()], [f"{i:032x}" for i in range(3)])

    def test_a_property_update_on_a_non_row_page_is_ignored(self):
        # 3 of 21,977 observed properties_updated events name a page parent.
        # Those pages have no CSV line and no property table; probing one would
        # spend a request to discover there is nothing to render.
        ev = dict(REAL_PROPERTIES_UPDATED)
        ev["data"] = {"parent": {"id": "e" * 32, "type": "page"}}
        self.route(ev)
        self.assertEqual(self.queue(), [])

    def test_body_events_are_left_to_the_nightly_sweeps(self):
        """content_updated/created/moved/deleted move last_edited_time, which the
        row query and the page search diff every night; a queue for them only
        made the row sweep probe the same rows twice."""
        for etype in ("page.content_updated", "page.created", "page.moved", "page.deleted"):
            self.route({"type": etype, "entity": {"id": "a" * 32}, "data": {}})
        self.assertEqual(os.listdir(self.tmp.name), [])


class DrainTestCase(MirrorTestCase):
    """The drain, over a temp mirror with one DB directory."""

    RID = "a" * 32

    def setUp(self):
        super().setUp()
        self.seed_row(self.RID, "Row One", enrichment=self.ENRICHMENT, status="old")
        self.write_csv([[self.RID, "Row One", "old"]])
        self.api = FakeApi({self.RID: page(self.RID, self.DB_ID, "Row One", Status="new")})
        # this kind must never reach the body
        refresh.probe_row = self._forbidden

    ENRICHMENT = ("\n\n## Body\n\n  a stored body line\n\n"
                  "## Comments\n\n- _Someone (2026-07-01):_ stored comment\n")

    @staticmethod
    def _forbidden(*a, **kw):
        raise AssertionError("props_probe must not walk the body")

    def enqueue(self, *rids):
        refresh.jsave(refresh.props_queue_path(),
                      [{"kind": "props_probe", "row": r} for r in rids])

    def drain(self, api=None, discovered=None):
        refresh.drain_props_probe(api or self.api, None, self.state_dict, self.report,
                                  self.args, discovered if discovered is not None else set())

    def pending(self):
        """Everything still owed: the live queue plus any processing file."""
        return (refresh.jload(refresh.props_queue_path(), [])
                + refresh.jload(refresh.props_processing_path(), []))


class EnrichmentInvarianceTest(DrainTestCase):

    def test_the_body_and_comments_region_is_byte_identical_after_a_drain(self):
        before = refresh.existing_enrichment(
            os.path.join(self.dirpath, f"Row One {self.RID}.md"))
        self.enqueue(self.RID)
        self.drain()
        after = refresh.existing_enrichment(
            os.path.join(self.dirpath, f"Row One {self.RID}.md"))
        self.assertEqual(after, before)
        self.assertEqual(after, self.ENRICHMENT)

    def test_it_is_carried_over_not_re_fetched_even_when_the_remote_body_changed(self):
        # `probe_row` raises for the whole class: the invariant is "this kind
        # does not read the body", not "the body happened not to change".
        self.enqueue(self.RID)
        self.drain()
        self.assertIn("a stored body line", self.row_text(self.RID))

    def test_the_property_table_and_the_csv_line_both_move(self):
        self.enqueue(self.RID)
        self.drain()
        self.assertIn("| Status | new |", self.row_text(self.RID))
        _header, rows = refresh.read_csv(self.csv_path)
        self.assertEqual(rows[self.RID], [self.RID, "Row One", "new"])
        self.assertEqual(self.report["props_probe"]["drained"], 1)

    def test_a_missing_csv_drops_the_entry_rather_than_blanking_the_table(self):
        os.remove(self.csv_path)
        before = self.row_text(self.RID)
        self.enqueue(self.RID)
        self.drain()
        self.assertEqual(self.row_text(self.RID), before)
        self.assertEqual(self.report["props_probe"]["drained"], 0)
        self.assertIn("no CSV header", self.report["props_probe"]["errors"][0]["error"])

    def test_one_request_per_entry(self):
        self.enqueue(self.RID)
        self.drain()
        self.assertEqual(self.api.n, 1)


class SnapshotAndSwapTest(DrainTestCase):

    def test_entries_appended_during_a_drain_are_not_lost(self):
        appended = "b" * 32
        api = FakeApi({self.RID: page(self.RID, self.DB_ID, "Row One", Status="new")})
        real_get = api.get

        def get_then_append(path, params=None, ver=None):
            out = real_get(path, params, ver)
            # the receiver, mid-drain: it appends to the live queue file, which
            # the drain has already renamed out from under itself
            refresh.jsave(refresh.props_queue_path(),
                          [{"kind": "props_probe", "row": appended}])
            api.get = real_get
            return out

        api.get = get_then_append
        self.enqueue(self.RID)
        self.drain(api=api)

        # drained in the same pass (the second take), so nothing is stranded
        self.assertEqual(self.pending(), [])
        self.assertEqual(self.report["props_probe"]["drained"], 1)
        self.assertEqual(self.report["props_probe"]["dropped"], 1,
                         "the appended row is not in this mirror, so it is dropped, not lost")

    def test_an_orphan_processing_file_is_drained_first(self):
        # a drain killed mid-flight leaves entries here; without recovery nothing
        # ever looks at them again and they vanish silently.
        refresh.jsave(refresh.props_processing_path(),
                      [{"kind": "props_probe", "row": self.RID}])
        self.drain()
        self.assertIn("| Status | new |", self.row_text(self.RID))
        self.assertEqual(self.pending(), [])
        self.assertFalse(os.path.exists(refresh.props_processing_path()))

    def test_an_orphan_is_drained_before_the_live_queue(self):
        other = "b" * 32
        api = FakeApi({self.RID: page(self.RID, self.DB_ID, "Row One", Status="new"),
                       other: page(other, self.DB_ID, "Row Two", Status="new")})
        refresh.jsave(refresh.props_processing_path(),
                      [{"kind": "props_probe", "row": self.RID}])
        self.enqueue(other)
        self.drain(api=api)
        self.assertEqual([p.split("/pages/")[1].replace("-", "") for p in api.calls],
                         [self.RID, other])


class DrainInRowsPhaseTest(MirrorTestCase):
    """A rows tick drains the props queue after its named rows."""

    A, B = "a" * 32, "b" * 32

    def setUp(self):
        super().setUp()
        for rid in (self.A, self.B):
            self.seed_row(rid, f"Row {rid[0]}")
        self.write_csv([[rid, f"Row {rid[0]}", "old"] for rid in (self.A, self.B)])

    def test_phase_rows_drains_the_props_queue_after_its_rows(self):
        api = FakeApi({r: page(r, self.DB_ID, f"Row {r[0]}", Status="new")
                       for r in (self.A, self.B)})
        refresh.jsave(refresh.props_queue_path(), [{"kind": "props_probe", "row": self.B}])
        refresh.phase_rows(api, None, self.state_dict, self.report, self.args, [self.A], set())
        self.assertEqual(self.probed, [self.A], "only the named row gets a body probe")
        self.assertIn("| Status | new |", self.row_text(self.B, "Row b"))
        self.assertEqual(self.report["props_probe"]["drained"], 1)



if __name__ == "__main__":
    unittest.main()
