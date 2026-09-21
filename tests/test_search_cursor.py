"""Notion now and then rejects a /search cursor it issued itself mid-sweep (HTTP
400 validation_error, "The start_cursor provided is invalid"). On 2026-09-09 that
took the nightly down after its whole API pull and wedged the mirror for a week
behind the dirty-tree preflight. The content sweep is idempotent, so it restarts
from the top once; anything else still raises."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import refresh  # noqa: E402  (_tools is not a package; discover's top dir is tests/)
from notion_core.api import ApiError  # noqa: E402

BAD_CURSOR = ('{"object":"error","status":400,"code":"validation_error","message":'
              '"The start_cursor provided is invalid: 33000000-0000-0000-0000-000000000000",'
              '"request_id":"0f000000-0000-0000-0000-000000000000"}')


class ScriptedApi:
    """paginate() plays one script per call: the results to yield, or an exception
    raised after the results that precede it (a cursor request going wrong)."""

    def __init__(self, *scripts):
        self.scripts = list(scripts)
        self.calls = []

    def paginate(self, method, path, body=None, params=None, ver=None):
        self.calls.append((method, path, body))
        for item in self.scripts.pop(0):
            if isinstance(item, Exception):
                raise item
            yield item


def page(n):
    return {"object": "page", "id": f"{n:032x}"}


class SearchSweep(unittest.TestCase):
    body = {"filter": {"value": "page", "property": "object"},
            "sort": {"timestamp": "last_edited_time", "direction": "ascending"}}

    def test_a_rejected_cursor_restarts_the_sweep_once(self):
        api = ScriptedApi([page(1), page(2), ApiError(400, BAD_CURSOR)],
                          [page(1), page(2), page(3)])
        report = {"notes": []}
        got = [p["id"] for p in refresh.search_sweep(api, self.body, report)]
        self.assertEqual(got, [page(n)["id"] for n in (1, 2, 1, 2, 3)])
        self.assertEqual(len(api.calls), 2)
        self.assertTrue(any("cursor" in n for n in report["notes"]), report["notes"])

    def test_the_restart_sends_the_same_search(self):
        api = ScriptedApi([ApiError(400, BAD_CURSOR)], [page(1)])
        list(refresh.search_sweep(api, self.body, {"notes": []}))
        self.assertEqual([c[:2] for c in api.calls], [("POST", "/search")] * 2)
        self.assertEqual(api.calls[0][2], self.body)
        self.assertEqual(api.calls[1][2], self.body)

    def test_a_second_rejection_is_raised(self):
        api = ScriptedApi([page(1), ApiError(400, BAD_CURSOR)], [ApiError(400, BAD_CURSOR)])
        with self.assertRaises(ApiError):
            list(refresh.search_sweep(api, self.body, {"notes": []}))
        self.assertEqual(len(api.calls), 2)

    def test_any_other_error_is_raised_at_once(self):
        for err in (ApiError(400, '{"code":"validation_error","message":"body.filter should be defined"}'),
                    ApiError(500, "boom"),
                    ApiError(0, "retries exhausted")):
            api = ScriptedApi([page(1), err], [page(1)])
            with self.assertRaises(ApiError):
                list(refresh.search_sweep(api, self.body, {"notes": []}))
            self.assertEqual(len(api.calls), 1, err)

    def test_a_clean_sweep_runs_once_and_notes_nothing(self):
        api = ScriptedApi([page(1), page(2)])
        report = {"notes": []}
        self.assertEqual(len(list(refresh.search_sweep(api, self.body, report))), 2)
        self.assertEqual(len(api.calls), 1)
        self.assertEqual(report["notes"], [])


if __name__ == "__main__":
    unittest.main()
