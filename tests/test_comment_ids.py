"""Row comments are keyed by their comment id, not by their rendered text
and comment text renders through `rich_md` like every other
piece of Notion rich text (the mention-fidelity addendum).

Offline: `FakeApi` serves the two endpoints a row probe touches and asserts on
anything else, so nothing here can reach Notion.
"""
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coverage_backfill  # noqa: E402  (_tools is not a package; discover's top dir is tests/)
import migrate_comment_ids as mig  # noqa: E402
import refresh  # noqa: E402

ROW = "aa" * 16
BLOCK = "bb" * 16


def today():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def comment(cid, text, did=None, who="uu" * 16, when="2026-07-01T09:00:00.000Z"):
    """A /comments result item as Notion returns it."""
    return {"id": refresh.dashed(cid), "discussion_id": refresh.dashed(did or cid),
            "rich_text": [{"type": "text", "plain_text": text, "href": None}],
            "created_by": {"id": who, "name": "Someone"},
            "created_time": when}


class FakeApi:
    """/blocks/<id>/children and /comments, from a dict. Anything else is a bug
    in the test."""

    CHILDREN = re.compile(r"^/blocks/([0-9a-f-]{32,36})/children$")

    def __init__(self, comments=None, children=None):
        self.comments = {refresh.undash(k): v for k, v in (comments or {}).items()}
        self.children = {refresh.undash(k): v for k, v in (children or {}).items()}
        self.n = 0
        self.r429 = 0

    def paginate(self, method, path, body=None, params=None, ver=None):
        self.n += 1
        if path == "/comments":
            yield from self.comments.get(refresh.undash((params or {})["block_id"]), [])
            return
        m = self.CHILDREN.match(path)
        if not m or method != "GET":
            raise AssertionError(f"unexpected call: {method} {path}")
        yield from self.children.get(refresh.undash(m.group(1)), [])


class FakeUsers:
    def name(self, ref):
        if not ref:
            return ""
        return (ref.get("name") if isinstance(ref, dict) else None) or "Someone"


class ProbeTestCase(unittest.TestCase):
    """A row probe with no captures in play."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="a5-comment-ids-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.report = refresh.new_report("test")
        # union_captured reads the receiver's log through a module-level cache
        self.addCleanup(setattr, refresh, "_WEBHOOK_CAPTURES", None)
        refresh._WEBHOOK_CAPTURES = {}

    def probe(self, comments, old_comments_body="", children=None, cap=25):
        api = FakeApi(comments=comments, children=children)
        enrich, capped = refresh.probe_row(api, FakeUsers(), refresh.dashed(ROW), self.dir,
                                           self.report, old_comments_body=old_comments_body,
                                           block_comment_cap=cap)
        return refresh.split_bullets(refresh.extract_comments_body(enrich), prefix="- _"), capped


class TestIdentityIsTheId(ProbeTestCase):

    def test_two_comments_with_identical_text_stay_two_bullets(self):
        # The text key collapsed these: 108 (page, identical text) groups in the
        # capture log hold two or more distinct ids, several across different
        # threads ('TBD' on one page in two discussions). Collapsing is data loss.
        bullets, _ = self.probe({ROW: [comment("c1" * 16, "TBD", did="d1" * 16),
                                       comment("c2" * 16, "TBD", did="d2" * 16)]})
        self.assertEqual(len(bullets), 2)
        self.assertEqual([refresh.bullet_cid(b) for b in bullets], ["c1" * 16, "c2" * 16])
        self.assertIn("d=" + "d1" * 16, bullets[0])
        self.assertIn("d=" + "d2" * 16, bullets[1])

    def test_seven_repeats_of_one_automation_line_all_survive(self):
        # AS_Integration posts '🔗 CLICK TRACKED: <email>' once per click. Seven
        # identical lines on one row are seven events, and the count is the
        # information — this is why the dedup is by id and never by text.
        line = "🔗 CLICK TRACKED: someone@example.com"
        cs = [comment(f"{i:032x}", line) for i in range(7)]
        bullets, _ = self.probe({ROW: cs})
        self.assertEqual(len(bullets), 7)
        self.assertEqual(len({refresh.bullet_cid(b) for b in bullets}), 7)

    def test_a_re_probe_of_that_row_is_a_no_op(self):
        line = "🔗 CLICK TRACKED: someone@example.com"
        cs = [comment(f"{i:032x}", line) for i in range(7)]
        first, _ = self.probe({ROW: cs})
        again, _ = self.probe({ROW: cs}, old_comments_body="\n".join(first))
        self.assertEqual(again, first)
        self.assertFalse(any(refresh.RESOLVED_MARK.search(b) for b in again))

    def test_no_two_surviving_bullets_share_a_comment_id(self):
        # One comment reachable both page-level and through its anchor block.
        c = comment("c1" * 16, "hello")
        para = {"id": refresh.dashed(BLOCK), "type": "paragraph", "has_children": False,
                "paragraph": {"rich_text": [{"plain_text": "anchor", "href": None}]}}
        bullets, _ = self.probe({ROW: [c], BLOCK: [c]},
                                children={ROW: [para]})
        self.assertEqual(len(bullets), 1)
        cids = [refresh.bullet_cid(b) for b in bullets]
        self.assertEqual(len(cids), len(set(cids)))

    def test_an_edited_comment_updates_in_place(self):
        # Same id, new text: one bullet, no phantom resolved twin.
        before, _ = self.probe({ROW: [comment("c1" * 16, "first draft")]})
        after, _ = self.probe({ROW: [comment("c1" * 16, "edited text")]},
                              old_comments_body="\n".join(before))
        self.assertEqual(len(after), 1)
        self.assertIn("edited text", after[0])
        self.assertNotIn("first draft", "\n".join(after))
        self.assertFalse(refresh.RESOLVED_MARK.search(after[0]))

    def test_a_comment_that_stops_coming_back_is_annotated_not_dropped(self):
        before, _ = self.probe({ROW: [comment("c1" * 16, "resolved soon")]})
        after, _ = self.probe({ROW: []}, old_comments_body="\n".join(before))
        self.assertEqual(len(after), 1)
        self.assertIn("resolved soon", after[0])
        self.assertIn(f"_[resolved/deleted ≤{today()}]_", after[0])
        self.assertEqual(refresh.bullet_cid(after[0]), "c1" * 16)

    def test_the_resolved_annotation_sits_before_the_id_trailer(self):
        before, _ = self.probe({ROW: [comment("c1" * 16, "text")]})
        after, _ = self.probe({ROW: []}, old_comments_body="\n".join(before))
        self.assertTrue(after[0].endswith(refresh.cid_trailer("c1" * 16, "c1" * 16)),
                        f"trailer must stay last on the line: {after[0]!r}")

    def test_a_capped_block_scan_carries_the_stored_record_over_by_id(self):
        # The scan is skipped, so block-anchored bullets cannot be compared:
        # they must survive verbatim, and the page-level one must not double.
        stored = [refresh.stamp_cid("- _Someone (2026-06-01):_ block comment",
                                    "c9" * 16, "c9" * 16),
                  refresh.stamp_cid("- _Someone (2026-07-01):_ page comment",
                                    "c1" * 16, "c1" * 16)]
        blocks = [{"id": f"{i:032x}", "type": "paragraph", "has_children": False,
                   "paragraph": {"rich_text": [{"plain_text": f"b{i}", "href": None}]}}
                  for i in range(5)]
        bullets, capped = self.probe({ROW: [comment("c1" * 16, "page comment")]},
                                     old_comments_body="\n".join(stored),
                                     children={ROW: blocks}, cap=2)
        self.assertTrue(capped)
        self.assertEqual(len(bullets), 2)
        self.assertIn("block comment", "\n".join(bullets))
        self.assertEqual(sorted(refresh.bullet_cid(b) for b in bullets),
                         sorted(["c1" * 16, "c9" * 16]))


class TestLegacyShim(ProbeTestCase):
    """Bullets whose id no source recovered. They keep the old text key, marked
    as such, and are never dropped."""

    LEGACY = refresh.stamp_cid("- _Someone (2026-01-01):_ an old resolved thought")

    def test_the_shim_marker_is_the_documented_token(self):
        self.assertTrue(self.LEGACY.endswith("<!-- notion:cid legacy -->"))
        self.assertIsNone(refresh.bullet_cid(self.LEGACY))

    def test_a_legacy_bullet_the_api_never_returns_is_kept_and_annotated(self):
        bullets, _ = self.probe({ROW: []}, old_comments_body=self.LEGACY)
        self.assertEqual(len(bullets), 1)
        self.assertIn("an old resolved thought", bullets[0])
        self.assertIn("resolved/deleted", bullets[0])

    def test_an_already_annotated_legacy_bullet_is_not_re_annotated(self):
        once, _ = self.probe({ROW: []}, old_comments_body=self.LEGACY)
        twice, _ = self.probe({ROW: []}, old_comments_body="\n".join(once))
        self.assertEqual(twice, once)

    def test_a_legacy_bullet_matching_a_fresh_one_by_text_does_not_duplicate(self):
        # The state between deploying the code and applying the migration: the
        # stored bullet carries no id, the fresh one does. Text is the only key
        # they share, so the shim path has to compare against every fresh bullet.
        stored = "- _Someone (2026-07-01):_ still open"
        bullets, _ = self.probe({ROW: [comment("c1" * 16, "still open")]},
                                old_comments_body=stored)
        self.assertEqual(len(bullets), 1)
        self.assertEqual(refresh.bullet_cid(bullets[0]), "c1" * 16)
        self.assertFalse(refresh.RESOLVED_MARK.search(bullets[0]))

    def test_dedup_only_ever_looks_at_ids(self):
        # Directly, because the scan always supplies ids: if a payload ever
        # arrives without one, these bullets must still be treated as distinct
        # comments rather than folded together on their text.
        same = refresh.stamp_cid("- _Someone (2026-01-01):_ same words")
        self.assertEqual(refresh.dedup_by_cid([same, same, same]), [same, same, same])
        withid = refresh.stamp_cid("- _Someone (2026-01-01):_ same words", "c1" * 16)
        self.assertEqual(refresh.dedup_by_cid([withid, withid]), [withid])

    def test_identical_id_less_repeats_are_never_collapsed(self):
        line = "- _Someone (2026-01-01):_ 🔗 CLICK TRACKED: x@example.com"
        stored = "\n".join([refresh.stamp_cid(line)] * 7)
        bullets, _ = self.probe({ROW: []}, old_comments_body=stored)
        self.assertEqual(len(bullets), 7)


class TestCaptureUnion(ProbeTestCase):
    """union_captured is id-aware too, and renders raw items when it has them."""

    def capture(self, **kw):
        base = {"id": refresh.dashed("c1" * 16), "text": "captured text",
                "author_id": "uu" * 16, "created_time": "2026-07-01T09:00:00.000Z",
                "discussion_id": refresh.dashed("d1" * 16)}
        base.update(kw)
        return base

    def test_a_captured_comment_the_scan_still_returns_is_not_appended_twice(self):
        refresh._WEBHOOK_CAPTURES = {ROW: [("(page-level)", self.capture(text="live one"))]}
        bullets, _ = self.probe({ROW: [comment("c1" * 16, "live one", did="d1" * 16)]})
        self.assertEqual(len(bullets), 1)
        self.assertFalse(refresh.RESOLVED_MARK.search(bullets[0]))

    def test_a_captured_comment_the_scan_lost_is_appended_annotated_with_its_id(self):
        refresh._WEBHOOK_CAPTURES = {ROW: [("(page-level)", self.capture(text="since resolved"))]}
        bullets, _ = self.probe({ROW: []})
        self.assertEqual(len(bullets), 1)
        self.assertIn("since resolved", bullets[0])
        self.assertIn("resolved/deleted", bullets[0])
        self.assertEqual(refresh.bullet_cid(bullets[0]), "c1" * 16)

    def test_the_same_capture_twice_still_produces_one_bullet(self):
        cap = self.capture(text="once")
        refresh._WEBHOOK_CAPTURES = {ROW: [("(page-level)", cap), ("(page-level)", cap)]}
        bullets, _ = self.probe({ROW: []})
        self.assertEqual(len(bullets), 1)

    def test_a_capture_with_raw_items_renders_its_mention_as_a_link(self):
        href = "https://www.notion.so/" + "d" * 32
        refresh._WEBHOOK_CAPTURES = {ROW: [("(page-level)", self.capture(
            text="as mentioned Untitled",
            rich_text=[{"type": "text", "plain_text": "as mentioned ", "href": None},
                       {"type": "mention", "plain_text": "Untitled", "href": href}]))]}
        bullets, _ = self.probe({ROW: []})
        self.assertIn(f"[Untitled]({href})", bullets[0])

    def test_a_pre_addendum_capture_shows_the_text_it_stored(self):
        refresh._WEBHOOK_CAPTURES = {ROW: [("(page-level)", self.capture(text="flat text"))]}
        bullets, _ = self.probe({ROW: []})
        self.assertIn("flat text", bullets[0])


class TestRendering(ProbeTestCase):

    def test_a_comment_mention_keeps_its_target(self):
        # The motivating case: "as mentioned here" rendered as "as mentioned
        # Untitled", with the referenced task recoverable only by hand.
        href = "https://www.notion.so/" + "d" * 32
        c = comment("c1" * 16, "")
        c["rich_text"] = [{"type": "text", "plain_text": "as mentioned ", "href": None},
                          {"type": "mention", "plain_text": "Untitled", "href": href}]
        bullets, _ = self.probe({ROW: [c]})
        self.assertIn(f"as mentioned [Untitled]({href})", bullets[0])

    def test_both_producers_render_one_comment_identically(self):
        # They are compared as whole strings during the merge, so a disagreement
        # would manufacture a duplicate on every probe.
        import webhook_receiver as wr
        rts = [{"type": "text", "plain_text": "bold ", "href": None},
               {"type": "text", "plain_text": "bit", "href": None,
                "annotations": {"bold": True}}]
        c = comment("c1" * 16, "")
        c["rich_text"] = rts
        bullets, _ = self.probe({ROW: [c]})
        self.assertIn(refresh.rich_md(rts), bullets[0])
        self.assertEqual(wr.refresh.rich_md(rts), refresh.rich_md(rts))


class TestExistingParsersStillWork(unittest.TestCase):
    """Id embedding must not disturb anything that already reads these bullets."""

    def bullet(self, text="a comment", cid="c1" * 16, cont=""):
        return refresh.stamp_cid(f"- _Someone (2026-07-01):_ {text}" + cont, cid, "d1" * 16)

    def test_split_bullets_keeps_continuation_lines(self):
        b = self.bullet(cont="\n  second line\n  third line")
        got = refresh.split_bullets("\n".join([b, self.bullet(cid="c2" * 16)]), prefix="- _")
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0], b)
        self.assertIn("\n  second line", got[0])
        self.assertTrue(got[0].splitlines()[0].endswith("-->"),
                        "the trailer belongs on the first line, not after the continuations")

    def test_resolved_mark_stripping_is_unaffected(self):
        marked = refresh.annotate_resolved(self.bullet(), "2026-08-06")
        self.assertEqual(refresh.RESOLVED_MARK.sub("", marked), self.bullet())

    def test_union_captured_compares_whole_strings_across_the_trailer(self):
        self.assertEqual(refresh.bullet_text_key(self.bullet()),
                         refresh.bullet_text_key(self.bullet(cid="c2" * 16)),
                         "the text key ignores which comment it is")
        self.assertNotEqual(refresh.bullet_text_key(self.bullet()),
                            refresh.bullet_text_key(self.bullet(text="other")))

    def test_the_contamination_key_ignores_the_trailer(self):
        # The A4 assert and its baseline are keyed by comment text. If the id
        # trailer leaked into the key every bullet would be unique, the index
        # would flatten to spread 1 and the assert would silently stop working.
        plain_bullet = "- _Someone (2026-07-01):_ a comment"
        self.assertEqual(refresh.comment_text_key(self.bullet()),
                         refresh.comment_text_key(plain_bullet))
        self.assertEqual(refresh.comment_text_key(self.bullet(cid="c2" * 16)),
                         refresh.comment_text_key(plain_bullet))
        self.assertEqual(refresh.comment_text_key(self.bullet()), "a comment")

    def test_a_stamped_bullet_survives_a_row_render_round_trip(self):
        enrich = "\n\n## Comments\n\n" + self.bullet() + "\n"
        page = {"id": refresh.dashed("ee" * 16),
                "properties": {"Name": {"type": "title", "title": [{"plain_text": "R"}]}},
                "last_edited_time": "2026-08-01T00:00:00.000Z"}
        txt = refresh.render_row_md(page, "ff" * 16, "DB", ["Name"], None, enrich)
        body = refresh.extract_comments_body(txt.split(refresh.MARKER, 1)[1])
        self.assertEqual(refresh.split_bullets(body, prefix="- _"), [self.bullet()])

    def test_the_trailer_grammar_is_the_one_tasksync_parses(self):
        self.assertEqual(refresh.cid_trailer("c" * 32, "d" * 32),
                         " <!-- notion:cid " + "c" * 32 + " d=" + "d" * 32 + " -->")
        self.assertEqual(refresh.cid_trailer("c" * 32), " <!-- notion:cid " + "c" * 32 + " -->")
        self.assertEqual(refresh.cid_trailer(), " <!-- notion:cid legacy -->")
        self.assertEqual(refresh.bullet_cid("x " + refresh.cid_trailer("c" * 32, "d" * 32)),
                         "c" * 32)
        self.assertIsNone(refresh.bullet_cid("x " + refresh.cid_trailer()))


class TestContentTierBullets(unittest.TestCase):
    """`_comments.md` bullets are stamped by the same code path — the nightly
    writes them there as it rescans, even though the migration leaves that file
    alone by default."""

    def test_page_and_block_bullets_both_carry_their_id(self):
        pc = [refresh.Comment("page text", "Someone", "2026-07-01T00:00:00Z",
                              "c1" * 16, "d1" * 16)]
        bc = [("anchor", [refresh.Comment("block text", "Someone",
                                          "2026-07-02T00:00:00Z", "c2" * 16, "d2" * 16)])]
        out = refresh.comment_bullets(pc, bc)
        self.assertEqual([refresh.bullet_cid(b) for b in out], ["c1" * 16, "c2" * 16])
        self.assertTrue(out[0].startswith('- **on** "(page-level)" — Someone (2026-07-01):'))

    def test_merge_on_the_content_tier_keys_on_the_id_too(self):
        old = refresh.stamp_cid('- **on** "a" — Someone (2026-07-01): text', "c1" * 16)
        fresh = [refresh.stamp_cid('- **on** "a" — Someone (2026-07-01): edited', "c1" * 16)]
        merged, newly = refresh.merge_comment_bullets(old, fresh, "2026-08-06")
        self.assertEqual(merged, fresh)
        self.assertEqual(newly, 0)


class MigrationTestCase(unittest.TestCase):
    """`migrate_comment_ids.py` over a temp mirror. Offline unless a test opts
    into the fake refetch api; `refresh`'s corpus paths are rebound per test."""

    DB = "Tasks " + "dd" * 16
    AUTHOR = "uu" * 16

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="a5-migration-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ws = os.path.join(self.tmp, "workspace")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(os.path.join(self.ws, "_databases", self.DB))
        os.makedirs(self.state)
        for name, val in (("WS", self.ws), ("DBS", os.path.join(self.ws, "_databases")),
                          ("STATE", self.state)):
            self.addCleanup(setattr, refresh, name, getattr(refresh, name))
            setattr(refresh, name, val)
        self.write_users({self.AUTHOR: "Someone"})
        self.lock = os.path.join(self.tmp, "lock")

    def write_users(self, m):
        with open(os.path.join(self.state, "users.json"), "w") as f:
            json.dump(m, f)

    def row_path(self, rid):
        return os.path.join(self.ws, "_databases", self.DB, f"A row {rid}.md")

    def write_row(self, rid, bullets, body=""):
        """A row `.md` exactly as the nightly renders one."""
        enrich = ("\n\n## Body\n\n" + body if body else "") + \
                 "\n\n## Comments\n\n" + "\n".join(bullets) + "\n"
        page = {"id": refresh.dashed(rid),
                "properties": {"Name": {"type": "title", "title": [{"plain_text": "A row"}]}},
                "last_edited_time": "2026-08-01T00:00:00.000Z"}
        txt = refresh.render_row_md(page, "ff" * 16, "Tasks", ["Name"], None, enrich)
        with open(self.row_path(rid), "w") as f:
            f.write(txt)
        return txt

    def bullets_of(self, rid):
        with open(self.row_path(rid)) as f:
            txt = f.read()
        body = refresh.extract_comments_body(txt.split(refresh.MARKER, 1)[1])
        return refresh.split_bullets(body, prefix="- _")

    def write_captures(self, entries, name=None):
        path = os.path.join(self.state, name or mig.CAPTURE_NAME)
        with open(path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def capture_entry(self, page, comments, captured_at="2026-08-01T00:00:00Z"):
        return {"page_id": refresh.dashed(page), "captured_at": captured_at,
                "comments": [dict({"author_id": self.AUTHOR,
                                   "created_time": "2026-07-01T09:00:00.000Z"}, **c)
                             for c in comments]}

    def run_migration(self, *argv):
        return mig.main(["--lock", self.lock, *argv])


class TestMigrationStamping(MigrationTestCase):

    def test_a_dry_run_reports_but_writes_nothing(self):
        before = self.write_row(ROW, ["- _Someone (2026-07-01):_ hello"])
        self.write_captures([self.capture_entry(
            ROW, [{"id": refresh.dashed("c1" * 16), "text": "hello"}])])
        self.assertEqual(self.run_migration(), 0)
        with open(self.row_path(ROW)) as f:
            self.assertEqual(f.read(), before, "a dry run must not touch the corpus")

    def test_apply_stamps_the_bullet_with_its_captured_id(self):
        self.write_row(ROW, ["- _Someone (2026-07-01):_ hello"])
        self.write_captures([self.capture_entry(
            ROW, [{"id": refresh.dashed("c1" * 16),
                   "discussion_id": refresh.dashed("d1" * 16), "text": "hello"}])])
        self.run_migration("--apply")
        got = self.bullets_of(ROW)
        self.assertEqual(len(got), 1)
        self.assertEqual(refresh.bullet_cid(got[0]), "c1" * 16)
        self.assertIn("d=" + "d1" * 16, got[0])

    def test_only_the_bullet_first_lines_change(self):
        before = self.write_row(ROW, ["- _Someone (2026-07-01):_ hello\n  continued here"],
                                body="a paragraph the migration must not touch")
        self.write_captures([self.capture_entry(
            ROW, [{"id": refresh.dashed("c1" * 16), "text": "hello\ncontinued here"}])])
        self.run_migration("--apply")
        with open(self.row_path(ROW)) as f:
            after = f.read()
        self.assertEqual(after.replace(refresh.cid_trailer("c1" * 16, ""), ""), before)
        self.assertIn("\n  continued here", after)

    def test_re_running_is_a_no_op(self):
        self.write_row(ROW, ["- _Someone (2026-07-01):_ hello"])
        self.write_captures([self.capture_entry(
            ROW, [{"id": refresh.dashed("c1" * 16), "text": "hello"}])])
        self.run_migration("--apply")
        with open(self.row_path(ROW)) as f:
            once = f.read()
        self.run_migration("--apply")
        with open(self.row_path(ROW)) as f:
            self.assertEqual(f.read(), once, "a stamped bullet must not be stamped twice")

    def test_identical_bullets_pair_one_to_one_with_identical_captures(self):
        # Both bullets read the same; two comments exist. Claiming must consume,
        # or both bullets take the first id and the row asserts two-share-an-id.
        self.write_row(ROW, ["- _Someone (2026-07-01):_ TBD",
                             "- _Someone (2026-07-01):_ TBD"])
        self.write_captures([self.capture_entry(ROW, [
            {"id": refresh.dashed("c1" * 16), "discussion_id": refresh.dashed("d1" * 16),
             "text": "TBD"},
            {"id": refresh.dashed("c2" * 16), "discussion_id": refresh.dashed("d2" * 16),
             "text": "TBD"}])])
        self.run_migration("--apply")
        cids = [refresh.bullet_cid(b) for b in self.bullets_of(ROW)]
        self.assertEqual(sorted(cids), sorted(["c1" * 16, "c2" * 16]))

    def test_a_bullet_no_source_resolves_is_shimmed_and_kept(self):
        self.write_row(ROW, ["- _Someone (2026-01-01):_ a long-resolved thought"])
        self.run_migration("--apply")
        got = self.bullets_of(ROW)
        self.assertEqual(len(got), 1, "append-only: a bullet is never dropped")
        self.assertIn("a long-resolved thought", got[0])
        self.assertTrue(got[0].endswith("<!-- notion:cid legacy -->"))
        self.assertIsNone(refresh.bullet_cid(got[0]))

    def test_an_unparseable_bullet_is_shimmed_rather_than_skipped(self):
        self.write_row(ROW, ["- _not the shape we write_"])
        self.run_migration("--apply")
        got = self.bullets_of(ROW)
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0].endswith("<!-- notion:cid legacy -->"))

    def test_comments_md_is_left_alone_unless_asked_for(self):
        path = os.path.join(self.ws, "_comments.md")
        section = ('## A page  `' + "ee" * 16 + '`\n\n'
                   '- **on** "anchor" — Someone (2026-07-01): content tier\n')
        with open(path, "w") as f:
            f.write(section)
        self.write_captures([self.capture_entry(
            "ee" * 16, [{"id": refresh.dashed("c1" * 16), "text": "content tier"}])])
        self.run_migration("--apply")
        with open(path) as f:
            self.assertEqual(f.read(), section, "the 6.1 MB file is out of scope by default")
        self.run_migration("--apply", "--include-comments-md")
        with open(path) as f:
            self.assertIn(refresh.cid_trailer("c1" * 16, ""), f.read())


class TestMigrationSources(MigrationTestCase):

    def test_the_pre_repair_log_supplies_ids_the_repaired_one_lacks(self):
        self.write_row(ROW, ["- _Someone (2026-07-01):_ old thread"])
        self.write_captures([])
        self.write_captures([self.capture_entry(
            ROW, [{"id": refresh.dashed("c1" * 16), "text": "old thread"}])],
            name=mig.PRE_REPAIR_NAME)
        self.run_migration("--apply")
        self.assertEqual(refresh.bullet_cid(self.bullets_of(ROW)[0]), "c1" * 16)

    def test_a_comment_the_repair_moved_elsewhere_is_not_re_imported(self):
        # The 2026-07-27 repair reattributed this comment to another page.
        # Taking it from the pre-repair log would put the misattribution back.
        self.write_row(ROW, ["- _Someone (2026-07-01):_ foreign thread"])
        self.write_captures([self.capture_entry(
            "ee" * 16, [{"id": refresh.dashed("c1" * 16), "text": "foreign thread"}])])
        self.write_captures([self.capture_entry(
            ROW, [{"id": refresh.dashed("c1" * 16), "text": "foreign thread"}])],
            name=mig.PRE_REPAIR_NAME)
        self.run_migration("--apply")
        got = self.bullets_of(ROW)
        self.assertIsNone(refresh.bullet_cid(got[0]),
                          "the pre-repair attribution must lose to the repaired log")
        self.assertIn("foreign thread", got[0], "and the bullet still is not dropped")

    def test_no_pre_repair_flag_ignores_that_source_entirely(self):
        self.write_row(ROW, ["- _Someone (2026-07-01):_ old thread"])
        self.write_captures([self.capture_entry(
            ROW, [{"id": refresh.dashed("c1" * 16), "text": "old thread"}])],
            name=mig.PRE_REPAIR_NAME)
        self.run_migration("--apply", "--no-pre-repair")
        self.assertIsNone(refresh.bullet_cid(self.bullets_of(ROW)[0]))

    def test_a_capture_stored_as_rich_text_matches_the_plain_corpus_text(self):
        # Captures written from 2026-08-06 render `text` through rich_md, but the
        # corpus on disk was written with `plain`. Both renderings are indexed.
        href = "https://www.notion.so/" + "d" * 32
        self.write_row(ROW, ["- _Someone (2026-07-01):_ see Untitled"])
        self.write_captures([self.capture_entry(ROW, [{
            "id": refresh.dashed("c1" * 16),
            "text": f"see [Untitled]({href})",
            "rich_text": [{"type": "text", "plain_text": "see ", "href": None},
                          {"type": "mention", "plain_text": "Untitled", "href": href}]}])])
        self.run_migration("--apply")
        self.assertEqual(refresh.bullet_cid(self.bullets_of(ROW)[0]), "c1" * 16)


class TestMigrationRefetch(MigrationTestCase):

    def install_api(self, comments, budget=100):
        """`--refetch` builds its own Api; hand it a fake and count the calls."""
        calls = []

        class Api(FakeApi):
            def __init__(self, token, rps, bud):
                super().__init__(comments=comments)
                self.budget = bud

            def check_budget(self):
                if self.n >= self.budget:
                    raise refresh.Budget()

            def paginate(self, method, path, body=None, params=None, ver=None):
                calls.append(refresh.undash((params or {})["block_id"]))
                yield from super().paginate(method, path, body, params, ver)

        self.addCleanup(setattr, refresh, "Api", refresh.Api)
        self.addCleanup(setattr, refresh, "Users", refresh.Users)
        refresh.Api = Api
        refresh.Users = lambda api: FakeUsersWithSave()
        os.environ.setdefault("NOTION_TOKEN", "t")
        return calls

    def test_a_live_refetch_resolves_what_the_logs_could_not(self):
        self.write_row(ROW, ["- _Someone (2026-07-01):_ still open"])
        calls = self.install_api({ROW: [comment("c1" * 16, "still open")]})
        self.run_migration("--apply", "--refetch", "--budget", "10")
        self.assertEqual(calls, [ROW])
        self.assertEqual(refresh.bullet_cid(self.bullets_of(ROW)[0]), "c1" * 16)

    def test_a_row_already_refetched_is_never_asked_twice(self):
        self.write_row(ROW, ["- _Someone (2026-07-01):_ never returned"])
        calls = self.install_api({})          # the API knows nothing about it
        self.run_migration("--apply", "--refetch", "--budget", "10")
        self.assertEqual(calls, [ROW])
        self.assertTrue(self.bullets_of(ROW)[0].endswith("<!-- notion:cid legacy -->"))
        self.run_migration("--apply", "--refetch", "--budget", "10")
        self.assertEqual(calls, [ROW], "the resumed run must not re-spend the request")

    def test_a_later_refetch_upgrades_a_bullet_shimmed_by_an_earlier_run(self):
        # Running --apply before --apply --refetch must not strand the bullet:
        # `legacy` records that no source had the id *last time*, not a verdict.
        self.write_row(ROW, ["- _Someone (2026-07-01):_ still open"])
        self.run_migration("--apply")
        self.assertTrue(self.bullets_of(ROW)[0].endswith("<!-- notion:cid legacy -->"))
        calls = self.install_api({ROW: [comment("c1" * 16, "still open")]})
        self.run_migration("--apply", "--refetch", "--budget", "10")
        self.assertEqual(calls, [ROW])
        got = self.bullets_of(ROW)
        self.assertEqual(len(got), 1)
        self.assertEqual(refresh.bullet_cid(got[0]), "c1" * 16)
        self.assertEqual(got[0].count("notion:cid"), 1, "one marker, not two")

    def test_refetch_without_apply_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_migration("--refetch", "--budget", "10")

    def test_refetch_without_a_budget_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_migration("--apply", "--refetch")

    def test_a_budget_stop_is_a_normal_exit_and_keeps_what_it_did(self):
        for n in range(3):
            self.write_row(f"{n:032x}", ["- _Someone (2026-07-01):_ open"])
        self.install_api({}, budget=1)
        self.assertEqual(self.run_migration("--apply", "--refetch", "--budget", "1"), 0)


class TestMigrationLock(MigrationTestCase):

    def test_a_writing_run_takes_the_mirror_lock(self):
        self.write_row(ROW, ["- _Someone (2026-07-01):_ hello"])
        held = coverage_backfill.take_lock(self.lock)
        self.addCleanup(held.close)
        self.assertEqual(self.run_migration("--apply"), 1,
                         "a writer must refuse while another holds the mirror lock")
        self.assertEqual(self.bullets_of(ROW), ["- _Someone (2026-07-01):_ hello"])

    def test_a_dry_run_does_not_need_the_lock(self):
        self.write_row(ROW, ["- _Someone (2026-07-01):_ hello"])
        held = coverage_backfill.take_lock(self.lock)
        self.addCleanup(held.close)
        self.assertEqual(self.run_migration(), 0)


class FakeUsersWithSave(FakeUsers):
    def save(self):
        pass


if __name__ == "__main__":
    unittest.main()
