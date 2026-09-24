"""A truncated response body is a transport failure, not a bug in the caller.

On 2026-09-17 the nightly died after 8,042 requests when a ~1 MB `/search` page
came back cut mid-string: `json.loads` raised `JSONDecodeError`, which escaped
`Api.call`'s retry loop, and the half-written tree wedged every run for three
days. A body that does not arrive whole is exactly as retryable as the
connection errors beside it, and the sweeps that hit it are reads.
"""
import http.client
import io
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import refresh  # noqa: E402  (tests/ is discover's top dir; see test_receiver_api.py)


class Body:
    """A response whose read() returns bytes, or raises what read() can raise."""

    def __init__(self, payload):
        self._payload = payload

    def read(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def truncated():
    """What Notion actually returned: valid JSON cut off mid-string."""
    whole = json.dumps({"results": [{"id": "a" * 64}], "has_more": False}).encode()
    return Body(whole[: len(whole) // 2])


def whole(payload):
    return Body(json.dumps(payload).encode())


class TruncatedBody(unittest.TestCase):
    def setUp(self):
        self.api = refresh.Api("test-token", 1000.0, float("inf"))
        self.slept = []
        p = mock.patch.object(refresh.time, "sleep", self.slept.append)
        p.start()
        self.addCleanup(p.stop)

    def serve(self, *responses):
        """Patch urlopen to hand out `responses` in order; returns the call log."""
        served = list(responses)
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            r = served.pop(0) if served else whole({"ok": True})
            if isinstance(r, Exception):
                raise r
            return r

        p = mock.patch.object(refresh.urllib.request, "urlopen", fake_urlopen)
        p.start()
        self.addCleanup(p.stop)
        return calls

    def test_a_truncated_body_is_retried_not_raised(self):
        calls = self.serve(truncated(), whole({"results": [], "has_more": False}))
        out = self.api.call("POST", "/search", body={})
        self.assertEqual(out, {"results": [], "has_more": False})
        self.assertEqual(len(calls), 2, "the retry never fired")

    def test_an_incomplete_read_is_retried(self):
        calls = self.serve(Body(http.client.IncompleteRead(b"partial")), whole({"ok": 1}))
        self.assertEqual(self.api.call("GET", "/pages/x"), {"ok": 1})
        self.assertEqual(len(calls), 2)

    def test_a_body_cut_mid_utf8_character_is_retried(self):
        cut = '{"t": "é'.encode()[:-1]  # ends inside the 2-byte sequence
        with self.assertRaises(UnicodeDecodeError):
            cut.decode("utf-8")  # the case this test exists for, pinned
        calls = self.serve(Body(cut), whole({"ok": 1}))
        self.assertEqual(self.api.call("GET", "/pages/x"), {"ok": 1})
        self.assertEqual(len(calls), 2)

    def test_a_retry_backs_off_rather_than_hammering(self):
        self.serve(truncated(), truncated(), whole({"ok": 1}))
        self.api.call("GET", "/pages/x")
        self.assertIn(1, self.slept)
        self.assertIn(2, self.slept)

    def test_a_body_that_never_arrives_whole_ends_as_an_apierror(self):
        """Not a JSONDecodeError: callers handle ApiError, and a bare decode
        error reaches the cron log as a crash with no request context."""
        self.serve(*[truncated() for _ in range(9)])
        with self.assertRaises(refresh.ApiError) as ctx:
            self.api.call("POST", "/search", body={})
        self.assertEqual(ctx.exception.code, 0)

    def test_a_whole_body_still_costs_one_request_and_no_sleep(self):
        calls = self.serve(whole({"ok": 1}))
        self.assertEqual(self.api.call("GET", "/pages/x"), {"ok": 1})
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.api.n, 1)
        self.assertEqual(self.slept, [])


class EndpointCount(TruncatedBody):
    """Every counted request lands in exactly one endpoint class, so a run report
    can say how many of its requests went to comments."""

    def test_classes(self):
        from notion_core.api import endpoint_class
        cases = {("POST", "/databases/x/query"): "query",
                 ("POST", "/data_sources/x/query"): "query",
                 ("POST", "/search"): "search",
                 ("GET", "/comments"): "comments",
                 ("GET", "/blocks/x/children"): "blocks/children",
                 ("GET", "/blocks/x"): "blocks",
                 ("GET", "/pages/x"): "pages",
                 ("GET", "/pages/x/properties/y"): "pages/properties",
                 ("GET", "/databases/x"): "databases",
                 ("GET", "/users/x"): "users",
                 ("PATCH", "/something-new"): "other"}
        for (method, path), want in cases.items():
            self.assertEqual(endpoint_class(method, path), want, (method, path))

    def test_counts_match_n_including_a_4xx_and_excluding_retries(self):
        import urllib.error
        err = urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO(b"{}"))
        self.serve(whole({"ok": 1}), truncated(), whole({"ok": 1}), err)
        self.api.get("/comments", params={"block_id": "b"})
        self.api.get("/blocks/b/children")
        with self.assertRaises(refresh.ApiError):
            self.api.get("/pages/p")
        self.assertEqual(dict(self.api.by_endpoint),
                         {"comments": 1, "blocks/children": 1, "pages": 1})
        self.assertEqual(sum(self.api.by_endpoint.values()), self.api.n)



class OptionalBudget(unittest.TestCase):
    """The engine runs with no request budget; the standalone tools still set one."""

    def test_no_budget_never_stops(self):
        api = refresh.Api("t", 1000.0)
        api.n = 10 ** 9
        api.check_budget()

    def test_a_set_budget_still_stops(self):
        api = refresh.Api("t", 1000.0, 3)
        api.n = 3
        with self.assertRaises(refresh.Budget):
            api.check_budget()


if __name__ == "__main__":
    unittest.main()
