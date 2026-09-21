"""The receiver's Notion client and its permanently-lossy give-up paths.

Fully offline: every HTTP call is stubbed at refresh.urllib.request.urlopen, and
every ntfy is stubbed at webhook_receiver.ntfy.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

# `unittest discover tests` makes tests/ the top-level dir, so the
# modules under test are not importable without this.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import refresh  # noqa: E402
import webhook_receiver as wr  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, headers=None):
    return urllib.error.HTTPError(
        "https://api.notion.com/v1/comments", code, "err",
        headers or {}, io.BytesIO(b'{"message": "boom"}'))


class ApiClientTest(unittest.TestCase):
    """api_get goes through refresh.Api, so it inherits Retry-After handling."""

    def setUp(self):
        wr._api = refresh.Api("test-token", 1000.0, float("inf"))
        self.addCleanup(setattr, wr, "_api", None)
        self.slept = []
        p = mock.patch.object(refresh.time, "sleep", self.slept.append)
        p.start()
        self.addCleanup(p.stop)

    def test_429_waits_the_retry_after_interval(self):
        responses = [http_error(429, {"Retry-After": "7"}), FakeResponse({"ok": True})]

        def fake_urlopen(req, timeout=None):
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with mock.patch.object(refresh.urllib.request, "urlopen", fake_urlopen):
            out = wr.api_get("/comments", {"block_id": "abc"})

        self.assertEqual(out, {"ok": True})
        self.assertEqual(responses, [], "the retry never fired")
        # refresh.Api sleeps Retry-After + 0.5 rather than hammering
        self.assertIn(7.5, self.slept)
        self.assertEqual(wr._api.r429, 1)

    def test_429_without_retry_after_backs_off_exponentially(self):
        responses = [http_error(429), http_error(429), FakeResponse({"ok": 1})]

        def fake_urlopen(req, timeout=None):
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with mock.patch.object(refresh.urllib.request, "urlopen", fake_urlopen):
            wr.api_get("/comments")

        # attempt 0 -> 2**0, attempt 1 -> 2**1, each + the 0.5 ease-in
        self.assertIn(1.5, self.slept)
        self.assertIn(2.5, self.slept)

    def test_5xx_retries_with_backoff_then_succeeds(self):
        responses = [http_error(502), http_error(503), FakeResponse({"results": []})]

        def fake_urlopen(req, timeout=None):
            r = responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        with mock.patch.object(refresh.urllib.request, "urlopen", fake_urlopen):
            out = wr.api_get("/comments")

        self.assertEqual(out, {"results": []})
        self.assertIn(1, self.slept)
        self.assertIn(2, self.slept)

    def test_permanent_4xx_raises_apierror_with_the_code(self):
        with mock.patch.object(refresh.urllib.request, "urlopen",
                               mock.Mock(side_effect=http_error(404))):
            with self.assertRaises(refresh.ApiError) as ctx:
                wr.api_get("/comments")
        self.assertEqual(ctx.exception.code, 404)

    def test_no_flat_sleep_on_a_clean_call(self):
        """The replaced client slept 0.5s after every request regardless."""
        with mock.patch.object(refresh.urllib.request, "urlopen",
                               mock.Mock(return_value=FakeResponse({"ok": 1}))):
            wr.api_get("/comments")
        self.assertNotIn(0.5, self.slept)


class StateTestCase(unittest.TestCase):
    """Redirects the module's state files into a temp dir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        for name, path in (("STATE", d),
                           ("EVENTS", os.path.join(d, "webhook-events.jsonl")),
                           ("CAPTURE", os.path.join(d, "webhook-comments-capture.jsonl")),
                           ("OFFSET", os.path.join(d, "webhook-capture-offset.json")),
                           ("PRIORITY", os.path.join(d, "webhook-priority-pages.json"))):
            p = mock.patch.object(wr, name, path)
            p.start()
            self.addCleanup(p.stop)
        self.sent = []
        p = mock.patch.object(wr, "ntfy", lambda t, m, pr="default": self.sent.append((t, m, pr)))
        p.start()
        self.addCleanup(p.stop)
        wr._ntfy_last.clear()
        wr._ntfy_suppressed.clear()
        self.addCleanup(wr._ntfy_last.clear)
        self.addCleanup(wr._ntfy_suppressed.clear)

    def write_events(self, events):
        with open(wr.EVENTS, "w") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

    def offset_state(self):
        with open(wr.OFFSET) as f:
            return json.load(f)


COMMENT_EVENT = {
    "type": "comment.created",
    "entity": {"id": "c" * 32},
    "data": {"page_id": "p" * 32, "parent": {"type": "block", "id": "b" * 32}},
}


class GiveUpNtfyTest(StateTestCase):

    def test_exhausted_attempts_ntfy_names_page_and_entity(self):
        self.write_events([COMMENT_EVENT])
        with mock.patch.object(wr, "capture_entity",
                               mock.Mock(side_effect=refresh.ApiError(500, "server"))):
            for _ in range(7):
                wr.capture_tick()

        self.assertEqual(len(self.sent), 1, "exactly one alert per abandoned thread")
        title, msg, prio = self.sent[0]
        self.assertIn("abandoned", title)
        self.assertEqual(prio, "high")
        self.assertIn("p" * 32, msg)
        self.assertIn("b" * 32, msg)
        # and the event is not retried forever: the watermark moved past it
        self.assertEqual(self.offset_state()["offset"], os.path.getsize(wr.EVENTS))
        self.assertEqual(self.offset_state()["attempts"], {})

    def test_permanent_4xx_ntfys_and_skips(self):
        self.write_events([COMMENT_EVENT])
        with mock.patch.object(wr, "capture_entity",
                               mock.Mock(side_effect=refresh.ApiError(404, "gone"))):
            wr.capture_tick()

        self.assertEqual(len(self.sent), 1)
        self.assertIn("404", self.sent[0][1])
        self.assertEqual(self.offset_state()["offset"], os.path.getsize(wr.EVENTS))

    def test_transient_failure_below_the_cap_is_silent_and_holds_the_watermark(self):
        self.write_events([COMMENT_EVENT])
        with mock.patch.object(wr, "capture_entity",
                               mock.Mock(side_effect=refresh.ApiError(500, "server"))):
            wr.capture_tick()

        self.assertEqual(self.sent, [], "a retryable failure is not an incident")
        self.assertEqual(self.offset_state()["offset"], 0)
        self.assertEqual(self.offset_state()["attempts"], {"b" * 32: 1})

    def test_non_http_exception_also_reaches_the_give_up_ntfy(self):
        self.write_events([COMMENT_EVENT])
        with mock.patch.object(wr, "capture_entity",
                               mock.Mock(side_effect=OSError("socket died"))):
            for _ in range(7):
                wr.capture_tick()

        self.assertEqual(len(self.sent), 1)
        self.assertIn("socket died", self.sent[0][1])

    def test_a_systemic_outage_sends_one_alert_not_one_per_page(self):
        self.write_events([
            {"type": "comment.created", "entity": {"id": f"c{i:031d}"},
             "data": {"page_id": f"p{i:031d}", "parent": {"type": "block", "id": f"b{i:031d}"}}}
            for i in range(5)])
        with mock.patch.object(wr, "capture_entity",
                               mock.Mock(side_effect=refresh.ApiError(404, "gone"))):
            wr.capture_tick()

        self.assertEqual(len(self.sent), 1, "cooldown collapses the burst")
        self.assertEqual(wr._ntfy_suppressed["capture-give-up"], 4)

    def test_the_next_alert_after_a_cooldown_reports_what_was_suppressed(self):
        wr.ntfy_throttled("bucket", "t", "first")
        for _ in range(3):
            wr.ntfy_throttled("bucket", "t", "swallowed")
        wr._ntfy_last["bucket"] = -wr.NTFY_COOLDOWN_S  # cooldown elapses
        wr.ntfy_throttled("bucket", "t", "later")

        self.assertEqual(len(self.sent), 2)
        self.assertIn("+3 more", self.sent[1][1])

    def test_a_crashing_tick_is_reported_rather_than_killing_the_thread(self):
        with mock.patch.object(wr, "capture_tick", mock.Mock(side_effect=RuntimeError("bug"))), \
             mock.patch.object(wr.time, "sleep", mock.Mock(side_effect=[None, StopIteration])):
            with self.assertRaises(StopIteration):
                wr.capture_loop()

        self.assertEqual(len(self.sent), 1)
        self.assertIn("RuntimeError: bug", self.sent[0][1])


class CaptureRecordTest(StateTestCase):
    """Addendum items 1 and 2: raw rich_text rides alongside the rendered text,
    and (from A5) the rendered text is `rich_md`, matching refresh.py's own
    comment scan — the two producers' strings are compared during the merge."""

    def test_capture_record_keeps_raw_rich_text_items(self):
        mention = {"type": "mention", "plain_text": "Untitled",
                   "href": "https://www.notion.so/" + "d" * 32,
                   "mention": {"type": "page", "page": {"id": "d" * 32}}}
        rts = [{"type": "text", "plain_text": "as mentioned ", "href": None}, mention]
        payload = {"results": [{"id": "c" * 32, "rich_text": rts,
                                "created_by": {"id": "u" * 32},
                                "created_time": "2026-08-06T10:00:00.000Z",
                                "discussion_id": "d" * 32}],
                   "has_more": False}

        with mock.patch.object(wr, "api_get", mock.Mock(return_value=payload)):
            wr.capture_entity("p" * 32, "page", page_hint="p" * 32)

        with open(wr.CAPTURE) as f:
            rec = json.loads(f.readline())
        comment = rec["comments"][0]
        self.assertEqual(comment["text"],
                         "as mentioned [Untitled](https://www.notion.so/" + "d" * 32 + ")",
                         "an Untitled mention degrades to a followable link, not a dead word")
        self.assertEqual(comment["text"], refresh.rich_md(rts),
                         "the receiver renders a comment exactly as the mirror's scan does")
        self.assertEqual(comment["rich_text"], rts)
        self.assertEqual(comment["rich_text"][1]["href"],
                         "https://www.notion.so/" + "d" * 32,
                         "the mention target survives capture")

    def test_the_field_is_present_but_empty_when_notion_sends_none(self):
        payload = {"results": [{"id": "c" * 32, "rich_text": None,
                                "created_by": {}, "created_time": "", "discussion_id": ""}],
                   "has_more": False}
        with mock.patch.object(wr, "api_get", mock.Mock(return_value=payload)):
            wr.capture_entity("p" * 32, "page", page_hint="p" * 32)

        with open(wr.CAPTURE) as f:
            rec = json.loads(f.readline())
        self.assertEqual(rec["comments"][0]["rich_text"], [])


if __name__ == "__main__":
    unittest.main()


class NoTokenStartup(unittest.TestCase):
    """The token is read once, before serving. Read lazily on the first comment event
    instead, a missing `.claude.json` lands in the capture loop's transient branch, is
    retried to the cap, and the thread is abandoned — while the unit reports healthy."""

    def test_main_refuses_before_binding_a_socket(self):
        logged = []
        with tempfile.TemporaryDirectory() as state, tempfile.TemporaryDirectory() as cfg, \
             mock.patch.object(wr, "STATE", state), \
             mock.patch.object(wr, "log", logged.append), \
             mock.patch.object(wr, "ThreadingHTTPServer") as server, \
             mock.patch.object(wr.threading, "Thread") as thread, \
             mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": cfg}):
            os.environ.pop("NOTION_TOKEN", None)
            rc = wr.main()
        self.assertEqual(rc, 1)
        self.assertTrue(any(line.startswith("refusing to start: no Notion token") for line in logged),
                        logged)
        self.assertTrue(any("NOTION_TOKEN or CLAUDE_CONFIG_DIR" in line for line in logged), logged)
        server.assert_not_called()
        thread.assert_not_called()


class UnmountedVolumeStartup(unittest.TestCase):
    """An unmounted mirror reaches the journal as a refusal, not a traceback."""

    def test_import_refuses_naming_the_fix(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with tempfile.TemporaryDirectory() as mount:  # the empty mount point
            env = {k: v for k, v in os.environ.items()
                   if k not in ("NOTION_MIRROR", "NOTION_MIRROR_TOOLS")}
            env["NOTION_MIRROR"] = mount
            done = subprocess.run(
                [sys.executable, "-c", "import webhook_receiver"],
                capture_output=True, text=True, env=env, cwd=here)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("refusing to start:", done.stderr)
        self.assertIn("fix:", done.stderr)
        self.assertNotIn("Traceback", done.stderr)
