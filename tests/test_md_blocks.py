"""B3: the markdown -> Notion blocks converter.

Every parser here is tested against the *exact* string `notion_core/walker.py`'s
renderer emits for that block type, because those two halves are a round trip: the mirror
renders a Notion page to markdown, and this converter pushes markdown back. A
parser that accepts a shape the renderer never emits is untested surface; one
that rejects a shape the renderer does emit silently corrupts a live page.

The refusal tests are the other half. Several Notion block types survive the
render only as a stand-in line — a hosted image, a sub-page, an embed — and pushing
that line back either duplicates the block or converts it to a paragraph. Those
must fail loudly and name the line, never degrade quietly. A sentinelled bookmark is
the one stand-in that converts instead, because its line carries the whole block.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from notion_core import md_blocks as mb  # noqa: E402


def text_of(block):
    """The plain text of a block, however it is annotated."""
    body = block[block["type"]]
    return "".join(r.get("text", {}).get("content", "") for r in body.get("rich_text", []))


def types_of(blocks):
    return [b["type"] for b in blocks]


class ToDoTestCase(unittest.TestCase):
    def test_checked_box_parses_as_a_checked_to_do(self):
        [b] = mb.lines_blocks("- [x] ship the thing")
        self.assertEqual(b["type"], "to_do")
        self.assertTrue(b["to_do"]["checked"])
        self.assertEqual(text_of(b), "ship the thing")

    def test_empty_box_parses_as_an_unchecked_to_do(self):
        [b] = mb.lines_blocks("- [ ] ship the thing")
        self.assertEqual(b["type"], "to_do")
        self.assertFalse(b["to_do"]["checked"])

    def test_a_plain_bullet_is_still_a_bullet(self):
        [b] = mb.lines_blocks("- ship the thing")
        self.assertEqual(b["type"], "bulleted_list_item")


class ToggleTestCase(unittest.TestCase):
    def test_the_renderers_toggle_marker_parses_as_a_toggle(self):
        [b] = mb.lines_blocks("- ▸ Details")
        self.assertEqual(b["type"], "toggle")
        self.assertEqual(text_of(b), "Details")


class DividerTestCase(unittest.TestCase):
    def test_a_lone_rule_parses_as_a_divider(self):
        blocks = mb.lines_blocks("before\n\n---\n\nafter")
        self.assertEqual(types_of(blocks), ["paragraph", "divider", "paragraph"])
        self.assertEqual(blocks[1]["divider"], {})


class CalloutTestCase(unittest.TestCase):
    def test_a_quote_with_an_emoji_icon_parses_as_a_callout(self):
        [b] = mb.lines_blocks("> 💡 mind the gap")
        self.assertEqual(b["type"], "callout")
        self.assertEqual(b["callout"]["icon"], {"type": "emoji", "emoji": "💡"})
        self.assertEqual(text_of(b), "mind the gap")

    def test_a_multi_codepoint_icon_survives_whole(self):
        [b] = mb.lines_blocks("> ⚠️ mind the gap")
        self.assertEqual(b["callout"]["icon"], {"type": "emoji", "emoji": "⚠️"})
        self.assertEqual(text_of(b), "mind the gap")

    def test_an_iconless_callout_keeps_its_type(self):
        # refresh.py renders `> {icon} {text}` and icon is "" when the callout has
        # none, so the doubled space is the only signal that this is not a quote.
        [b] = mb.lines_blocks(">  we are getting some data")
        self.assertEqual(b["type"], "callout")
        self.assertNotIn("icon", b["callout"])
        self.assertEqual(text_of(b), "we are getting some data")

    def test_a_bullet_glyph_is_not_an_icon(self):
        [b] = mb.lines_blocks("> • Years of research experience")
        self.assertEqual(b["type"], "quote")
        self.assertEqual(text_of(b), "• Years of research experience")


class EquationTestCase(unittest.TestCase):
    def test_a_fenced_expression_parses_as_an_equation(self):
        [b] = mb.lines_blocks("$$\nE = mc^2\n$$")
        self.assertEqual(b["type"], "equation")
        self.assertEqual(b["equation"]["expression"], "E = mc^2")

    def test_a_multi_line_expression_keeps_its_newlines(self):
        [b] = mb.lines_blocks("$$\na = 1\nb = 2\n$$")
        self.assertEqual(b["equation"]["expression"], "a = 1\nb = 2")


class HeadingTestCase(unittest.TestCase):
    def test_a_single_hash_stays_a_heading_1(self):
        [b] = mb.lines_blocks("# Top")
        self.assertEqual(b["type"], "heading_1")
        self.assertEqual(text_of(b), "Top")

    def test_the_three_notion_levels_map_one_to_one(self):
        blocks = mb.lines_blocks("# a\n\n## b\n\n### c")
        self.assertEqual(types_of(blocks), ["heading_1", "heading_2", "heading_3"])

    def test_a_fourth_level_clamps_to_the_deepest_notion_has(self):
        [b] = mb.lines_blocks("#### d")
        self.assertEqual(b["type"], "heading_3")

    def test_an_indented_heading_still_parses_as_a_heading(self):
        # the mirror indents every child block by two spaces per level, so a
        # pattern anchored at column 0 turns a heading into a paragraph
        blocks = mb.lines_blocks("- ▸ Details\n  ## What it is")
        self.assertEqual(types_of(blocks), ["toggle"])
        self.assertEqual(types_of(blocks[0]["toggle"]["children"]), ["heading_2"])


class TableTestCase(unittest.TestCase):
    def test_a_table_parses_with_its_header_and_rows(self):
        b, = mb.lines_blocks("| a | b |\n| --- | --- |\n| 1 | 2 |")
        self.assertEqual(b["type"], "table")
        self.assertEqual(b["table"]["table_width"], 2)
        rows = [[ "".join(c["text"]["content"] for c in cell) for cell in r["table_row"]["cells"]]
                for r in b["table"]["children"]]
        self.assertEqual(rows, [["a", "b"], ["1", "2"]])

    def test_an_indented_table_parses_whole(self):
        # entry and continuation conditions must agree: entry on the rstripped
        # line and continuation on the stripped one half-parsed every indented
        # table, keeping the header row and dropping the body
        blocks = mb.lines_blocks("- ▸ Details\n  | a | b |\n  | --- | --- |\n  | 1 | 2 |")
        table = blocks[0]["toggle"]["children"][0]
        self.assertEqual(table["type"], "table")
        self.assertEqual(len(table["table"]["children"]), 2)


SENTINEL = "<!-- notion:keep block={} type={} -->"
IMAGE_ID = "27000000000000000000000000000000"


class KeepSentinelTestCase(unittest.TestCase):
    def test_a_covered_stand_in_is_not_re_emitted(self):
        md = "before\n\n{}\n![image](ATTACH:{})\n\nafter".format(
            SENTINEL.format(IMAGE_ID, "image"), IMAGE_ID)
        self.assertEqual(types_of(mb.lines_blocks(md)), ["paragraph", "paragraph"])

    def test_a_covered_sub_page_stand_in_is_not_re_emitted(self):
        pid = "24000000000000000000000000000000"
        md = "{}\n- 📄 **Hub** — sub-page `{}`".format(SENTINEL.format(pid, "child_page"), pid)
        self.assertEqual(mb.lines_blocks(md), [])

    def test_a_sentinel_whose_id_misses_its_stand_in_refuses(self):
        other = "11000000000000000000000000000000"
        md = "{}\n![image](ATTACH:{})".format(SENTINEL.format(other, "image"), IMAGE_ID)
        with self.assertRaises(mb.Refused) as cm:
            mb.lines_blocks(md)
        self.assertIn("does not carry the sentinel's block id", str(cm.exception))

    def test_a_sentinel_over_a_line_that_is_not_a_stand_in_refuses(self):
        # media the mirror downloaded renders as `![cap](<file>)` with no id at
        # all, so the id check cannot catch a sentinel that has drifted off its
        # line; what it can check is that the covered line is a stand-in
        md = "{}\nordinary prose".format(SENTINEL.format(IMAGE_ID, "image"))
        with self.assertRaises(mb.Refused) as cm:
            mb.lines_blocks(md)
        self.assertIn("drifted", str(cm.exception))

    def test_a_sentinel_covers_downloaded_media_that_carries_no_id(self):
        md = "{}\n![image](19ffcfd1_image.png)".format(SENTINEL.format(IMAGE_ID, "image"))
        self.assertEqual(mb.lines_blocks(md), [])

    def test_a_sentinel_for_a_type_that_renders_nothing_refuses(self):
        for kind in ("column", "breadcrumb"):
            with self.subTest(kind=kind):
                with self.assertRaises(mb.Refused) as cm:
                    mb.lines_blocks(SENTINEL.format(IMAGE_ID, kind) + "\nsomething")
                self.assertIn("renders as nothing", str(cm.exception))


class StandinExtentTestCase(unittest.TestCase):
    """`standin_extent` is what stops a drifted sentinel from swallowing prose, so each
    branch has to *reject* as well as accept. Every accepted line here is quoted from
    the `walker.py` branch that writes it; every rejected one is a line that type never
    renders as — which is what a sentinel sitting on the wrong line looks like."""

    ID = "27000000000000000000000000000000"

    #: (type, a line the renderer writes for it and its length, a line it never does)
    CASES = [
        ("child_page", ["- 📄 **Hub** — sub-page `{id}`"], "Hub"),
        ("child_database", ["- 🗄️ **Rows** — database `{id}` (rows in workspace/_databases/)"],
         "**Rows**"),
        ("link_to_page", ["- 🔗 link to `{id}`"], "link to a page"),
        ("image", ["![image](ATTACH:{id})"], "![image](https://cdn.test/a.png)"),
        ("table_of_contents", ["- 📑 (table of contents)"], "- 📑 (the contents)"),
        ("template", ["- 🧩 (template: New item)"], "- 🧩 (templates: New item)"),
        # each marker is that type's and no other's, which is the whole point of them
        ("bookmark", ["- 🔖 [the write-up](https://x.test/a)"], "- 🖼️ [x](https://x.test/a)"),
        ("embed", ["- 🖼️ [https://x.test/a](https://x.test/a)"], "- 🔖 [x](https://x.test/a)"),
        ("link_preview", ["- 👁️ [https://x.test/a](https://x.test/a)"],
         "[https://x.test/a](https://x.test/a)"),
        ("equation", ["$$", "a^2 + b^2", "$$"], "a^2 + b^2"),
        ("some_type_from_next_year", ["<!-- unhandled block type: some_type_from_next_year -->"],
         "ordinary prose"),
    ]

    def test_each_type_accepts_exactly_what_the_renderer_writes_for_it(self):
        for kind, rendered, _other in self.CASES:
            with self.subTest(kind=kind):
                lines = [line.format(id=self.ID) for line in rendered]
                self.assertEqual(mb.standin_extent(kind, lines, 0), len(lines))

    def test_each_type_rejects_a_line_it_never_renders_as(self):
        for kind, _rendered, other in self.CASES:
            with self.subTest(kind=kind):
                self.assertEqual(mb.standin_extent(kind, [other.format(id=self.ID)], 0), 0)

    def test_each_marked_stand_in_belongs_to_exactly_one_type(self):
        """Every marked line matches `STANDIN`, so a branch that only asked "is this a
        stand-in?" accepted any of them for any of its types — a `child_page` sentinel
        sitting over a table of contents converted cleanly instead of refusing as
        drifted. The marker says which type it is; that is what it is for."""
        lines = {"child_page": "- 📄 **Hub** — sub-page `{id}`",
                 "child_database": "- 🗄️ **Rows** — database `{id}` (rows in x/)",
                 "link_to_page": "- 🔗 link to `{id}`",
                 "table_of_contents": "- 📑 (table of contents)",
                 "template": "- 🧩 (template: New item)",
                 "bookmark": "- 🔖 [x](https://x.test/a)"}
        for kind in lines:
            for other, line in lines.items():
                with self.subTest(sentinel=kind, line=other):
                    got = mb.standin_extent(kind, [line.format(id=self.ID)], 0)
                    self.assertEqual(got, 1 if kind == other else 0)

    def test_an_unclosed_equation_fence_is_not_a_stand_in(self):
        # the renderer always closes it, so an open one is markdown someone typed
        self.assertEqual(mb.standin_extent("equation", ["$$", "a^2"], 0), 0)

    def test_an_equation_stand_in_has_to_start_at_the_fence(self):
        # a sentinel that drifted one line up sits on prose with a fence below it;
        # reading from there would take the prose into the block and drop it
        self.assertEqual(mb.standin_extent("equation", ["a^2 + b^2", "$$", "a", "$$"], 0), 0)

    def test_an_equation_extends_over_however_many_lines_its_expression_takes(self):
        self.assertEqual(mb.standin_extent("equation", ["$$", "a", "b", "c", "$$"], 0), 5)


class KeptRegionTestCase(unittest.TestCase):
    """A synced block and a column list render no line of their own — the walker goes
    straight through to their children, whose markdown is indistinguishable from
    ordinary content. A sentinel pair brackets that content so a push leaves it alone:
    the blocks are already on the page, and re-emitting them wrote a second copy of
    every one, on every push."""

    ID = "3a000000000000000000000000000000"
    OTHER = "bb000000000000000000000000000000"

    def region(self, body, block_id=None, kind="synced_block"):
        return "{}\n{}\n<!-- notion:keep-end block={} -->".format(
            SENTINEL.format(block_id or self.ID, kind), body, block_id or self.ID)

    def test_the_content_of_a_region_is_not_re_emitted(self):
        blocks = mb.lines_blocks("before\n\n" + self.region("inside the block") + "\n\nafter")
        self.assertEqual([text_of(b) for b in blocks], ["before", "after"])

    def test_a_column_list_region_swallows_every_column(self):
        blocks = mb.lines_blocks(self.region("left\n\nright", kind="column_list"))
        self.assertEqual(blocks, [])

    def test_an_empty_region_is_allowed(self):
        # a synced block whose original has no children renders nothing between the two
        blocks = mb.lines_blocks("before\n\n{}\n<!-- notion:keep-end block={} -->".format(
            SENTINEL.format(self.ID, "synced_block"), self.ID))
        self.assertEqual([text_of(b) for b in blocks], ["before"])

    def test_regions_nest_by_id(self):
        inner = self.region("inner text", block_id=self.OTHER)
        blocks = mb.lines_blocks("before\n\n" + self.region(inner) + "\n\nafter")
        self.assertEqual([text_of(b) for b in blocks], ["before", "after"])

    def test_a_region_that_is_never_closed_refuses(self):
        # the alternative is silently swallowing the rest of the body
        with self.assertRaises(mb.Refused) as cm:
            mb.lines_blocks(SENTINEL.format(self.ID, "synced_block") + "\ninside\n\nafter")
        self.assertIn("never closed", str(cm.exception))

    def test_a_closing_marker_with_no_opener_refuses(self):
        with self.assertRaises(mb.Refused):
            mb.lines_blocks("before\n\n<!-- notion:keep-end block={} -->".format(self.ID))

    def test_a_closing_marker_for_a_different_block_does_not_close_the_region(self):
        with self.assertRaises(mb.Refused) as cm:
            mb.lines_blocks("{}\ninside\n<!-- notion:keep-end block={} -->".format(
                SENTINEL.format(self.ID, "synced_block"), self.OTHER))
        self.assertIn("never closed", str(cm.exception))


class BookmarkTestCase(unittest.TestCase):
    """A sentinelled `[url](url)` is rebuilt as the bookmark it stands for.

    Bookmark is archivable — a push deletes it and writes the new body — so the
    stand-in has to come back as a bookmark rather than be skipped, or the block
    would be silently demoted to a paragraph. The sentinel is what licenses that:
    an *unsentinelled* bare autolink is an embed, a link preview or someone's
    prose, and none of those may be rebuilt as a bookmark."""

    URL = "https://discord.com/channels/1042030674388979713/1528310843668828160"
    BOOKMARK_ID = "3a000000000000000000000000000000"

    def sentinelled(self, url=None, indent="", caption=None, mark="🔖", kind="bookmark"):
        url = url or self.URL
        return "{}\n{}- {} [{}]({})".format(SENTINEL.format(self.BOOKMARK_ID, kind),
                                            indent, mark, caption or url, url)

    def test_the_caption_round_trips_now_that_the_renderer_writes_it(self):
        [b] = mb.lines_blocks(self.sentinelled(caption="the write-up"))
        self.assertEqual(b["type"], "bookmark")
        self.assertEqual(b["bookmark"]["url"], self.URL)
        self.assertEqual("".join(r["text"]["content"] for r in b["bookmark"]["caption"]),
                         "the write-up")

    def test_an_embed_is_rebuilt_as_an_embed_not_as_a_bookmark(self):
        [b] = mb.lines_blocks(self.sentinelled(mark="🖼️", kind="embed"))
        self.assertEqual(b["type"], "embed")
        self.assertEqual(b["embed"]["url"], self.URL)

    def test_an_embed_carries_its_caption_too(self):
        """Notion's `embed` really does hold a caption — sent live on 2026-08-24 under
        `2022-06-28` and read back populated, so this is measured rather than taken
        from the block reference. Without it the rebuild would quietly drop one."""
        [b] = mb.lines_blocks(self.sentinelled(mark="🖼️", kind="embed", caption="what it is"))
        self.assertEqual("".join(r["text"]["content"] for r in b["embed"]["caption"]),
                         "what it is")

    def test_a_link_preview_is_kept_because_the_api_cannot_create_one(self):
        self.assertEqual(mb.lines_blocks(self.sentinelled(mark="👁️", kind="link_preview")), [])

    def test_a_bare_autolink_is_now_just_a_paragraph(self):
        # the renderer marks every block that is only a URL, so a bare `[url](url)`
        # can no longer be one — it is a link someone typed, and it used to refuse
        blocks = mb.lines_blocks("[{}]({})".format(self.URL, self.URL))
        self.assertEqual([b["type"] for b in blocks], ["paragraph"])

    def test_a_sentinelled_stand_in_is_rebuilt_as_a_bookmark_block(self):
        [b] = mb.lines_blocks(self.sentinelled())
        self.assertEqual(b, {"type": "bookmark",
                             "bookmark": {"url": self.URL, "caption": []}})

    def test_it_stays_where_the_stand_in_was_indented_not_where_the_sentinel_sits(self):
        # `pull.render_body` inserts the sentinel at column 0 whatever the depth of
        # the block it covers, so taking the indent from the sentinel line would lift
        # a nested bookmark out to the top level and restructure the page.
        [bullet] = mb.lines_blocks("- AI Slop Checker. Thread reference:\n"
                                   + self.sentinelled(indent="  "))
        self.assertEqual(bullet["type"], "bulleted_list_item")
        [child] = bullet["bulleted_list_item"]["children"]
        self.assertEqual(child["type"], "bookmark")
        self.assertEqual(child["bookmark"]["url"], self.URL)

    def test_it_round_trips_through_the_renderers_own_output(self):
        fx = {"blocks": [{"id": self.BOOKMARK_ID, "type": "bookmark", "has_children": False,
                          "bookmark": {"url": self.URL, "caption": []}}]}
        rendered, _walker, _api = fixture_support.render_fixture(fx)
        [back] = mb.lines_blocks(
            SENTINEL.format(self.BOOKMARK_ID, "bookmark") + "\n" + rendered.strip())
        self.assertEqual(back["type"], "bookmark")
        self.assertEqual(back["bookmark"]["url"], self.URL)

    def test_a_url_carrying_32_hex_characters_is_still_a_bookmark(self):
        # a Notion page URL ends in the page's own id, and a commit URL carries a sha.
        # The id check the sentinel branch used to sit under reads any 32 hex run in
        # the line as a block id, so it refused these — the modal bookmark in a Notion
        # workspace — as a sentinel that had drifted off its line.
        for url in ("https://www.notion.so/example-workspace/Some-Page-3a000000000000000000000000000000",
                    "https://github.com/org/repo/commit/" + "a" * 40):
            with self.subTest(url=url):
                [b] = mb.lines_blocks(self.sentinelled(url))
                self.assertEqual(b["bookmark"]["url"], url)

    def test_a_stand_in_that_is_not_a_bare_link_refuses_rather_than_being_skipped(self):
        # skipping it would be the one shape that loses a block outright: bookmark is
        # archivable, so the replace deletes it and nothing in the body puts it back
        for standin in ("![image](ATTACH:{})".format(BookmarkTestCase.BOOKMARK_ID),
                        "- 📄 **Hub** — sub-page `{}`".format(BookmarkTestCase.BOOKMARK_ID),
                        "ordinary prose"):
            with self.subTest(standin=standin):
                with self.assertRaises(mb.Refused) as cm:
                    mb.lines_blocks(SENTINEL.format(self.BOOKMARK_ID, "bookmark")
                                    + "\n" + standin)
                self.assertIn("drifted", str(cm.exception))

    def test_a_target_that_is_not_an_http_url_refuses(self):
        # `replace_body` deletes the old blocks before it appends the new ones, so a
        # payload Notion rejects empties the page and leaves it empty
        with self.assertRaises(mb.Refused) as cm:
            mb.lines_blocks(self.sentinelled("TBD"))
        self.assertIn("http", str(cm.exception))

    def test_a_sentinel_naming_another_type_over_a_bare_link_refuses(self):
        # only a bookmark, an embed and a link preview render as a bare autolink, and
        # of those only bookmark is sentinelled — so any other `type=` over this line
        # is a drifted sentinel. Skipping the pair would drop the line from the push.
        with self.assertRaises(mb.Refused) as cm:
            mb.lines_blocks(self.sentinelled().replace("type=bookmark", "type=image"))
        self.assertIn("drifted", str(cm.exception))



class ExternalMediaTestCase(unittest.TestCase):
    """`![cap](https://…)` is the one media form markdown carries whole.

    The file lives somewhere else and the line names it, so the block can be
    deleted and rebuilt from the URL alone — which is what makes it ordinary
    archivable content rather than a stand-in for something unrecreatable."""

    def caption_of(self, block):
        return "".join(r.get("text", {}).get("content", "")
                       for r in block[block["type"]].get("caption", []))

    def test_an_external_media_line_parses_as_an_external_image_block(self):
        [b] = mb.lines_blocks("![diagram](https://example.com/a.png)")
        self.assertEqual(b["type"], "image")
        self.assertEqual(b["image"]["type"], "external")
        self.assertEqual(b["image"]["external"], {"url": "https://example.com/a.png"})
        self.assertEqual(self.caption_of(b), "diagram")

    def test_the_renderers_no_caption_placeholder_names_the_type_not_a_caption(self):
        # a captionless block renders as `![{block type}](url)` (walker.py:176), so
        # that word is the block's type; carrying it back as text would print
        # "image" under every captionless image and lose every non-image type
        for word in ("image", "file", "pdf", "video", "audio"):
            with self.subTest(word=word):
                [b] = mb.lines_blocks(f"![{word}](https://example.com/a.png)")
                self.assertEqual(b["type"], word)
                self.assertEqual(b[word].get("caption"), [])

    def test_a_caption_that_merely_contains_a_type_name_is_kept(self):
        [b] = mb.lines_blocks("![image of the pipeline](https://example.com/a.png)")
        self.assertEqual(self.caption_of(b), "image of the pipeline")

    def test_it_sits_between_its_neighbours_rather_than_ending_the_body(self):
        blocks = mb.lines_blocks("before\n\n![shot](https://example.com/a.png)\n\nafter")
        self.assertEqual(types_of(blocks), ["paragraph", "image", "paragraph"])

    def test_an_indented_external_media_line_is_a_child_block(self):
        [toggle] = mb.lines_blocks("- ▸ Details\n  ![shot](https://example.com/a.png)")
        self.assertEqual(types_of(toggle["toggle"]["children"]), ["image"])

    def test_a_sentinel_over_an_external_media_line_still_refuses_as_drift(self):
        # pull never sentinels external media (it has no id to prove), so a
        # sentinel sitting on one has drifted off the line it names
        md = "{}\n![shot](https://example.com/a.png)".format(SENTINEL.format(IMAGE_ID, "image"))
        with self.assertRaises(mb.Refused) as cm:
            mb.lines_blocks(md)
        self.assertIn("drifted", str(cm.exception))

    def test_it_survives_a_prose_line_directly_above_it(self):
        # the mirror puts sibling blocks on adjacent lines with nothing between
        # them (`walker.py`), and 410 of the 561 external media lines in the
        # mirror sit directly under prose — so this is the ordinary shape, not an
        # edge. Read as paragraph continuation it would be deleted and its URL
        # smuggled into the paragraph as a link.
        blocks = mb.lines_blocks("here is the diagram\n![image](https://example.org/logo.png)")
        self.assertEqual(types_of(blocks), ["paragraph", "image"])
        self.assertEqual(text_of(blocks[0]), "here is the diagram")

    def test_an_empty_caption_is_not_media_because_the_renderer_never_writes_one(self):
        # `![](url)` cannot come out of the media branch — a block with no caption
        # renders as `![{type}](url)` — so it is literal markdown inside a
        # paragraph's text, and converting it would replace that paragraph with an
        # image block. 64 such lines exist in the mirror; all of them are prose.
        with self.assertRaises(mb.Refused) as cm:
            mb.lines_blocks("![](https://cdn.discordapp.com/avatars/1/2.png)")
        # and it must say *that*, not "downloaded into the mirror" — an operator
        # reading this is looking at prose, not at a file that went missing
        self.assertIn("empty caption", str(cm.exception))
        self.assertNotIn("downloaded", str(cm.exception))

    def test_it_round_trips_through_the_renderers_own_output(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import fixture_support
        cases = [("image", "https://example.org/logo.png", []),
                 ("video", "https://youtu.be/abc", []),
                 ("file", "https://example.org/sheet.csv",
                  [{"type": "text", "plain_text": "the export", "annotations": {}}])]
        for btype, url, caption in cases:
            with self.subTest(url=url):
                fx = {"blocks": [{"id": "8b" + "0" * 30, "type": btype, "has_children": False,
                                  btype: {"type": "external", "external": {"url": url},
                                          "caption": caption}}]}
                rendered, _walker, _api = fixture_support.render_fixture(fx)
                [back] = mb.lines_blocks(rendered.strip())
                # a captioned block loses only its type, which the line never carried
                self.assertEqual(back["type"], btype if not caption else "image")
                self.assertEqual(back[back["type"]]["external"]["url"], url)
                self.assertEqual(self.caption_of(back),
                                 "".join(c["plain_text"] for c in caption))


class RefusalTestCase(unittest.TestCase):
    def refusal(self, md):
        with self.assertRaises(mb.Refused) as cm:
            mb.lines_blocks(md)
        return cm.exception

    def test_the_refusal_names_the_line_and_its_number(self):
        e = self.refusal("one\n\n![shot](3b4fcfd1-1_shot.png)")
        self.assertEqual(e.line_no, 3)
        self.assertIn("![shot](3b4fcfd1-1_shot.png)", str(e))

    def test_an_inaccessible_children_diagnostic_refuses(self):
        self.refusal("<!-- child blocks inaccessible here: HTTP 404 -->")

    def test_an_unhandled_block_type_diagnostic_refuses(self):
        self.refusal("<!-- unhandled block type: ai_block -->")

    def test_an_unrecognized_html_comment_refuses(self):
        self.refusal("<!-- something nobody planned for -->")

    def test_downloaded_media_refuses_because_it_carries_no_block_id(self):
        self.refusal("![image](3b4fcfd1-1_image.png)")

    def test_downloaded_media_still_refuses_directly_under_prose(self):
        # the refusal is only reached if the line is a line: read as paragraph
        # continuation it would be folded into the prose above it instead
        e = self.refusal("here is the diagram\n![image](3b4fcfd1-1_image.png)")
        self.assertEqual(e.line_no, 2)

    def test_uncovered_notion_hosted_media_refuses(self):
        self.refusal("![image](ATTACH:{})".format(IMAGE_ID))

    def test_an_id_less_sub_page_stand_in_refuses(self):
        self.refusal("- 📄 AI Manipulation hackathon Info")

    def test_an_id_less_database_stand_in_refuses(self):
        self.refusal("- 🗄️ Some inline database")

    def test_an_uncovered_sub_page_stand_in_refuses(self):
        self.refusal("- 📄 **Hub** — sub-page `24000000000000000000000000000000`")

    def test_an_uncovered_link_to_page_stand_in_refuses(self):
        self.refusal("- 🔗 link to `24000000000000000000000000000000`")

    def test_a_stand_in_for_an_untitled_page_refuses(self):
        # an untitled sub-page renders as `- 📄 ` and the trailing space is
        # stripped before matching, so a pattern requiring it lets the line
        # through as a bullet holding a lone emoji
        for line in ("- 📄", "- 🗄️", "- 🔗"):
            with self.subTest(line=line):
                self.refusal(line)

    def test_a_table_of_only_a_separator_row_refuses(self):
        self.refusal("| --- | --- |")

    def test_a_quote_carrying_a_child_block_rebuilds_it_as_a_child(self):
        # a quote is a region: its text, then its children — the shape that
        # used to refuse because the flattened rendering could not carry it
        [b] = mb.lines_blocks("> the quote\n> - a child bullet")
        self.assertEqual(b["type"], "quote")
        self.assertEqual(text_of(b), "the quote")
        [kid] = b["quote"]["children"]
        self.assertEqual(kid["type"], "bulleted_list_item")
        self.assertEqual(text_of(kid), "a child bullet")

    def test_a_bare_marker_is_an_empty_quote(self):
        [b] = mb.lines_blocks(">")
        self.assertEqual(b["type"], "quote")
        self.assertEqual(text_of(b), "")

    def test_a_details_tag_with_attributes_refuses(self):
        self.refusal("<details open>\n\ninside\n\n</details>")

    def test_an_unclosed_details_refuses(self):
        self.refusal("<details><summary>S</summary>\n\ninside\n\nmore text")

    def test_a_url_block_stand_in_with_no_sentinel_refuses(self):
        """The marker says a real block is there, and only a sentinel proves it. A line
        someone typed that happens to wear one would otherwise be rebuilt as a block, or
        — for a link preview, which is kept — skipped out of the push entirely."""
        url = "https://www.linkedin.com/in/someone/"
        for mark in ("🔖", "🖼️", "👁️"):
            with self.subTest(mark=mark):
                e = self.refusal("- {} [{}]({})".format(mark, url, url))
                self.assertIn("no keep-sentinel", str(e))

    def test_an_ordinary_link_in_prose_is_not_a_bookmark(self):
        blocks = mb.lines_blocks("see [the docs](https://example.com/docs) for more")
        self.assertEqual(types_of(blocks), ["paragraph"])


class NestingTestCase(unittest.TestCase):
    def test_an_indented_bullet_becomes_a_child_of_the_one_above(self):
        blocks = mb.lines_blocks("- parent\n  - child")
        self.assertEqual(len(blocks), 1)
        kids = blocks[0]["bulleted_list_item"]["children"]
        self.assertEqual(text_of(kids[0]), "child")

    def test_a_toggles_indented_content_stays_inside_it(self):
        # the mirror renders a toggle's children indented under it; flattening
        # them publishes content the page had collapsed
        blocks = mb.lines_blocks("- ▸ Details\n  ## What it is\n  An internal agent.")
        self.assertEqual(types_of(blocks), ["toggle"])
        self.assertEqual(types_of(blocks[0]["toggle"]["children"]), ["heading_2", "paragraph"])

    def test_two_levels_of_nesting_are_accepted(self):
        blocks = mb.lines_blocks("- a\n  - b\n    - c")
        child = blocks[0]["bulleted_list_item"]["children"][0]
        self.assertEqual(text_of(child["bulleted_list_item"]["children"][0]), "c")

    def test_a_third_level_refuses_rather_than_flattening(self):
        with self.assertRaises(mb.Refused) as cm:
            mb.lines_blocks("- a\n  - b\n    - c\n      - d")
        self.assertEqual(cm.exception.line_no, 4)

    def test_depth_is_counted_at_the_root_too(self):
        # a nested `<details>` arrives as a toggle that already carries its own
        # children, so a depth check that only runs on nested blocks never sees
        # it — and the payload reaches Notion, which rejects the whole request
        # after the pusher has already deleted the page body
        md = ("<details><summary>Outer</summary>\n\n"
              "<details><summary>Inner</summary>\n\n"
              "- a\n  - b\n    - c\n\n"
              "</details>\n\n</details>")
        with self.assertRaises(mb.Refused) as cm:
            mb.md_blocks(md)
        self.assertIn("deeper than", cm.exception.reason)

    def test_a_details_layer_may_use_both_levels(self):
        vis, det = mb.md_blocks("<details><summary>D</summary>\n\n- a\n  - b\n    - c\n\n</details>")
        self.assertEqual(det[0]["bulleted_list_item"]["children"][0]
                         ["bulleted_list_item"]["children"][0]["type"], "bulleted_list_item")

    def test_indented_prose_under_a_bullet_is_a_child_block(self):
        # the mirror writes a list item as one line and indents its children, so
        # an indented line under a bullet is a child paragraph, not wrapped text.
        # Measured over the live row bodies: 119 such lines in 44 rows, each a
        # block that joining would fold into its parent's text.
        [b] = mb.lines_blocks("- a claim\n  its own paragraph underneath")
        self.assertEqual(text_of(b), "a claim")
        [child] = b["bulleted_list_item"]["children"]
        self.assertEqual(text_of(child), "its own paragraph underneath")

    def test_a_wrapped_paragraph_at_one_level_stays_one_paragraph(self):
        # GFM soft wrap: consecutive lines at the same indent are one paragraph
        [b] = mb.lines_blocks("a claim that runs on\nand finishes here")
        self.assertEqual(text_of(b), "a claim that runs on and finishes here")

    def test_a_shift_enter_line_break_survives_as_a_newline(self):
        # refresh.py marks an in-paragraph break with two trailing spaces
        [b] = mb.lines_blocks("first half  \nsecond half")
        self.assertEqual(text_of(b), "first half\nsecond half")

    def test_indentation_inside_a_code_fence_is_content_not_nesting(self):
        [b] = mb.lines_blocks("  ```python\n  name = (\n      word.capitalize()\n  )\n  ```")
        self.assertEqual(b["type"], "code")
        self.assertEqual(text_of(b), "name = (\n    word.capitalize()\n)")

    def test_max_nesting_none_builds_the_whole_tree(self):
        # the write path splits a deep tree across requests, so it asks for the
        # tree the markdown actually describes rather than the part one request
        # can carry
        [b] = mb.lines_blocks("- a\n  - b\n    - c\n      - d", max_nesting=None)
        depth = 0
        node = b
        while (node[node["type"]].get("children") or []):
            node = node[node["type"]]["children"][0]
            depth += 1
        self.assertEqual(depth, 3)
        self.assertEqual(text_of(node), "d")

    def test_nesting_depth_counts_levels_of_children(self):
        [b] = mb.lines_blocks("- a\n  - b\n    - c", max_nesting=None)
        self.assertEqual(mb.nesting_depth(b), 2)


class DetailsTestCase(unittest.TestCase):
    def test_the_visible_layer_and_the_details_layer_split(self):
        vis, det = mb.md_blocks("intro\n\n<details><summary>Details</summary>\n\ninside\n\n</details>")
        self.assertEqual(text_of(vis[0]), "intro")
        self.assertEqual(text_of(det[0]), "inside")

    def test_a_document_without_details_has_no_second_layer(self):
        vis, det = mb.md_blocks("intro")
        self.assertIsNone(det)

    def test_content_after_the_closing_tag_stays_visible(self):
        vis, det = mb.md_blocks("<details><summary>D</summary>\n\ninside\n\n</details>\n\ntail")
        self.assertEqual([text_of(b) for b in vis], ["tail"])

    def test_a_nested_details_becomes_a_nested_toggle(self):
        md = ("<details><summary>Outer</summary>\n\n"
              "before\n\n"
              "<details><summary>Inner</summary>\n\n"
              "the restored prompt\n\n"
              "</details>\n\n"
              "after\n\n</details>")
        _, det = mb.md_blocks(md)
        self.assertEqual(types_of(det), ["paragraph", "toggle", "paragraph"])
        toggle = det[1]
        self.assertEqual(text_of(toggle), "Inner")
        self.assertEqual(text_of(toggle["toggle"]["children"][0]), "the restored prompt")


class ChunkTestCase(unittest.TestCase):
    def test_blocks_are_split_into_batches_notion_accepts(self):
        blocks = [{"type": "paragraph", "paragraph": {"rich_text": []}} for _ in range(200)]
        batches = mb.chunks(blocks)
        self.assertEqual([len(b) for b in batches], [90, 90, 20])
        self.assertEqual(sum(batches, []), blocks)


class RendererRoundTripTestCase(unittest.TestCase):
    """The other half of the round trip, against the renderer itself.

    Every other test here quotes a string I read out of refresh.py by hand. These
    run the renderer's own golden fixtures through it and then through this
    converter, so a renderer change that this parser stops recognising fails
    here rather than on a live page."""

    # types the converter is expected to read back; the rest are the refusal
    # classes and the blocks that render as nothing
    REPRODUCIBLE = {"paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item",
                    "numbered_list_item", "to_do", "toggle", "quote", "callout", "code",
                    "divider", "equation", "table"}
    # fixtures whose rendering cannot be converted at all, and why
    REFUSED = {
        "child_page": "sub-page stand-in", "child_database": "database stand-in",
        "link_to_page": "page-link stand-in",
        # its bookmark, embed and link_preview all render as bare autolinks, which
        # refuse without a sentinel; its pdf is Notion-hosted
        "media": "hosted media, and url-only stand-ins carrying no sentinel",
        "unknown": "the mirror's unhandled-block-type diagnostic",
        # both render a marked stand-in now rather than italic prose, so an
        # unsentinelled one refuses instead of coming back as a paragraph
        "breadcrumb": "a table-of-contents stand-in carrying no sentinel",
        "template": "a template stand-in carrying no sentinel",
    }
    # measured losses in the round trip, each with the reason it cannot be helped here
    LOSSES = {
        ("paragraph", "paragraph"):
            "an empty paragraph renders as an empty line; a spacer block has nothing to rebuild from",
    }

    def setUp(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import fixture_support
        self.fx = fixture_support

    def rendered(self, name):
        got, _walker, _api = self.fx.render_fixture(self.fx.load_fixture(name))
        return got

    def test_the_expectation_table_covers_every_fixture(self):
        # a new renderer fixture must force a decision here, not pass unnoticed
        named = set(self.REFUSED) | {n for n, _ in self.LOSSES}
        for name in self.fx.fixture_names():
            self.assertTrue(name in named or self.fx.fixture_covers(self.fx.load_fixture(name)),
                            f"{name} has no payload types to check")

    def test_every_reproducible_type_survives_the_round_trip(self):
        for name in self.fx.fixture_names():
            if name in self.REFUSED:
                continue
            with self.subTest(fixture=name):
                covers = self.fx.fixture_covers(self.fx.load_fixture(name)) & self.REPRODUCIBLE
                got = set(flatten_types(mb.lines_blocks(self.rendered(name))))
                missing = {t for t in covers - got if (name, t) not in self.LOSSES}
                self.assertEqual(missing, set())

    def test_the_refusing_fixtures_refuse_by_name(self):
        for name, what in self.REFUSED.items():
            with self.subTest(fixture=name):
                with self.assertRaises(mb.Refused, msg=what):
                    mb.lines_blocks(self.rendered(name))

    def test_a_rendered_paragraph_keeps_its_line_breaks(self):
        blocks = mb.lines_blocks(self.rendered("paragraph_rich"))
        # one paragraph per block, since the renderer separates siblings now; the
        # shift+enter breaks have to stay inside the one that carries them
        self.assertIn("first line\nsecond line\nthird", [text_of(b) for b in blocks])


def flatten_types(blocks):
    for b in blocks:
        yield b["type"]
        kids = b[b["type"]].get("children") if b["type"] != "table" else None
        if kids:
            yield from flatten_types(kids)


# `PusherCreateTestCase` lived here until 2026-08-13: it loaded
# `notes/infra-task-triage/proposals/push.py` and pinned that pusher's create path,
# the one call site that sent `children[:100]` instead of chunking. That pusher was
# retired with the triage corpus, and the behaviour it guarded is covered without
# it — `ChunkTestCase` above for the splitter, and tasksync's own
# `test_a_long_body_is_chunked` for the surviving write path.

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixture_support  # noqa: E402


def annotated(block):
    """[(text, sorted annotation names, link)] for one block's rich text."""
    out = []
    for run in block[block["type"]].get("rich_text", []):
        ann = sorted(k for k, v in (run.get("annotations") or {}).items() if v)
        link = ((run.get("text") or {}).get("link") or {}).get("url")
        out.append((run["text"]["content"], ann, link))
    return out


class EmphasisTestCase(unittest.TestCase):
    """`refresh.rich_md` emits one marker per annotation; each must parse back."""

    def test_a_single_asterisk_pair_is_italic_not_literal_text(self):
        [block] = mb.lines_blocks("(1) *Topic search* while the window is open")
        self.assertEqual(annotated(block),
                         [("(1) ", [], None), ("Topic search", ["italic"], None),
                          (" while the window is open", [], None)])

    def test_a_tilde_pair_is_strikethrough(self):
        [block] = mb.lines_blocks("dropped ~~entirely~~ here")
        self.assertIn(("entirely", ["strikethrough"], None), annotated(block))

    def test_the_renderers_underline_tag_is_an_underline(self):
        [block] = mb.lines_blocks("read <u>this</u> first")
        self.assertIn(("this", ["underline"], None), annotated(block))

    def test_a_triple_asterisk_run_is_bold_and_italic(self):
        [block] = mb.lines_blocks("***both at once***")
        self.assertEqual(annotated(block), [("both at once", ["bold", "italic"], None)])

    def test_emphasis_nests_around_code_and_links(self):
        [block] = mb.lines_blocks("*see `code` and [docs](https://x.test)*")
        self.assertEqual(annotated(block),
                         [("see ", ["italic"], None), ("code", ["code", "italic"], None),
                          (" and ", ["italic"], None),
                          ("docs", ["italic"], "https://x.test")])

    def test_every_annotation_the_renderer_writes_survives_the_round_trip(self):
        for ann in ("bold", "italic", "strikethrough", "underline", "code"):
            with self.subTest(annotation=ann):
                fx = {"blocks": [{"id": "a" * 32, "type": "paragraph", "has_children": False,
                                  "paragraph": {"rich_text": [
                                      {"type": "text", "plain_text": "kept",
                                       "annotations": {ann: True}}]}}]}
                rendered, _w, _a = fixture_support.render_fixture(fx)
                [block] = mb.lines_blocks(rendered.strip())
                self.assertEqual(annotated(block), [("kept", [ann], None)])


class LiteralMarkerTestCase(unittest.TestCase):
    """Plain text that merely *looks* like markdown must survive both directions."""

    LITERALS = ["a *star* pair", "a **double** pair", "back`tick`s", "a [link](x) shape",
                "a ~~tilde~~ pair", "an <u>angle</u> tag", "a \\ backslash",
                "an escaped \\* star", "C:\\temp\\data", "regex \\d+ digits"]

    def rendered(self, plain_text):
        fx = {"blocks": [{"id": "b" * 32, "type": "paragraph", "has_children": False,
                          "paragraph": {"rich_text": [
                              {"type": "text", "plain_text": plain_text,
                               "annotations": {}}]}}]}
        return fixture_support.render_fixture(fx)[0].rstrip("\n")

    def test_literal_markers_come_back_as_the_same_plain_text(self):
        for plain_text in self.LITERALS:
            with self.subTest(text=plain_text):
                [block] = mb.lines_blocks(self.rendered(plain_text))
                self.assertEqual(annotated(block), [(plain_text, [], None)])

    def test_a_code_span_is_not_escaped_because_markdown_does_not_unescape_it(self):
        """CommonMark gives a backslash no meaning inside a code span, so escaping
        one there is a backslash a reader sees — `x-dashboard-\\*` on the page."""
        fx = {"blocks": [{"id": "c" * 32, "type": "paragraph", "has_children": False,
                          "paragraph": {"rich_text": [
                              {"type": "text", "plain_text": "x-dashboard-*",
                               "annotations": {"code": True}}]}}]}
        rendered = fixture_support.render_fixture(fx)[0].rstrip("\n")
        self.assertEqual(rendered, "`x-dashboard-*`")
        [block] = mb.lines_blocks(rendered)
        self.assertEqual(annotated(block), [("x-dashboard-*", ["code"], None)])

    def test_code_span_boundary_whitespace_moves_out_of_the_annotation(self):
        """Notion stores `` `[AI] ` `` as code("[AI]") plus a plain space — boundary
        whitespace leaves the code annotation (observed live 2026-08-16). Parsing
        must produce the runs Notion will hold, or a correct push reads back as a
        mismatch. The hoisted space merges with the neighbouring plain run, because
        that is the run shape Notion returns."""
        [block] = mb.lines_blocks("x writes `[AI] ` properties")
        self.assertEqual(annotated(block), [("x writes ", [], None),
                                            ("[AI]", ["code"], None),
                                            ("  properties", [], None)])
        [block] = mb.lines_blocks("x writes ` [AI]` properties")
        self.assertEqual(annotated(block), [("x writes  ", [], None),
                                            ("[AI]", ["code"], None),
                                            (" properties", [], None)])

    def test_a_whitespace_only_code_span_becomes_plain_whitespace(self):
        [block] = mb.lines_blocks("a ` ` b")
        self.assertEqual(annotated(block), [("a   b", [], None)])

    def test_interior_code_whitespace_is_content(self):
        [block] = mb.lines_blocks("`a b`")
        self.assertEqual(annotated(block), [("a b", ["code"], None)])

    def test_a_backslash_inside_a_code_span_is_content(self):
        fx = {"blocks": [{"id": "c" * 32, "type": "paragraph", "has_children": False,
                          "paragraph": {"rich_text": [
                              {"type": "text", "plain_text": "\\d+ and *this*",
                               "annotations": {"code": True}}]}}]}
        rendered = fixture_support.render_fixture(fx)[0].rstrip("\n")
        [block] = mb.lines_blocks(rendered)
        self.assertEqual(annotated(block), [("\\d+ and *this*", ["code"], None)])

    def test_a_link_label_holding_a_bracket_does_not_end_early(self):
        """The label runs to the first unescaped `]`, so one in the text would leave
        the rest of the run as loose prose and lose the link."""
        fx = {"blocks": [{"id": "j" * 32, "type": "paragraph", "has_children": False,
                          "paragraph": {"rich_text": [
                              {"type": "text", "plain_text": "rows[0] and more",
                               "annotations": {"italic": True},
                               "href": "https://x.test/p"}]}}]}
        rendered = fixture_support.render_fixture(fx)[0].rstrip("\n")
        [block] = mb.lines_blocks(rendered)
        self.assertEqual(annotated(block),
                         [("rows[0] and more", ["italic"], "https://x.test/p")])

    def test_a_linked_code_span_keeps_a_bracket_in_its_content(self):
        """The link label escapes `]` so it cannot end early — but a code span is
        literal, so that backslash would become content the reader sees."""
        fx = {"blocks": [{"id": "h" * 32, "type": "paragraph", "has_children": False,
                          "paragraph": {"rich_text": [
                              {"type": "text", "plain_text": "rows[0]",
                               "annotations": {"code": True},
                               "href": "https://x.test/p"}]}}]}
        rendered = fixture_support.render_fixture(fx)[0].rstrip("\n")
        [block] = mb.lines_blocks(rendered)
        self.assertEqual(annotated(block), [("rows[0]", ["code"], "https://x.test/p")])

    def test_a_backslash_before_a_bracket_survives_a_paragraph_that_has_a_link(self):
        """The bracket rule must run in the same pass as the rest of the escaping.
        A second pass cannot tell the escape it just wrote for a literal backslash
        from one written for the bracket, so it leaves the bracket bare and the
        link in the next run swallows it."""
        fx = {"blocks": [{"id": "i" * 32, "type": "paragraph", "has_children": False,
                          "paragraph": {"rich_text": [
                              {"type": "text", "plain_text": "see \\[ and ",
                               "annotations": {}},
                              {"type": "text", "plain_text": "the doc",
                               "annotations": {}, "href": "https://x.test/p"}]}}]}
        rendered = fixture_support.render_fixture(fx)[0].rstrip("\n")
        [block] = mb.lines_blocks(rendered)
        self.assertEqual(annotated(block),
                         [("see \\[ and ", [], None),
                          ("the doc", [], "https://x.test/p")])

    def test_brackets_are_escaped_only_ahead_of_the_run_that_carries_the_link(self):
        """The rule exists for a `[` whose `](` is in a *later* run. Firing it on
        every run of the paragraph escapes brackets that were never at risk."""
        fx = {"blocks": [{"id": "g" * 32, "type": "paragraph", "has_children": False,
                          "paragraph": {"rich_text": [
                              {"type": "text", "plain_text": "the doc",
                               "annotations": {}, "href": "https://x.test/p"},
                              {"type": "text", "plain_text": " covers [most] of it",
                               "annotations": {}}]}}]}
        rendered = fixture_support.render_fixture(fx)[0].rstrip("\n")
        self.assertEqual(rendered, "[the doc](https://x.test/p) covers [most] of it")

    def test_a_linked_annotated_run_with_spaces_around_it_is_stable(self):
        """The markers sit inside the label, so the spaces would too — and a label
        parsed into three runs re-renders as three links, which reads to push as
        *the body does not match task.md* on a body nobody touched."""
        # mid-line, so the comparison is about the label rather than about the
        # line-level strip every markdown line gets
        fx = {"blocks": [{"id": "d" * 32, "type": "paragraph", "has_children": False,
                          "paragraph": {"rich_text": [
                              {"type": "text", "plain_text": "due", "annotations": {}},
                              {"type": "text", "plain_text": " deadline ",
                               "annotations": {"bold": True},
                               "href": "https://x.test/p"},
                              {"type": "text", "plain_text": "today", "annotations": {}}]}}]}
        once = fixture_support.render_fixture(fx)[0].rstrip("\n")
        self.assertEqual(once, fixture_support.render_blocks(mb.lines_blocks(once)))

    def test_an_unpaired_marker_in_prose_stays_a_character(self):
        """No flanking rule meant two unrelated asterisks paired up and the
        characters were *deleted* from what the push wrote. Every one of these is
        prose somebody typed, not emphasis."""
        for line in ["2 * 3 * 4 = 24",
                     "the glob *.csv and *.json files",
                     "footnote * and another *",
                     "a ~ b ~ c",
                     "spaced ** double ** markers"]:
            with self.subTest(line=line):
                blocks = mb.lines_blocks(line)
                self.assertEqual(text_of(blocks[0]), line)
                self.assertEqual(annotated(blocks[0]), [(line, [], None)])

    def test_emphasis_still_parses_when_it_hugs_its_content(self):
        """The flanking rule must not cost the shapes the renderer actually writes."""
        for line, want in [("an *italic* word", "italic"),
                           ("an **emphatic** word", "emphatic"),
                           ("a ~~struck~~ word", "struck")]:
            with self.subTest(line=line):
                [block] = mb.lines_blocks(line)
                self.assertIn(want, [run[0] for run in annotated(block)])

    def test_a_bracket_in_one_run_does_not_swallow_a_link_in_the_next(self):
        """The `](` that pairs with it lives in a different run, so the per-run rule
        cannot see it and the two read back as one long link."""
        fx = {"blocks": [{"id": "f" * 32, "type": "paragraph", "has_children": False,
                          "paragraph": {"rich_text": [
                              {"type": "text", "plain_text": "see [ and ",
                               "annotations": {}},
                              {"type": "text", "plain_text": "the doc",
                               "annotations": {}, "href": "https://x.test/p"}]}}]}
        rendered = fixture_support.render_fixture(fx)[0].rstrip("\n")
        [block] = mb.lines_blocks(rendered)
        self.assertEqual(annotated(block),
                         [("see [ and ", [], None),
                          ("the doc", [], "https://x.test/p")])

    def test_a_run_ending_in_a_backslash_still_closes_its_marker(self):
        """The core is trimmed after escaping, so a backslash that was harmless mid-run
        can end up against the closing marker and escape it."""
        fx = {"blocks": [{"id": "e" * 32, "type": "paragraph", "has_children": False,
                          "paragraph": {"rich_text": [
                              {"type": "text", "plain_text": "path C:\\ ",
                               "annotations": {"bold": True}}]}}]}
        rendered = fixture_support.render_fixture(fx)[0].rstrip("\n")
        [block] = mb.lines_blocks(rendered)
        self.assertEqual(annotated(block), [("path C:\\", ["bold"], None)])

    def test_every_marker_only_string_up_to_four_characters_round_trips(self):
        """Exhaustive over the alphabet that can break the parser at all: any plain
        text made of nothing but markers has to come back as itself."""
        import itertools
        losses = []
        for size in (1, 2, 3, 4):
            for chars in itertools.product("*`~\\[]<u", repeat=size):
                text = "".join(chars)
                rendered = self.rendered(text)
                try:
                    blocks = mb.lines_blocks(rendered)
                except mb.Refused:
                    continue        # a refusal is a loud answer, not a silent loss
                if len(blocks) != 1 or blocks[0]["type"] != "paragraph":
                    continue        # the line became another block; not this test's subject
                if text_of(blocks[0]) != text:
                    losses.append((text, rendered, text_of(blocks[0])))
        self.assertEqual(losses[:5], [])

    def test_the_markdown_spelling_is_stable_across_a_second_round_trip(self):
        """render∘parse is the identity: a body already in the renderer's spelling
        must push and pull back byte-identical, or every push wedges on drift."""
        for plain_text in self.LITERALS:
            with self.subTest(text=plain_text):
                once = self.rendered(plain_text)
                self.assertEqual(once, fixture_support.render_blocks(mb.lines_blocks(once)))


class HandTypedMarkdownTestCase(unittest.TestCase):
    """Markdown a person typed, which is what every `task.md` body starts as.

    Two properties, and both blockers of the 2026-08-12 review were a breach of one
    of them: the characters a line spends on prose survive the parse, and the
    canonical spelling the renderer gives back is a fixed point — a spelling that
    keeps moving is a row whose push refuses forever against its own base.
    """

    #: (what a person typed, the plain text Notion must end up holding)
    LINES = [
        ("2 * 3 * 4 = 24", "2 * 3 * 4 = 24"),
        ("the glob *.csv and *.json files", "the glob *.csv and *.json files"),
        ("an *italic* word and a **bold** one", "an italic word and a bold one"),
        ("***both at once*** in one span", "both at once in one span"),
        ("see `duplicate_checker.py` for the check", "see duplicate_checker.py for the check"),
        ("a [link](https://x.test/p) mid-sentence", "a link mid-sentence"),
        ("the five `x-dashboard-*` Lambdas", "the five x-dashboard-* Lambdas"),
        ("C:\\temp\\data and the regex \\d+", "C:\\temp\\data and the regex \\d+"),
        ("quoting Sam: *\"look at all the 'slug\\*' fields\"* — verbatim",
         "quoting Sam: \"look at all the 'slug*' fields\" — verbatim"),
        ("a ~~struck~~ phrase and a ~ tilde", "a struck phrase and a ~ tilde"),
        ("brackets [like this] and a [real](https://x.test/p) one",
         "brackets [like this] and a real one"),
    ]

    def canonical(self, md):
        return fixture_support.render_blocks(mb.lines_blocks(md))

    def test_prose_characters_are_not_spent_as_markers(self):
        """The blocker: an unpaired `*` was read as emphasis and *deleted* from the
        text the push wrote. `slug\\*` is the live corpus victim, escaped as the
        canonical spelling would have it."""
        for line, want in self.LINES:
            with self.subTest(line=line):
                self.assertEqual(text_of(mb.lines_blocks(line)[0]), want)

    def test_the_canonical_spelling_is_a_fixed_point(self):
        for line, _want in self.LINES:
            with self.subTest(line=line):
                once = self.canonical(line)
                self.assertEqual(once, self.canonical(once))

    def test_the_canonical_spelling_keeps_the_same_plain_text(self):
        for line, want in self.LINES:
            with self.subTest(line=line):
                self.assertEqual(text_of(mb.lines_blocks(self.canonical(line))[0]), want)


class SiblingBlockTestCase(unittest.TestCase):
    """Two blocks must not come back as one: the merge is invisible until someone
    reads the page, and the pusher deletes the body before recreating it."""

    def test_two_sibling_paragraphs_stay_two_blocks(self):
        blocks = [{"type": "paragraph", "paragraph": {"rich_text": [
            {"type": "text", "text": {"content": f"paragraph {n}"}}]}} for n in (1, 2)]
        back = mb.lines_blocks(fixture_support.render_blocks(blocks))
        self.assertEqual(types_of(back), ["paragraph", "paragraph"])
        self.assertEqual([text_of(b) for b in back], ["paragraph 1", "paragraph 2"])

    def test_a_toggles_paragraph_children_stay_separate(self):
        kids = [{"type": "paragraph", "paragraph": {"rich_text": [
            {"type": "text", "text": {"content": f"child {n}"}}]}} for n in (1, 2, 3)]
        toggle = [{"type": "toggle", "toggle": {
            "rich_text": [{"type": "text", "text": {"content": "Details"}}],
            "children": kids}}]
        [back] = mb.lines_blocks(fixture_support.render_blocks(toggle))
        self.assertEqual(back["type"], "toggle")
        self.assertEqual([text_of(k) for k in back["toggle"]["children"]],
                         ["child 1", "child 2", "child 3"])

    def test_a_cell_holding_a_pipe_stays_one_cell(self):
        """`md_cell` escapes a pipe as `\\|`; splitting on it anyway grows the table
        by a column and cuts the cell in half."""
        row = {"type": "table_row", "table_row": {"cells": [
            [{"type": "text", "text": {"content": "a|b"}}],
            [{"type": "text", "text": {"content": "c"}}]]}}
        table = [{"type": "table", "table": {"table_width": 2, "has_column_header": True,
                                             "has_row_header": False, "children": [row]}}]
        [back] = mb.lines_blocks(fixture_support.render_blocks(table))
        self.assertEqual(back["table"]["table_width"], 2)
        self.assertEqual([[r["text"]["content"] for r in cell]
                          for cell in back["table"]["children"][0]["table_row"]["cells"]],
                         [["a|b"], ["c"]])

    def kids(self, n=2):
        return {"c" * 32: [
            {"id": f"{i}d" * 16, "type": "paragraph", "has_children": False,
             "paragraph": {"rich_text": [
                 {"type": "text", "plain_text": f"child {i}", "annotations": {}}]}}
            for i in (1, 2, 3)[:n]]}

    def test_a_quote_with_two_children_keeps_them_as_children(self):
        """A bare `>` inside the region is a blank line, not an empty quote:
        the seam between the quote's text and its children, and between
        sibling child paragraphs."""
        fx = {"blocks": [{"id": "c" * 32, "type": "quote", "has_children": True,
                          "quote": {"rich_text": [
                              {"type": "text", "plain_text": "Quoted head",
                               "annotations": {}}]}}],
              "children": self.kids()}
        rendered = fixture_support.render_fixture(fx)[0].rstrip("\n")
        [b] = mb.lines_blocks(rendered)
        self.assertEqual(b["type"], "quote")
        self.assertEqual(text_of(b), "Quoted head")
        self.assertEqual([text_of(k) for k in b["quote"]["children"]],
                         ["child 1", "child 2"])

    def test_a_callout_with_two_children_keeps_them_as_children(self):
        fx = {"blocks": [{"id": "c" * 32, "type": "callout", "has_children": True,
                          "callout": {"icon": {"type": "emoji", "emoji": "💡"},
                                      "rich_text": [
                                          {"type": "text", "plain_text": "Callout head",
                                           "annotations": {}}]}}],
              "children": self.kids()}
        rendered = fixture_support.render_fixture(fx)[0].rstrip("\n")
        [b] = mb.lines_blocks(rendered)
        self.assertEqual(b["type"], "callout")
        self.assertEqual(b["callout"]["icon"]["emoji"], "💡")
        self.assertEqual([text_of(k) for k in b["callout"]["children"]],
                         ["child 1", "child 2"])

    def test_a_line_that_is_not_a_block_marker_does_not_swallow_the_next_paragraph(self):
        """A code block's caption renders as a line `_flat` reads as a paragraph, so the
        block after it was absorbed — losing the caption *and* the paragraph.

        A table of contents and a template used to be in here for the same reason. They
        are not any more: both carry a `- ` marker now, which ends the run of paragraphs
        by itself, and an unsentinelled one refuses rather than parsing at all."""
        cases = {
            "code caption": {
                "id": "c" * 32, "type": "code", "has_children": False,
                "code": {"language": "python",
                         "rich_text": [{"type": "text", "plain_text": "x = 1"}],
                         "caption": [{"type": "text", "plain_text": "the snippet",
                                      "annotations": {}}]}},
        }
        for name, block in cases.items():
            with self.subTest(block=name):
                fx = {"blocks": [block,
                                 {"id": "d" * 32, "type": "paragraph", "has_children": False,
                                  "paragraph": {"rich_text": [
                                      {"type": "text", "plain_text": "the paragraph after",
                                       "annotations": {}}]}}]}
                rendered = fixture_support.render_fixture(fx)[0].rstrip("\n")
                self.assertIn("the paragraph after",
                              [text_of(b) for b in mb.lines_blocks(rendered)])

    def test_a_paragraph_before_a_container_that_renders_nothing_stays_separate(self):
        """A synced block or a column emits no line of its own, so it cannot end the
        run of paragraphs — its first child would otherwise merge into the one above."""
        for container in ("synced_block", "column_list"):
            with self.subTest(container=container):
                fx = {"blocks": [
                    {"id": "a" * 32, "type": "paragraph", "has_children": False,
                     "paragraph": {"rich_text": [
                         {"type": "text", "plain_text": "top paragraph",
                          "annotations": {}}]}},
                    {"id": "c" * 32, "type": container, "has_children": True,
                     container: {}}],
                    "children": self.kids(1)}
                rendered = fixture_support.render_fixture(fx)[0].rstrip("\n")
                self.assertEqual([text_of(b) for b in mb.lines_blocks(rendered)],
                                 ["top paragraph", "child 1"])

    def test_a_wrapped_paragraph_an_author_typed_is_still_one_block(self):
        """The separator is the renderer's; hand-written soft wrapping keeps working."""
        blocks = mb.lines_blocks("one paragraph\nwrapped by hand")
        self.assertEqual(types_of(blocks), ["paragraph"])


class SpellingsOfTheSameBlocksTestCase(unittest.TestCase):
    """The property tasksync's push-verification rests on: for the spellings the
    renderer normalises, this converter is a *fixed point* — the markdown a human
    types and the markdown Notion hands back for the same blocks convert to the same
    tree, so a body that differs only in spelling is a body Notion already holds
    correctly.

    Byte-comparing those two spellings instead is what wedged a live row for three
    hours on 2026-08-14: a correct push reported "the body does not match task.md",
    the merge base was never rebased, and the next pull wrote whole-body conflict
    markers over a document nobody had edited twice. The pairs below are the measured
    inventory; each is a way markdown can say a thing that Notion does not store.
    """

    #: ``(label, as a human types it, as the mirror renders it back)``.
    PAIRS = [
        ("a blank line under a heading",
         "## Heading\n\nA paragraph under it.", "## Heading\nA paragraph under it."),
        ("a blank line before a list", "Intro.\n\n- one\n- two", "Intro.\n- one\n- two"),
        ("a blank line between a list and a heading", "- one\n\n## Next", "- one\n## Next"),
        ("a blank line between two headings", "## A\n\n## B", "## A\n## B"),
        ("blank lines around a divider", "text\n\n---\n\ntext2", "text\n---\ntext2"),
        ("a blank line before a nested item", "- one\n\n  - nested", "- one\n  - nested"),
        ("a run of blank lines", "Para.\n\n\n\nOther para.", "Para.\n\nOther para."),
        ("ordered-list numbering",
         "1. first\n2. second\n3. third", "1. first\n1. second\n1. third"),
        ("a details block",
         "<details><summary>D</summary>\n\nInner.\n\n</details>", "- ▸ D\n  Inner."),
        ("an unpaired ~~", "Due ~~Aug 17 soon.", "Due \\~~Aug 17 soon."),
        ("a lone *", "a * lone star", "a \\* lone star"),
        ("a lone **", "a ** lone pair", "a \\*\\* lone pair"),
        ("a lone backtick", "a ` lone backtick", "a \\` lone backtick"),
        ("trailing whitespace", "line with trailing   ", "line with trailing"),
        ("leading whitespace", "  indented paragraph", "indented paragraph"),
    ]

    def test_both_spellings_convert_to_the_same_blocks(self):
        for label, authored, rendered in self.PAIRS:
            with self.subTest(shape=label):
                self.assertEqual(mb.lines_blocks(authored, max_nesting=None),
                                 mb.lines_blocks(rendered, max_nesting=None))

    def test_pushing_the_escaped_spelling_back_does_not_accumulate_backslashes(self):
        """What push settles into `task.md` gets pushed again on the next edit. The
        escape rows are the ones that could drift: if the backslash the renderer added
        were stored as text rather than consumed, every round trip would add another
        one and the file would never converge."""
        for label, authored, rendered in self.PAIRS:
            if "\\" not in rendered:
                continue
            with self.subTest(shape=label):
                self.assertEqual(text_of(mb.lines_blocks(rendered)[0]), authored)

    def test_the_pairs_really_are_different_strings(self):
        """A pair that quietly became identical would make the assertions above pass
        while testing nothing."""
        for label, authored, rendered in self.PAIRS:
            with self.subTest(shape=label):
                self.assertNotEqual(authored, rendered)

    def test_a_lone_tilde_is_not_escaped(self):
        """The escaping rules read a lone `~` as prose, which is why `~Aug 17` round
        trips today. Recorded because the live bug report named it as the failing
        shape: the failing one is an unpaired `~~`, and a fix aimed at the lone tilde
        would have broken a working case and fixed nothing."""
        self.assertEqual(mb.lines_blocks("Due ~Aug 17 at the latest."),
                         mb.lines_blocks("Due ~Aug 17 at the latest."))
        self.assertEqual(text_of(mb.lines_blocks("Due ~Aug 17 at the latest.")[0]),
                         "Due ~Aug 17 at the latest.")


if __name__ == "__main__":
    unittest.main()
