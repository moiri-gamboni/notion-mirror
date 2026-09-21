"""Size-capped rotation of webhook-events.jsonl with an offset handover.

webhook-capture-offset.json, despite the name, is capture_tick's byte watermark
into the EVENTS file — its value equals that file's size. Rotating without a
handover leaves the watermark past the end of a fresh file, where the capture
loop reads nothing and reports success every 45 seconds, forever.

Fully offline: no HTTP, no Notion; capture_entity is stubbed into a recorder.
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

# `unittest discover tests` makes tests/ the top-level dir, so the
# modules under test are not importable without this.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webhook_receiver as wr  # noqa: E402


def comment_event(i):
    return {"type": "comment.created",
            "entity": {"id": f"c{i:031d}"},
            "data": {"page_id": f"p{i:031d}",
                     "parent": {"type": "block", "id": f"b{i:031d}"}}}


class RotationTestCase(unittest.TestCase):

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
        p = mock.patch.object(wr, "ntfy", mock.Mock())
        p.start()
        self.addCleanup(p.stop)
        # a byte cap a handful of synthetic events clears, so no 32 MB fixtures
        p = mock.patch.object(wr, "EVENTS_MAX_BYTES", 400)
        p.start()
        self.addCleanup(p.stop)

    def append_events(self, indexes):
        with open(wr.EVENTS, "a") as f:
            for i in indexes:
                f.write(json.dumps(comment_event(i)) + "\n")

    def drain(self, captured):
        """Run one tick with capture_entity recording the block ids it saw."""
        def record(target, ttype, page_hint=""):
            captured.append(target)
        with mock.patch.object(wr, "capture_entity", record):
            wr.capture_tick()

    def offset_state(self):
        with open(wr.OFFSET) as f:
            return json.load(f)

    def segments(self):
        return sorted(n for n in os.listdir(self.tmp.name)
                      if n.startswith("webhook-events.jsonl."))


class RotationTriggerTest(RotationTestCase):

    def test_below_the_cap_nothing_rotates(self):
        self.append_events([0])
        self.drain([])
        self.assertTrue(os.path.exists(wr.EVENTS))
        self.assertEqual(self.segments(), [])

    def test_a_drained_over_cap_log_rotates_and_hands_the_offset_over(self):
        self.append_events(range(6))
        size = os.path.getsize(wr.EVENTS)
        self.assertGreater(size, wr.EVENTS_MAX_BYTES)

        self.drain([])

        self.assertFalse(os.path.exists(wr.EVENTS), "the live log was moved aside")
        self.assertEqual(self.segments(), ["webhook-events.jsonl.1"])
        self.assertEqual(os.path.getsize(wr.EVENTS + ".1"), size)
        self.assertEqual(self.offset_state()["offset"], 0,
                         "a watermark left at the old size would point past EOF")

    def test_rotation_is_deferred_while_capture_is_behind(self):
        self.append_events(range(6))
        with mock.patch.object(wr, "capture_entity",
                               mock.Mock(side_effect=OSError("network down"))):
            wr.capture_tick()

        self.assertTrue(os.path.exists(wr.EVENTS), "un-captured events must not move")
        self.assertEqual(self.segments(), [])
        self.assertEqual(self.offset_state()["offset"], 0)

    def test_rotation_is_deferred_while_a_retry_is_pending(self):
        """Watermark at EOF but an attempt outstanding is not 'drained'."""
        self.append_events(range(6))
        self.drain([])  # rotates; start clean
        self.append_events(range(100, 106))
        state = self.offset_state()
        state["attempts"] = {"b" * 32: 3}
        with open(wr.OFFSET, "w") as f:
            json.dump(state, f)
        # pretend the watermark caught up without clearing the attempt
        state["offset"] = os.path.getsize(wr.EVENTS)
        with open(wr.OFFSET, "w") as f:
            json.dump(state, f)

        self.assertFalse(wr.maybe_rotate_events())
        self.assertTrue(os.path.exists(wr.EVENTS))


class NoSkipNoReplayTest(RotationTestCase):

    @mock.patch.object(wr, "EVENTS_MAX_BYTES", 1000)
    def test_capture_resumes_across_a_rotation_losing_and_repeating_nothing(self):
        first = list(range(6))
        self.append_events(first)
        captured = []
        self.drain(captured)
        self.assertEqual(captured, [f"b{i:031d}" for i in first])
        self.assertFalse(os.path.exists(wr.EVENTS))

        # traffic resumes into a fresh file
        second = list(range(100, 103))
        self.append_events(second)
        after = []
        self.drain(after)

        self.assertEqual(after, [f"b{i:031d}" for i in second],
                         "zero replayed from the rotated segment, zero skipped in the new one")
        self.assertEqual(captured + after,
                         [f"b{i:031d}" for i in first + second])
        self.assertEqual(self.offset_state()["offset"], os.path.getsize(wr.EVENTS))

    @mock.patch.object(wr, "EVENTS_MAX_BYTES", 10_000)  # isolate the clamp from rotation
    def test_a_watermark_stranded_past_eof_is_alarmed_and_clamped(self):
        """The failure the handover exists to prevent. Rotation can no longer
        cause it, so reaching it means external corruption — which must be heard
        rather than silently swallowing every future event."""
        self.append_events(range(6))
        stale = os.path.getsize(wr.EVENTS)
        os.replace(wr.EVENTS, wr.EVENTS + ".1")  # rotate WITHOUT the handover
        with open(wr.OFFSET, "w") as f:
            json.dump({"offset": stale, "attempts": {}}, f)
        self.append_events(range(100, 103))
        size = os.path.getsize(wr.EVENTS)
        self.assertLess(size, stale)

        lost = []
        self.drain(lost)

        self.assertEqual(lost, [], "those events are genuinely unrecoverable here")
        self.assertEqual(wr.ntfy.call_count, 1, "but the stop is not silent")
        self.assertIn("past EOF", wr.ntfy.call_args[0][0])
        self.assertEqual(self.offset_state()["offset"], size,
                         "clamped to EOF so new traffic keeps flowing")

        # and traffic after the clamp is captured normally
        self.append_events(range(200, 202))
        resumed = []
        self.drain(resumed)
        self.assertEqual(resumed, [f"b{i:031d}" for i in (200, 201)])

    @mock.patch.object(wr, "EVENTS_MAX_BYTES", 10_000)
    def test_a_correct_handover_loses_none_of_the_same_traffic(self):
        """The control for the test above: same inputs, offset handed over."""
        self.append_events(range(6))
        os.replace(wr.EVENTS, wr.EVENTS + ".1")
        with open(wr.OFFSET, "w") as f:
            json.dump({"offset": 0, "attempts": {}}, f)
        self.append_events(range(100, 103))

        got = []
        self.drain(got)
        self.assertEqual(got, [f"b{i:031d}" for i in (100, 101, 102)])
        self.assertEqual(wr.ntfy.call_count, 0)


class RetentionTest(RotationTestCase):

    def test_segments_age_out_at_the_configured_depth(self):
        for round_ in range(wr.EVENTS_SEGMENTS + 2):
            self.append_events(range(round_ * 10, round_ * 10 + 6))
            self.drain([])

        self.assertEqual(self.segments(),
                         [f"webhook-events.jsonl.{i}"
                          for i in range(1, wr.EVENTS_SEGMENTS + 1)])
        # .1 is the newest segment, .N the oldest still kept
        with open(wr.EVENTS + ".1") as f:
            newest = json.loads(f.readline())
        self.assertEqual(newest["entity"]["id"], f"c{40:031d}")


class CrashOrderTest(RotationTestCase):

    def test_the_offset_is_written_before_the_rename(self):
        """Crashing between the two must replay a drained segment, never strand
        the watermark past the end of a fresh file."""
        self.append_events(range(6))
        self.drain([])  # nothing to assert yet; this rotation succeeds
        self.append_events(range(100, 106))
        captured = []
        with mock.patch.object(wr, "capture_entity", lambda t, ty, page_hint="": captured.append(t)):
            wr.capture_tick()
        self.assertEqual(len(captured), 6)

        # now fail the rename itself
        self.append_events(range(200, 206))
        real_replace = os.replace

        def flaky(src, dst):
            if src == wr.EVENTS:
                raise OSError("crash mid-rotation")
            return real_replace(src, dst)

        with mock.patch.object(wr, "capture_entity", lambda t, ty, page_hint="": None):
            with mock.patch.object(os, "replace", flaky):
                with self.assertRaises(OSError):
                    wr.capture_tick()

        self.assertEqual(self.offset_state()["offset"], 0,
                         "the watermark was already reset, so the segment replays")
        self.assertTrue(os.path.exists(wr.EVENTS))
        replayed = []
        self.drain(replayed)
        self.assertEqual(len(replayed), 6, "replay, not silence")


class ConcurrencyTest(RotationTestCase):

    def test_the_event_append_and_the_rotation_share_one_lock(self):
        """An append landing between the watermark write and the rename would be
        rotated away un-captured; both paths must serialise on wr._lock."""
        import inspect
        src = inspect.getsource(wr.Handler.do_POST)
        self.assertIn("with _lock:", src)
        self.assertIn("jappend(EVENTS", src)
        self.assertIn("with _lock:", inspect.getsource(wr.maybe_rotate_events))


if __name__ == "__main__":
    unittest.main()
