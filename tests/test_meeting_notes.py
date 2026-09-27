"""An AI meeting-notes block renders as what it is.

A `meeting_notes` block (`transcription` before 2026-03-11) has three children,
empty paragraphs standing for its Summary, Notes and Transcript tabs, whose own
children hold the content; the block names them in `children`. The walker used
to print `<!-- unhandled block type: transcription -->` and then walk the three
empty paragraphs, so the content was in the file with nothing saying which part
was the AI summary, which the notes, which a verbatim transcript, or what the
meeting was. It now writes a header line (title, time, attendees, and the status
while notes are not ready) and a label over each tab. Transcript lines are not
listed for comments: a transcript runs to hundreds of paragraphs, each would
cost a request on every comment scan, and nobody comments on them.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from notion_core.walker import Walker  # noqa: E402

MB, SUM, NOTES, TR = "aa" * 16, "b1" * 16, "b2" * 16, "b3" * 16


def para(bid, text, children=False):
    return {"id": bid, "type": "paragraph", "has_children": children, "paragraph": {"rich_text": [
        {"type": "text", "text": {"content": text}, "plain_text": text, "annotations": {}}]}}


def meeting(kind="meeting_notes", status="notes_ready", tabs=True):
    data = {"title": [{"type": "text", "text": {"content": "Quinn and Mackenzie"},
                       "plain_text": "Quinn and Mackenzie", "annotations": {}}],
            "status": status,
            "calendar_event": {"start_time": "2026-04-10T15:30:00.000-04:00",
                               "end_time": "2026-04-10T16:00:00.000-04:00",
                               "attendees": ["u1", "u2"]},
            "recording": {"start_time": "2026-04-10T19:30:00.000Z"}}
    if tabs:
        data["children"] = {"summary_block_id": SUM, "notes_block_id": NOTES, "transcript_block_id": TR}
    return {"id": MB, "type": kind, "has_children": True, kind: data}


class Users:
    def name(self, ref):
        return {"u1": "Quinn Dougherty", "u2": "Mackenzie Puig-Hall"}.get((ref or {}).get("id"), "Someone")


class Api:
    def __init__(self, top):
        self.kids = {"page": top, MB: [para("t1", ""), para("t2", ""), para("t3", "")],
                     SUM: [{"id": "s1", "type": "heading_3", "has_children": False, "heading_3": {"rich_text": [
                         {"type": "text", "text": {"content": "Action Items"}, "plain_text": "Action Items",
                          "annotations": {}}]}}, para("s2", "Mackenzie to draft the form")],
                     NOTES: [para("n1", "Mentor application form")],
                     TR: [para("r1", "Hello there."), para("r2", "Good afternoon.")]}
        self.paths = []
        self.n = 0

    def paginate(self, method, path, **kw):
        self.paths.append(path)
        return iter(self.kids.get(path.split("/")[2], []))


def render(top):
    api = Api(top)
    w = Walker(api, Users())
    lines = []
    w.walk("page", lines, 0)
    return lines, w, api


class MeetingNotes(unittest.TestCase):
    def test_the_block_renders_a_header_and_labelled_tabs(self):
        lines, _w, _a = render([meeting()])
        self.assertEqual(lines, [
            "- 🎙️ **AI meeting notes: Quinn and Mackenzie** — 2026-04-10 15:30–16:00 (-04:00)"
            " · Quinn Dougherty, Mackenzie Puig-Hall",
            "",
            "**Summary**",
            "### Action Items",
            "Mackenzie to draft the form",
            "",
            "**Notes**",
            "",
            "Mentor application form",
            "",
            "**Transcript**",
            "",
            "Hello there.",
            "",
            "Good afternoon.",
        ])

    def test_the_tabs_are_read_directly_not_through_the_empty_paragraphs(self):
        _l, _w, api = render([meeting()])
        self.assertEqual(api.paths, [f"/blocks/{x}/children" for x in ("page", SUM, NOTES, TR)])

    def test_the_old_type_name_renders_the_same(self):
        self.assertEqual(render([meeting("transcription")])[0], render([meeting()])[0])

    def test_a_meeting_still_processing_says_so(self):
        lines, _w, _a = render([meeting(status="transcription_in_progress")])
        self.assertTrue(lines[0].endswith("(transcription in progress)"), lines[0])

    def test_transcript_lines_are_not_listed_for_comments(self):
        _l, w, _a = render([meeting()])
        anchors = [bid for bid, _a in w.block_anchors]
        self.assertIn(MB, anchors)
        self.assertIn("s2", anchors)
        self.assertNotIn("r1", anchors)
        self.assertNotIn("r2", anchors)

    def test_without_tab_ids_the_children_are_walked_as_before(self):
        lines, _w, api = render([meeting(tabs=False)])
        self.assertTrue(lines[0].startswith("- 🎙️ **AI meeting notes: Quinn and Mackenzie**"))
        self.assertIn(f"/blocks/{MB}/children", api.paths)


if __name__ == "__main__":
    unittest.main()
