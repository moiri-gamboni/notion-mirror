"""Block tree -> markdown: the `notion_walk`-compatible renderer.

The counterpart of `md_blocks`' parser at the block tier, as `richtext` is at the span
tier. tasksync subclasses `Walker` (`pull.SentinelWalker`) to record which line each
keep-worthy block produced, so the class's `render`/`walk` split is a contract, not an
implementation detail: a rewrite that stopped calling `render` per block would break the
sentinel pairing without breaking anything here.
"""
import collections

from .api import ApiError
from .flatten import md_cell
from .md_blocks import CAPTION_MARK, escape_inner_marker, escape_leading_marker
from .richtext import absorbs_a_paragraph, plain, rich_md
from .util import undash


# One comment as the mirror renders it. `cid`/`did` are the comment's own id and
# its discussion's, undashed — they are what the mirror keys comments by, and
# what tasksync reads back out of the bullet (see `rowmd.cid_trailer`). Both are ''
# only for a comment that reached us without them (a pre-2026-08 webhook capture).
Comment = collections.namedtuple("Comment", "text who when cid did")

#: The three block types that are nothing but a URL, and the marker that distinguishes
#: them in the rendered markdown. `md_blocks.STANDIN` is the other half — a parser
#: reading these back keys on the same three characters.
URL_BLOCK_MARK = {"bookmark": "🔖", "embed": "🖼️", "link_preview": "👁️"}

# `CAPTION_MARK` (imported): a code block's caption goes on its own line under
# the fence. The `- ` keeps it out of a neighbouring paragraph, and the marker
# is what `md_blocks` keys on to give it back to the block above.


class Walker:
    """notion_walk-compatible block -> markdown renderer.

    Row bodies and content pages render identically. They did not until
    2026-08-27: a `row_mode` flag nested a heading's children only for row
    bodies, and the difference was a bug rather than a convention — the
    content-page spelling lost the nesting, so the flag went with it.
    Collects (block_id, anchor) pairs during the walk; comments are fetched
    afterwards via harvest_comments() so callers can cap the cost.
    """

    def __init__(self, api, users, max_blocks=4000, max_requests=None):
        self.api = api
        self.users = users
        self.max_blocks = max_blocks
        # per-page request budget: one deeply-nested auto-generated page (Notion
        # makes one /children call per has_children block) can otherwise eat
        # thousands of requests. Snapshot the global counter and stop when the
        # delta exceeds the cap, marking the render truncated.
        self.max_requests = max_requests
        self._req_start = api.n
        self.nblocks = 0
        self.block_anchors = []  # (block_id, anchor[:90]) in walk order
        self.attachments = []    # (block_id, kind, url, caption)
        self.child_pages = []    # (id32, title)
        self.child_dbs = []      # (id32, title)
        self.truncated = False
        self.partial = False     # some child blocks were inaccessible (4xx)

    def over_budget(self):
        return self.max_requests is not None and (self.api.n - self._req_start) >= self.max_requests

    def comments_for(self, block_id):
        """-> [Comment]. Text goes through `rich_md`, not `plain`: a comment's
        @-mention carries its target only in the item's `href`, so flattening to
        plain_text turns "as mentioned <page>" into "as mentioned Untitled" with
        nothing left to follow. Bodies have always rendered this way."""
        out = []
        try:
            for c in self.api.paginate("GET", "/comments", params={"block_id": block_id}):
                out.append(Comment(rich_md(c.get("rich_text")),
                                   self.users.name(c.get("created_by")),
                                   c.get("created_time", ""),
                                   undash(c.get("id") or ""),
                                   undash(c.get("discussion_id") or "")))
        except ApiError:
            pass
        return out

    def harvest_comments(self, cap=None):
        """Query comments for every walked block -> ([(anchor, comments)], capped).
        cap: skip the per-block scan entirely when the page has more blocks
        (page-level comments are the caller's job either way)."""
        if cap is not None and len(self.block_anchors) > cap:
            return [], True
        found = []
        for bid, anchor in self.block_anchors:
            cs = self.comments_for(bid)
            if cs:
                found.append((anchor, cs))
        return found, False

    def walk(self, block_id, lines, indent):
        if self.truncated:
            return
        try:
            for b in self.api.paginate("GET", f"/blocks/{block_id}/children"):
                self.nblocks += 1
                if self.nblocks > self.max_blocks or self.over_budget():
                    self.truncated = True
                    return
                self.render(b, lines, indent)
        except ApiError as e:
            # a 4xx on one container (unshared synced block, AI block, deleted
            # child) must not abandon the whole page — mark and continue
            self.partial = True
            lines.append("  " * indent + f"<!-- child blocks inaccessible here: HTTP {e.code} -->")

    @staticmethod
    def _broken(text):
        """``text`` as lines, each but the last carrying the two-space GFM hard
        break. A shift+enter inside a bullet used to render as a bare newline,
        which reads back as a *second block* — the block silently split in two
        on the next push, and the split was a fixed point so nothing reported
        it. Only the paragraph and quote branches ever marked the break."""
        parts = text.split("\n")
        # every line but the last carries the marker, blank ones included: a
        # run whose first line is empty used to drop the marker there, and the
        # rest of the run became a block of its own. `_flat` tests the raw line
        # for the marker and strips it from the content, so the two spaces on
        # an otherwise blank line cost nothing but carry the break.
        return [f"{ln}  " if k < len(parts) - 1 else ln for k, ln in enumerate(parts)]

    def _one_line(self, lines, p, prefix, text):
        """A one-line block: ``prefix`` then its text, hard breaks marked. The
        prefix keeps its trailing space even when the text is empty — an empty
        `- [x] ` rstripped to `- [x]` used to read back as a *bullet* whose text
        is `[x]`, so `_one_line_block` accepts the bare marker as the empty
        form and `escape_inner_marker` escapes text that looks like one."""
        broken = self._broken(text)
        # the prefix's trailing space goes only when there is nothing after it
        # at all: rstripping a line that ends in the two-space hard break would
        # eat the marker and rejoin the block's own lines into separate blocks
        head = f"{p}{prefix}{broken[0]}"
        # the prefix's trailing space goes only when this is the whole block:
        # with lines after it, `broken[0]` carries the break marker, whose own
        # `.strip()` is empty — rstripping on that test ate the marker
        lines.append(head.rstrip() if len(broken) == 1 and not broken[0].strip() else head)
        # a continuation line is read at the block's own indent, so a block
        # marker at its start would end the block there — the same escape the
        # paragraph branch applies to every one of its lines
        lines.extend(f"{p}{escape_leading_marker(ln)}".rstrip() if not ln.strip()
                     else f"{p}{escape_leading_marker(ln)}" for ln in broken[1:])

    def render(self, b, lines, indent):
        t = b.get("type")
        data = b.get(t, {}) or {}
        p = "  " * indent

        anchor = plain(data.get("rich_text")) or (
            data.get("title") if t in ("child_page", "child_database") else "") or f"({t})"
        self.block_anchors.append((b["id"], anchor[:90]))

        if t == "child_page":
            pid = undash(b["id"])
            title = data.get("title", "untitled")
            self.child_pages.append((pid, title))
            lines.append(f"{p}- 📄 **{title}** — sub-page `{pid}`")
            return
        if t == "child_database":
            did = undash(b["id"])
            title = data.get("title", "untitled")
            self.child_dbs.append((did, title))
            lines.append(f"{p}- 🗄️ **{title}** — database `{did}` (rows in workspace/_databases/)")
            return

        if t == "paragraph":
            txt = rich_md(data.get("rich_text"))
            if absorbs_a_paragraph(lines):
                lines.append("")
            if txt:
                # a line-leading block marker is escaped so the paragraph comes
                # back a paragraph, not the block the marker names; the
                # two-space GFM hard break preserves shift+enter and keeps
                # emphasis spanning the break intact
                for raw_ln in self._broken(txt):
                    lines.append(f"{p}{escape_leading_marker(raw_ln.rstrip())}"
                                 + ("  " if raw_ln.endswith("  ") else ""))
            else:
                lines.append("")
        elif t in ("heading_1", "heading_2", "heading_3"):
            lvl = {"heading_1": "#", "heading_2": "##", "heading_3": "###"}[t]
            # the indent applies in both modes: a heading nested inside a
            # toggle or a column that renders at column 0 escapes its parent on
            # the way back, and its own children then attach to whatever the
            # flat line landed under
            self._one_line(lines, p, f"{lvl} ", rich_md(data.get("rich_text")))
        elif t == "bulleted_list_item":
            # the text is escaped where it would re-read as a different block
            # type — `[x] ` as a to-do, `▸ ` as a toggle, an icon as a callout
            self._one_line(lines, p, "- ", escape_inner_marker(rich_md(data.get("rich_text"))))
        elif t == "numbered_list_item":
            self._one_line(lines, p, "1. ", escape_inner_marker(rich_md(data.get("rich_text"))))
        elif t == "to_do":
            chk = "x" if data.get("checked") else " "
            self._one_line(lines, p, f"- [{chk}] ",
                           escape_inner_marker(rich_md(data.get("rich_text"))))
        elif t == "toggle":
            self._one_line(lines, p, "- ▸ ",
                           escape_inner_marker(rich_md(data.get("rich_text"))))
        elif t in ("quote", "callout"):
            # a quote/callout is a *region* of `> ` lines: its own text first,
            # then its children below (rendered normally, prefixed). Adjacent
            # regions get a blank line, or two sibling quotes read as one.
            if lines and lines[-1].lstrip().startswith(">"):
                lines.append("")
            icon = ((data.get("icon") or {}).get("emoji") or "") if t == "callout" else ""
            # an iconless callout keeps its leading-space signal — the empty
            # icon still gets its separator, which is what tells the reader
            # this is a callout and not a quote
            head = f"{icon} " if icon or t == "callout" else ""
            parts_ = self._broken(rich_md(data.get("rich_text")))
            for k, raw_ln in enumerate(parts_):
                # the text lines carry the same escapes a paragraph's do — a
                # block marker or an icon-lookalike at the start must not
                # change the block's type on the way back
                brk = "  " if raw_ln.endswith("  ") else ""
                ln = escape_leading_marker(raw_ln.rstrip())
                if k == 0:
                    ln = f"{head}{escape_inner_marker(ln)}"
                # an iconless callout is spelled by its empty icon slot — the
                # separator space — so when it has no text either, that space
                # is the whole signal and must survive the rstrip that an
                # ordinary blank quote line gets
                if t == "quote" and k == 0 and not ln.strip():
                    # `>  ` already means "callout with an empty icon slot", so
                    # a quote whose own first line is empty cannot be spelled
                    # with whitespace at all; one backslash says "no icon here"
                    # and the region reader takes it back off
                    ln = "\\"
                bare_callout = t == "callout" and not icon and k == 0 and not ln.strip()
                line = f"{p}> {ln}"
                lines.append((line if bare_callout else line.rstrip()) + brk)
        elif t == "code":
            lang = data.get("language", "") or ""
            body = plain(data.get("rich_text"))  # never inject md markers into code
            lines.append(f"{p}```{lang if lang != 'plain text' else ''}")
            for ln in body.split("\n"):
                lines.append(f"{p}{ln}")
            lines.append(f"{p}```")
            cap = rich_md(data.get("caption"))
            if cap:
                # a marked line, not italic prose: `*cap*` was indistinguishable
                # from a paragraph, so the caption came back as one and the
                # block lost it. The parser reattaches this to the code block
                # above it.
                lines.append(f"{p}{CAPTION_MARK} {cap}")
        elif t == "divider":
            lines.append(f"{p}---")
        elif t == "equation":
            lines.append(f"{p}$$")
            for ln in (data.get("expression", "") or "").split("\n"):
                lines.append(f"{p}{ln}")
            lines.append(f"{p}$$")
        elif t in ("image", "file", "pdf", "video", "audio"):
            f = data.get("file") or data.get("external") or {}
            url = f.get("url", "")
            cap = rich_md(data.get("caption")) or t
            if data.get("file"):  # Notion-hosted: signed URL expires -> download
                self.attachments.append((undash(b["id"]), t, url, cap))
                lines.append(f"{p}![{cap}](ATTACH:{undash(b['id'])})")
            else:
                lines.append(f"{p}![{cap}]({url})")
        elif t in URL_BLOCK_MARK:
            # All three used to render as a bare `[url](url)` — as does a paragraph
            # whose whole text is a link — so a reader could not tell an embedded video
            # from a bookmark from someone's typed link, and a bookmark's caption was
            # dropped outright. The marker follows the `- 📄`/`- 🗄️`/`- 🔗` stand-in
            # vocabulary, and the leading `- ` also stops the line being absorbed into
            # the paragraph above it.
            url = data.get("url", "") or ""
            cap = rich_md(data.get("caption"))
            lines.append(f"{p}- {URL_BLOCK_MARK[t]} [{cap or url}]({url})")
        elif t == "table_of_contents":
            lines.append(f"{p}- 📑 (table of contents)")
        elif t == "breadcrumb":
            pass
        elif t == "table":
            self.render_table(b, lines, indent)
            return
        elif t in ("column_list", "column", "synced_block"):
            pass
        elif t == "link_to_page":
            ref = data.get("page_id") or data.get("database_id") or data.get("comment_id") or ""
            lines.append(f"{p}- 🔗 link to `{undash(str(ref))}`")
        elif t == "template":
            lines.append(f"{p}- 🧩 (template: {rich_md(data.get('rich_text'))})")
        else:
            rt = data.get("rich_text")
            if rt:
                lines.append(f"{p}{rich_md(rt)}")
            else:
                lines.append(f"{p}<!-- unhandled block type: {t} -->")

        if b.get("has_children"):
            if t in ("quote", "callout"):
                # children render normally — blank separators and all — into a
                # private list, then every line joins the region under the `> `
                # prefix; a blank line becomes bare `>`, which the converter
                # reads as a blank line inside the region rather than a block.
                # The leading bare `>` is the seam between the block's own text
                # and its first child, exactly the blank line that separates
                # sibling paragraphs — without it a child paragraph reads as
                # the text's soft-wrapped continuation.
                sub = []
                self.walk(b["id"], sub, 0)
                lines.append(f"{p}>")
                lines.extend(f"{p}> {ln}" if ln.strip() else f"{p}>" for ln in sub)
                return
            if t == "synced_block":
                # reference blocks carry children on the ORIGINAL block
                target = ((data.get("synced_from") or {}).get("block_id")) or b["id"]
                self.walk(target, lines, indent)
                return
            nest = indent
            if t in ("bulleted_list_item", "numbered_list_item", "to_do", "toggle"):
                nest = indent + 1
            elif t in ("heading_1", "heading_2", "heading_3"):
                # a heading's children nest under it, as every other
                # child-bearing block's do — flat, they read back as siblings
                # and the heading loses them
                nest = indent + 1
            self.walk(b["id"], lines, nest)

    def render_table(self, b, lines, indent):
        p = "  " * indent
        if lines and lines[-1].lstrip().startswith("|"):
            # two sibling tables with nothing between them are one unbroken run
            # of `|` lines, which reads back as a single wider table; a blank
            # line is the boundary the parser's row loop already stops at.
            lines.append("")
        rows = []
        for rb in self.api.paginate("GET", f"/blocks/{b['id']}/children"):
            if rb.get("type") == "table_row":
                rows.append([rich_md(c) for c in rb["table_row"]["cells"]])
            self.block_anchors.append((rb["id"], "(table row)"))
        if not rows:
            return
        ncol = max(len(r) for r in rows)

        def fmt(r):
            return "| " + " | ".join(md_cell(c) for c in (r + [""] * ncol)[:ncol]) + " |"
        lines.append(p + fmt(rows[0]))
        lines.append(p + "| " + " | ".join(["---"] * ncol) + " |")
        for r in rows[1:]:
            lines.append(p + fmt(r))
