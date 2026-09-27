"""One comment, one bullet, one place.

Two ways a comment ended up in the mirror twice, both measured on the live mirror:

* A bullet with no comment id (`legacy`) was matched to its fresh, id-bearing
  copy by its whole rendered text, anchor included. An older renderer wrote the
  anchor and every @-mention differently (`‣` for a mention, an anchor cut
  elsewhere), so the text never matched: every rescan kept the legacy bullet,
  annotated it resolved, and added the id'd copy beside it. The one-off legacy
  upgrade did this to a few hundred comments. A legacy bullet now pairs with
  an id'd bullet of the same author and day whose text agrees once mentions and
  link targets are set aside, and the id'd bullet replaces it.
* A database row can have a section in `_comments.md` as well as its row file
  (a one-off re-capture walked two database rows as pages; a wiki page is also a
  row of its wiki database), and each tier then kept its own copy
  of the thread. A row's comments now live in its row file only: whatever the
  page tier would write for an id that has a row file goes there.

Two different ids with the same text are two comments (checked against the API:
distinct discussions and times) and are both kept.
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh  # noqa: E402
from fake_notion import MirrorSandbox, page_obj  # noqa: E402

C1, C2, C3 = "d1" * 16, "d2" * 16, "d3" * 16
D1 = "e1" * 16


def pb(anchor, who, day, text, cid="", did="", resolved=""):
    b = refresh.stamp_cid(f'- **on** "{anchor}" — {who} ({day}): {text}', cid, did)
    return refresh.annotate_resolved(b, resolved) if resolved else b


def rb(who, day, text, cid="", did="", resolved=""):
    b = refresh.stamp_cid(f"- _{who} ({day}):_ {text}", cid, did)
    return refresh.annotate_resolved(b, resolved) if resolved else b


class Reconcile(unittest.TestCase):
    def test_a_legacy_bullet_beside_its_id_bearing_copy_is_dropped(self):
        legacy = pb("t Team ‣", "Avery", "2023-12-29", "‣ fyi. This draft is still a work in progress",
                    resolved="2026-09-26")
        ided = pb("Settle the open questions", "Avery", "2023-12-29",
                  "@Robin Kay fyi. This draft is still a work in progress", C1, D1)
        self.assertEqual(refresh.reconcile_bullets([ided, legacy]), [ided])
        self.assertEqual(refresh.reconcile_bullets([legacy, ided]), [ided])

    def test_a_page_link_and_a_mention_glyph_read_alike(self):
        legacy = pb("a", "Blake", "2024-01-02", "NOTE: REPLACED BY ‣ see the new plan")
        ided = pb("b", "Blake", "2024-01-02",
                  "NOTE: REPLACED BY [Untitled](https://app.notion.com/p/2c6f) see the new plan", C1)
        self.assertEqual(refresh.reconcile_bullets([legacy, ided]), [ided])

    def test_a_legacy_bullet_with_no_copy_is_kept(self):
        legacy = pb("a", "Blake", "2024-01-02", "an old thread whose id is gone", resolved="2026-07-20")
        other = pb("a", "Blake", "2024-01-02", "something else entirely said that day", C1)
        self.assertEqual(refresh.reconcile_bullets([legacy, other]), [legacy, other])

    def test_another_author_or_day_is_not_the_same_comment(self):
        legacy = pb("a", "Blake", "2024-01-02", "the same words said twice here")
        self.assertEqual(len(refresh.reconcile_bullets(
            [legacy, pb("a", "Avery", "2024-01-02", "the same words said twice here", C1)])), 2)
        self.assertEqual(len(refresh.reconcile_bullets(
            [legacy, pb("a", "Blake", "2024-01-03", "the same words said twice here", C1)])), 2)

    def test_a_short_text_pairs_only_on_the_same_anchor(self):
        """"done", "sent ✅": people post these more than once a day, so the words
        alone do not say it is the same comment."""
        legacy = pb("Row 3", "Casey Doe", "2024-10-25", "done")
        self.assertEqual(len(refresh.reconcile_bullets(
            [legacy, pb("Row 4", "Casey Doe", "2024-10-25", "done", C1)])), 2)
        self.assertEqual(len(refresh.reconcile_bullets(
            [legacy, pb("Row 3", "Casey Doe", "2024-10-25", "done", C1)])), 1)

    def test_a_short_row_comment_is_never_paired(self):
        self.assertEqual(len(refresh.reconcile_bullets(
            [rb("Casey Doe", "2024-10-25", "done"), rb("Casey Doe", "2024-10-25", "done", C1)])), 2)

    def test_row_bullets_pair_like_page_bullets(self):
        legacy = rb("Casey Doe", "2025-07-16", "update 2025_07_16:\n  ‣ was not done for the new site",
                    resolved="2026-09-26")
        ided = rb("Casey Doe", "2025-07-16",
                  "update 2025_07_16:\n  [Untitled](https://app.notion.com/p/22af) was not done for the new site",
                  C1, D1)
        self.assertEqual(refresh.reconcile_bullets([ided, legacy]), [ided])

    def test_one_id_bearing_copy_absorbs_one_legacy_bullet(self):
        a = pb("x", "Avery", "2024-03-08", "Of the survey answers how many changed their plans")
        b = pb("y", "Avery", "2024-03-08", "Of the survey answers how many changed their plans")
        ided = pb("z", "Avery", "2024-03-08", "Of the survey answers how many changed their plans", C1)
        self.assertEqual(refresh.reconcile_bullets([a, b, ided]), [b, ided])

    def test_the_same_id_twice_keeps_the_first(self):
        first = pb("Settle the", "Avery", "2023-12-29", "Good point here", C1, D1)
        second = pb("t Team ‣", "Avery", "2023-12-29", "Good point here", C1, D1)
        self.assertEqual(refresh.reconcile_bullets([first, second]), [first])

    def test_two_ids_with_the_same_text_are_two_comments(self):
        a = pb("x", "Casey Doe", "2025-12-22", "sent ✅", C1)
        b = pb("x", "Casey Doe", "2025-12-22", "sent ✅", C2)
        self.assertEqual(refresh.reconcile_bullets([a, b]), [a, b])


class ToRowBullet(unittest.TestCase):
    def test_an_anchor_over_two_lines_keeps_its_id_on_the_first_line(self):
        """stamp_cid puts the trailer on a bullet's first line, which for an anchor
        with a line break is inside the anchor: read naively, the converted bullet
        lost its id into a continuation line of the bullet before it."""
        b = pb("Role Description:\nResearchers here get the chance", "Avery", "2024-01-29",
               "So how does this usually work elsewhere", C1, D1, resolved="2026-07-20")
        out = refresh.to_row_bullet(b)
        self.assertEqual(out.split("\n")[0],
                         "- _Avery (2024-01-29):_ So how does this usually work elsewhere"
                         " _[resolved/deleted ≤2026-07-20]_" + refresh.cid_trailer(C1, D1))
        self.assertEqual(refresh.split_bullets("\n" + out, prefix="- _"), [out])

    def test_continuation_lines_are_indented(self):
        out = refresh.to_row_bullet(pb("a", "Casey Doe", "2025-07-16", "update:\n‣ was not done\n• ✅ done", C1))
        self.assertEqual(out, "- _Casey Doe (2025-07-16):_ update:" + refresh.cid_trailer(C1)
                         + "\n  ‣ was not done\n  • ✅ done")


class ChildPageBlocks(unittest.TestCase):
    def test_a_child_page_block_is_not_listed_for_comments(self):
        """Its id is the child page's, so its comments are the child's own."""
        from notion_core.walker import Walker
        from fake_notion import FakeUsers
        blocks = [{"id": "c4" * 16, "type": "child_page", "child_page": {"title": "Child"}},
                  {"id": "c5" * 16, "type": "paragraph", "paragraph": {"rich_text": [
                      {"type": "text", "plain_text": "text", "text": {"content": "text"}, "annotations": {}}]}}]
        api = types.SimpleNamespace(paginate=lambda *a, **kw: iter(blocks), n=0)
        w = Walker(api, FakeUsers())
        w.walk("aa" * 16, [], 0)
        self.assertEqual([bid for bid, _a in w.block_anchors], ["c5" * 16])
        self.assertEqual(w.child_pages, [("c4" * 16, "Child")])


class Merge(unittest.TestCase):
    def test_a_rescan_upgrades_a_legacy_bullet_in_place_of_annotating_it(self):
        legacy = pb("(table row)", "Blake", "2024-02-07", "how is “baseline” defined ‣?")
        fresh = pb("(table)", "Blake", "2024-02-07", "how is “baseline” defined @Avery?", C1)
        out, newly = refresh.merge_comment_bullets("\n" + legacy + "\n", [fresh], "2026-09-27")
        self.assertEqual(out, [fresh])
        self.assertEqual(newly, 0)


class Fold(unittest.TestCase):
    def cap(self, text, at):
        return {"id": refresh.dashed(C1), "text": text, "rich_text": [
            {"type": "text", "text": {"content": text}, "plain_text": text, "annotations": {}}],
            "author_id": "uu" * 16, "created_time": "2024-02-07T09:00:00.000Z",
            "discussion_id": refresh.dashed(D1), "_captured_at": at}

    def fold(self, stored, c, scan="2026-09-10T00:00:00.000Z"):
        from fake_notion import FakeUsers
        who = FakeUsers().name({"id": "uu" * 16})
        return refresh.fold_bullets([s.replace("WHO", who) for s in stored], [("(page-level)", c)],
                                    scan, FakeUsers(), False, {}, "2026-09-27")

    def test_a_capture_replaces_its_legacy_bullet_where_it_stands(self):
        stored = ['- **on** "(page-level)" — WHO (2024-02-07): first thing said here' + refresh.cid_trailer(),
                  '- **on** "(page-level)" — WHO (2024-02-07): ‣ please look at this soon' + refresh.cid_trailer()]
        out, st = self.fold(stored, self.cap("@Avery please look at this soon", "2026-09-20T00:00:00+00:00"))
        self.assertEqual(len(out), 2)
        self.assertEqual(refresh.bullet_cid(out[1]), C1)
        self.assertNotIn("resolved", out[1])
        self.assertEqual(st["added"], 0)

    def test_an_older_capture_keeps_the_legacy_bullets_resolved_date(self):
        stored = [refresh.annotate_resolved(
            '- **on** "(page-level)" — WHO (2024-02-07): ‣ please look at this soon' + refresh.cid_trailer(),
            "2026-07-20")]
        out, _ = self.fold(stored, self.cap("@Avery please look at this soon", "2026-09-01T00:00:00+00:00"))
        self.assertEqual(refresh.bullet_cid(out[0]), C1)
        self.assertIn("_[resolved/deleted ≤2026-07-20]_", out[0])


ROW, DB, PAGE = "b2" * 16, "c3" * 16, "a1" * 16


class RowOwnsItsComments(MirrorSandbox):
    def setUp(self):
        super().setUp()
        self.dbdir = os.path.join(self.dbs, f"Ideas {DB}")
        os.makedirs(self.dbdir)
        self.row_path = os.path.join(self.dbdir, f"Row {ROW}.md")
        self.write_row([rb("Casey Doe", "2025-03-14", "already on the row", C1, D1)])
        self.write_meta(page_obj(PAGE, "A page"))
        self.report = refresh.new_report("daily")

    def write_row(self, bullets, marker=True):
        body = "\n".join(refresh.body_section_lines("- the row body"))
        txt = "<!-- notion db row -->\n# Row\n\n| Property | Value |\n"
        if marker:
            txt += "\n" + refresh.MARKER + "\n" + body + "\n"
            if bullets:
                txt = txt.rstrip("\n") + "\n" + "\n".join(refresh.comments_section_lines(bullets)) + "\n"
        with open(self.row_path, "w") as f:
            f.write(txt)

    def row_bullets(self):
        txt = self.read(self.row_path)
        return refresh.split_bullets(refresh.stored_comments_body(txt.split(refresh.MARKER, 1)[1]),
                                     prefix="- _")

    def test_a_page_tier_update_for_a_row_lands_in_the_row_file(self):
        refresh.update_comments_md({ROW: {"title": "Row", "bullets": [
            pb("(page-level)", "Casey Doe", "2025-03-14", "already on the row", C1, D1),
            pb("some block", "Dana", "2025-03-15", "a new comment on a block", C2)]}}, self.report)
        self.assertEqual([refresh.bullet_cid(b) for b in self.row_bullets()], [C1, C2])
        self.assertIn("- _Dana (2025-03-15):_ a new comment on a block", self.row_bullets()[1])
        comments = os.path.join(self.ws, "_comments.md")
        self.assertFalse(os.path.exists(comments) and ROW in self.read(comments))

    def test_page_sections_are_untouched(self):
        refresh.update_comments_md({PAGE: {"title": "A page", "bullets": [
            pb("(page-level)", "Avery", "2025-01-01", "on a real page", C3)]}}, self.report)
        self.assertIn(f"## A page  `{PAGE}`", self.read(os.path.join(self.ws, "_comments.md")))

    def test_a_row_without_an_enrichment_region_gets_one(self):
        self.write_row([], marker=False)
        refresh.update_comments_md({ROW: {"title": "Row", "bullets": [
            pb("(page-level)", "Dana", "2025-03-15", "first comment on this row", C2)]}}, self.report)
        self.assertEqual([refresh.bullet_cid(b) for b in self.row_bullets()], [C2])

    def seed_comments_md(self, sections):
        head = "# Notion comments (content pages)\n\n"
        with open(os.path.join(self.ws, "_comments.md"), "w") as f:
            f.write(head + "".join(f"## {t}  `{i}`\n\n" + "\n".join(bs) + "\n\n" for t, i, bs in sections))

    def test_the_dedup_pass_moves_a_rows_section_into_its_row_and_cleans_both_tiers(self):
        self.seed_comments_md([
            ("Row", ROW, [pb("(page-level)", "Casey Doe", "2025-03-14", "already on the row", C1, D1),
                          pb("(page-level)", "Casey Doe", "2025-08-25", "‣ fyi this checklist exists for launches"),
                          pb("(page-level)", "Casey Doe", "2025-08-25",
                             "@Robin fyi this checklist exists for launches", C2)]),
            ("A page", PAGE, [pb("x", "Avery", "2023-12-29", "Good point here", C3),
                              pb("y", "Avery", "2023-12-29", "Good point here", C3)])])
        args = types.SimpleNamespace(dry_run=False)
        refresh.phase_comment_dedup(None, self.state(), self.report, args)
        self.assertEqual([refresh.bullet_cid(b) for b in self.row_bullets()], [C1, C2])
        _h, sections, _a = refresh.load_comments_md()
        self.assertEqual([s["id"] for s in sections], [PAGE])
        self.assertEqual(len(refresh.split_bullets(sections[0]["body"])), 1)
        dd = self.report["comments"]["dedup"]
        # repeats: the row already had C1, and the page section held C3 twice
        self.assertEqual((dd["sections_moved"], dd["legacy_dropped"], dd["repeats_dropped"]), (1, 1, 2))
        # idempotent
        before = (self.read(self.row_path), self.read(os.path.join(self.ws, "_comments.md")))
        refresh.phase_comment_dedup(None, self.state(), refresh.new_report("comment-dedup"), args)
        self.assertEqual(before, (self.read(self.row_path), self.read(os.path.join(self.ws, "_comments.md"))))

    def test_the_dedup_pass_writes_nothing_on_a_dry_run(self):
        self.seed_comments_md([("Row", ROW, [pb("(page-level)", "Dana", "2025-03-15", "only here", C2)])])
        before = (self.read(self.row_path), self.read(os.path.join(self.ws, "_comments.md")))
        refresh.phase_comment_dedup(None, self.state(), self.report, types.SimpleNamespace(dry_run=True))
        self.assertEqual(before, (self.read(self.row_path), self.read(os.path.join(self.ws, "_comments.md"))))
        self.assertEqual(self.report["comments"]["dedup"]["sections_moved"], 1)

    def test_a_child_pages_comment_listed_by_its_parent_stays_with_the_child(self):
        """The row's scan listed the comments of its child page (a child_page
        block's id is the child's); the thread says the comment is the child's."""
        child = "c4" * 16
        self.write_row([rb("Blake", "2023-12-04", "happy to hear your thoughts", C1, D1),
                        rb("Blake", "2023-12-05", "a comment on the row itself", C2, "e2" * 16)])
        self.seed_comments_md([("Child", child, [pb("(page-level)", "Blake", "2023-12-04",
                                                    "happy to hear your thoughts", C1, D1)])])
        st = self.state(comment_parents={D1: child, "e2" * 16: ROW})
        refresh.phase_comment_dedup(None, st, self.report, types.SimpleNamespace(dry_run=False))
        self.assertEqual([refresh.bullet_cid(b) for b in self.row_bullets()], [C2])
        self.assertIn(C1, self.read(os.path.join(self.ws, "_comments.md")))
        self.assertEqual(self.report["comments"]["dedup"]["child_page_copies_dropped"], 1)

    def test_an_unknown_thread_is_looked_up_once_and_recorded(self):
        child = "c4" * 16
        self.write_row([rb("Blake", "2023-12-04", "happy to hear your thoughts", C1, D1)])
        self.seed_comments_md([("Child", child, [pb("(page-level)", "Blake", "2023-12-04",
                                                    "happy to hear your thoughts", C1, D1)])])
        api = types.SimpleNamespace(get=lambda path, **kw: {"parent": {"type": "page_id",
                                                                       "page_id": refresh.dashed(child)}})
        st = self.state()
        refresh.phase_comment_dedup(api, st, self.report, types.SimpleNamespace(dry_run=False))
        self.assertEqual(st["comment_parents"], {D1: child})
        self.assertEqual(self.row_bullets(), [])

    def test_a_resolved_child_page_comment_is_placed_by_its_anchor(self):
        """GET /comments/{id} 404s for a deleted comment, and a legacy bullet has no
        id to ask about: the parent's copy is anchored on the child's title."""
        child = "c4" * 16
        self.seed_comments_md([
            ("Parent", PAGE, [pb("Operations Lead", "Blake", "2025-04-05", "have a look and adjust", C1, D1),
                              pb("Operations Lead", "Blake", "2025-04-05", "‣ an old one with no id here"),
                              pb("Intro", "Blake", "2025-04-05", "a comment on the parent's own block", C2)]),
            ("Operations Lead", child, [
                pb("(page-level)", "Blake", "2025-04-05", "have a look and adjust", C1, D1),
                pb("(page-level)", "Blake", "2025-04-05", "@Avery an old one with no id here", C3)])])

        def gone(path, **kw):
            raise refresh.ApiError(404, "object_not_found")
        refresh.phase_comment_dedup(types.SimpleNamespace(get=gone), self.state(), self.report,
                                    types.SimpleNamespace(dry_run=False))
        secs = {s["id"]: refresh.split_bullets(s["body"]) for s in refresh.load_comments_md()[1]}
        self.assertEqual([refresh.bullet_cid(b) for b in secs[PAGE]], [C2])
        self.assertEqual([refresh.bullet_cid(b) for b in secs[child]], [C1, C3])

    def test_a_synced_blocks_comment_stays_in_both_pages(self):
        other = "c5" * 16
        self.seed_comments_md([
            ("A page", PAGE, [pb("(table)", "Blake", "2024-02-07", "how is this defined", C3, D1)]),
            ("Other", other, [pb("(table)", "Blake", "2024-02-07", "how is this defined", C3, D1)])])
        st = self.state(comment_parents={D1: "9e" * 16})
        refresh.phase_comment_dedup(None, st, self.report, types.SimpleNamespace(dry_run=False))
        self.assertEqual(self.read(os.path.join(self.ws, "_comments.md")).count(C3), 2)


if __name__ == "__main__":
    unittest.main()
