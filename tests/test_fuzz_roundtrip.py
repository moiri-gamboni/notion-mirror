"""Seeded fuzz over the render -> parse round trip, at both tiers.

The example suites (`test_md_blocks.py`, `test_renderer.py`) pin each block type's
shape one case at a time; what they cannot enumerate is the interaction space —
an escaped delimiter against a link in the next run, boundary whitespace inside a
bold code span, a table cell whose text is mostly pipes. Each fuzz case here
builds a random document through the same write-shape the converter emits, runs
it through the real renderer and the real parser, and checks invariants that hold
for every document rather than outputs that hold for one:

- the visible text survives the round trip, character for character,
- every annotation and link survives on the characters that carry it
  (whitespace is exempt: the renderer deliberately wraps a run's trimmed core,
  so a marker on a space has no rendering and is dropped by design),
- the rendering *converges*: Notion may group one span's wrappers differently
  from the canonical nesting a re-render picks, so the bytes may change once —
  but the second cycle must reproduce both the runs and the bytes. This is the
  push-stability invariant: without it every push after the first reports a
  row nobody touched as changed,
- nothing throws, and nothing is Refused (the generator stays inside the
  shapes the renderer emits; a Refused here is a parser rejecting its own
  renderer's output).

The first runs of this suite surfaced six real defects, all fixed beside it:
the `~~` open guard refusing `~~*x*~~`, delimiters inside code spans closing
outer spans, group-edge star collisions (`***X** Y*` and `***i* x**`),
backtrack-inflated bold units, `md_link` escaping `]` inside code spans, and
`absorbs_a_paragraph` dropping the blank line after `<u>`-leading paragraphs.

Deterministic: case N always builds the same document (`random.Random(N)`), so a
failure reproduces from the printed case index alone. FUZZ_CASES scales the run
(default 400 span / 250 block, CI-friendly); a deep pass is
`FUZZ_CASES=200000 python3 -m unittest tests.test_fuzz_roundtrip` from the
repository root — sized by experience: the escaped-backtick link-label defect
only surfaced past 20,000 cases.

Substrate limits the generator steps around, all documented in `md_blocks`'s own
header: a backtick inside a code span has no spelling; a paragraph whose text
starts with a line-leading marker is read back as that block; a quote or callout
cannot carry children; a code block's caption comes back as an italic paragraph.
"""
import os
import random
import re
import sys
import unittest

TESTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(TESTS))
if TESTS not in sys.path:  # discovery adds it; `python3 -m unittest tests.<mod>` does not
    sys.path.insert(0, TESTS)
from notion_core import md_blocks as mb          # noqa: E402
from notion_core.richtext import rich_md         # noqa: E402

from fixture_support import _as_read_back, render_blocks  # noqa: E402

SPAN_CASES = int(os.environ.get("FUZZ_CASES", 400))
BLOCK_CASES = int(os.environ.get("FUZZ_CASES", 250))

# Text fragments lean on markdown-significant characters on purpose: whatever a
# Notion run holds must come back as the same text, escaped however needed.
FRAGMENTS = [
    "plain words", "a*b", "x**y", "50%*", "e~~f", "2 * 3 * 4 = 24",
    "[note]", "a]b", "C:\\temp", "back\\slash\\", "trailing\\",
    "<u>", "</u>", "a_b_c", "$5 and $10", "pipe | here", "*Topic search*",
    "  spaced  ", " ", "émoji 🎉 inside", "~x~", "***", "glob*.py",
    "x: ", " :y", "`tick", "tick`", "no",
]
URLS = ["https://example.org/a", "https://example.org/b?q=1&x=2", "mailto:x@example.org"]
#: Inline equations, which render as `$expr$` and are compared as whole units.
EXPRESSIONS = ["E = mc^2", "x_i", "a \\ne b", "\\sum_k k"]
ANNOTS = ("bold", "italic", "strikethrough", "underline", "code")


def item(text, annots=(), href=None):
    """One rich_text item in the write shape `md_blocks.rich` produces."""
    out = {"type": "text", "text": {"content": text}}
    if href:
        out["text"]["link"] = {"url": href}
    if annots:
        out["annotations"] = {a: True for a in annots}
    return out


def _signature(it):
    return (frozenset(k for k, v in (it.get("annotations") or {}).items() if v),
            (it["text"].get("link") or {}).get("url"))


def gen_items(rng, max_items=5):
    """A random run list, normalised the way Notion stores one.

    Adjacent runs with identical formatting are merged (`_merge_runs` semantics):
    Notion never hands the renderer two side-by-side identical-format runs, and
    without that normalisation the fuzz manufactures inputs no page can hold —
    a plain `[a` beside a plain `b](u)` is one stored run whose bracket the
    escaper sees, not two runs it cannot see across.

    Nothing is shielded: every adjacent-run shape the renderer can be handed is
    asserted, delimiter collisions included. `rich_md` verifies its own output
    against the real parser and re-spells a seam that would not read back, so a
    failure here is a genuine defect rather than a known limit.
    """
    items = []
    for _ in range(1 + int(rng.random() * max_items)):
        text = rng.choice(FRAGMENTS)
        annots = tuple(a for a in ANNOTS if rng.random() < 0.22)
        if rng.random() < 0.08:
            items.append({"type": "equation",
                          "equation": {"expression": rng.choice(EXPRESSIONS)}})
            continue
        href = rng.choice(URLS) if rng.random() < 0.15 else None
        items.append(item(text, annots, href))
    return mb._merge_runs(items)


def _eq_expr(it):
    return (it.get("equation") or {}).get("expression", "")


def char_map(items):
    """[(char, annotations, href)] with whitespace styling canonicalised.

    Whitespace styling has no rendering of its own: the renderer wraps a style
    group's whitespace-trimmed core (`** x **` violates CommonMark's flanking
    rule), and a whitespace-only run's own styling — say a bold+underline
    space between two bold runs — is dropped entirely, the space re-emerging
    inside whatever its neighbours share. So each whitespace run is normalised
    to the *intersection* of its non-whitespace neighbours' styles, and to
    bare at the ends of the string. That still asserts interior-whitespace
    survival where it is expressible (a space inside one bold sentence stays
    bold on both sides of the comparison), while text characters are always
    compared exactly.
    """
    raw = []
    for it in items:
        if it.get("type") == "equation":
            # an equation is one indivisible unit; its expression is compared
            # whole rather than character by character
            raw.append(("\u2211" + _eq_expr(it), frozenset(), None))
            continue
        text = it.get("text", {}).get("content", "")
        annots = frozenset(k for k, v in (it.get("annotations") or {}).items() if v)
        href = (it.get("text", {}).get("link") or {}).get("url")
        for ch in text:
            raw.append((ch, annots, href))
    out, i = [], 0
    while i < len(raw):
        if not raw[i][0].isspace():
            out.append(raw[i])
            i += 1
            continue
        j = i
        while j < len(raw) and raw[j][0].isspace():
            j += 1
        left = raw[i - 1] if i > 0 else None
        right = raw[j] if j < len(raw) else None
        if left is None or right is None:
            annots, href = frozenset(), None
        else:
            annots = left[1] & right[1]
            href = left[2] if left[2] == right[2] else None
        out.extend((raw[k][0], annots, href) for k in range(i, j))
        i = j
    return out


class SpanRoundTripFuzz(unittest.TestCase):
    def test_random_run_lists_survive_render_and_reparse(self):
        failures = []
        for case in range(SPAN_CASES):
            rng = random.Random(case)
            items = gen_items(rng)

            def fail(problem, detail):
                failures.append(f"case {case} items={items!r}\n  {problem}: {detail}")

            try:
                md = rich_md(_as_read_back(items))
                parsed = mb.inline_md(md)
                if char_map(parsed) != char_map(items):
                    fail("styled text changed across render/reparse",
                         f"md={md!r} parsed={parsed!r}")
                # Push stability is convergence, not a strict fixed point: Notion
                # may group one span's wrappers differently from the canonical
                # nesting a re-render picks, so the *bytes* may change once — but
                # the second cycle must reproduce both the runs and the bytes, or
                # every push after the first reports an untouched row as changed.
                md2 = rich_md(_as_read_back(parsed))
                parsed2 = mb.inline_md(md2)
                if char_map(parsed2) != char_map(parsed):
                    fail("styled text drifted on the second cycle",
                         f"md={md!r} md2={md2!r}")
                elif rich_md(_as_read_back(parsed2)) != md2:
                    fail("rendering does not converge",
                         f"md2={md2!r} md3={rich_md(_as_read_back(parsed2))!r}")
            except Exception as e:  # noqa: BLE001 — any throw is the finding
                fail("threw", repr(e))
        self.assertEqual([], failures[:10], f"{len(failures)} failing cases")


# ---------------------------------------------------------------- block tier

#: What must not open the rendered form of a paragraph-carried text: everything
#: `BLOCK_START` names plus the single-line block markers and a leading emoji
#: (an emoji after `- ` is a stand-in, after `> ` a callout icon). Conservative
#: across block types so one generator serves all of them.
_HAZARD_HEAD = re.compile(r"^(#{1,6} |- |\d+\. |> |\||```|\$\$|!\[|---\s*$|<|\[[ xX]\] |▸ )")
_SEPARATOR_CELL = re.compile(r":?-{3,}:?")


def gen_text_items(rng, max_items=4):
    """Run list safe to carry inside any block line.

    Edge whitespace is stripped (the renderer's own lines are rstripped on the
    way back in, and a leading space reads as indentation), a leading marker or
    emoji is shielded with a plain prefix, and hard-break newlines are left to
    the caller.
    """
    items = [it for it in gen_items(rng, max_items) if it.get("type") != "equation"]
    # strip edge whitespace off the run list as a whole
    while items and not items[0]["text"]["content"].strip():
        items.pop(0)
    while items and not items[-1]["text"]["content"].strip():
        items.pop()
    if not items:
        return []
    first = items[0]["text"]
    first["content"] = first["content"].lstrip()
    last = items[-1]["text"]
    last["content"] = last["content"].rstrip()
    rendered = rich_md(_as_read_back(items))
    if not rendered or rendered[0].isspace():
        items = [item("x ")] + items
    return mb._merge_runs(items)


CHILD_TYPES = ("paragraph", "bulleted_list_item", "numbered_list_item", "to_do",
               "toggle", "quote", "callout", "divider")
NESTING_TYPES = ("bulleted_list_item", "numbered_list_item", "to_do", "toggle",
                 "quote", "callout")
TOP_TYPES = CHILD_TYPES + ("heading", "code", "equation", "table")
CALLOUT_ICONS = ("⚠️", "💡", "📌", None)
#: No line may start with a fence — it would end the block early — and the
#: generator filters any that does as a second guard.
CODE_BODIES = ("x = 1\nprint(x)", "grep -r 'a|b' .", "SELECT *\nFROM t  -- c")


#: Text a paragraph may *start* with now that the renderer escapes line-leading
#: markers: each of these used to come back as the block its marker names.
MARKER_LEADS = ("- not a bullet ", "1. not numbered ", "> not a quote ",
                "# not a heading ", "| piped ", "$$ ", "![ ", "--- then prose ")

#: Text a one-line block may *start* with now that the renderer escapes the
#: markers that live inside such a block: each used to change the block's type.
INNER_MARKER_LEADS = ("[x] boxed ", "[ ] unboxed ", "▸ arrowed ", "🎉 iconned ",
                      " space-led ", "\\▸ literal-escape ")


def _no_trailing_space(items):
    """Drop whitespace at the very end of a block's text.

    A markdown line cannot carry it — `_flat` rstrips every line it reads, and
    an editor would strip it anyway — so a block whose text *ends* in a space
    or a newline is outside what the round trip can promise. That is the one
    substrate limit at this tier, and the generator honours it rather than
    asserting on it. Text that is *entirely* whitespace collapses to empty,
    which is a shape worth generating: an empty block must keep its type.
    """
    items = [dict(it, text=dict(it["text"])) for it in items]
    # every line edge, not just the last: a space sitting before a hard break
    # is indistinguishable from the break's own two-space marker
    for k, it in enumerate(items):
        it["text"]["content"] = re.sub(r"[ \t]+\n", "\n", it["text"]["content"])
        nxt = items[k + 1]["text"]["content"] if k + 1 < len(items) else ""
        if nxt.startswith("\n"):
            it["text"]["content"] = it["text"]["content"].rstrip()
    while items:
        items[-1]["text"]["content"] = items[-1]["text"]["content"].rstrip()
        if items[-1]["text"]["content"]:
            break
        items.pop()
    return [it for it in items if it["text"]["content"]] or items


def gen_rich_block(rng, btype, depth):
    data = {"rich_text": gen_text_items(rng)}
    if btype == "paragraph" and not data["rich_text"]:
        # An empty paragraph is a Notion *spacer*, and it renders as a blank
        # line — there is nothing in markdown to rebuild the block from. That
        # is the one documented loss at this tier (`test_md_blocks.LOSSES`),
        # and unlike an empty bullet or heading it cannot be given a marker
        # without writing one into every blank line of every mirrored page.
        data["rich_text"] = [item("x")]
    if rng.random() < 0.2:
        data["rich_text"] = mb._merge_runs(
            [item(rng.choice(MARKER_LEADS))] + data["rich_text"])
    if rng.random() < 0.2:
        # a shift+enter hard break: a second line that itself starts no block
        extra = gen_text_items(rng, 2)
        if rng.random() < 0.3:
            extra = [item(rng.choice(MARKER_LEADS))] + extra
        data["rich_text"] = mb._merge_runs(
            data["rich_text"] + [item("\n")] + extra)
    if btype == "to_do":
        data["checked"] = rng.random() < 0.5
    if btype == "callout":
        icon = rng.choice(CALLOUT_ICONS)
        if icon:
            data["icon"] = {"type": "emoji", "emoji": icon}
    if btype in ("bulleted_list_item", "numbered_list_item", "to_do", "toggle",
                 "quote", "callout") and rng.random() < 0.25:
        # text shaped like another block type's marker — a `[x] ` bullet, a
        # `▸ ` bullet, an icon-leading quote — which the renderer escapes
        data["rich_text"] = mb._merge_runs(
            [item(rng.choice(INNER_MARKER_LEADS))] + data["rich_text"])
    # last, after every prefix and break: the block's text cannot end in
    # whitespace, which is the one substrate limit at this tier
    data["rich_text"] = _no_trailing_space(data["rich_text"])
    if depth < 2 and btype in NESTING_TYPES and rng.random() < 0.45:
        data["children"] = [gen_block(rng, depth + 1)
                            for _ in range(1 + int(rng.random() * 2))]
    return {"type": btype, btype: data}


def gen_block(rng, depth=0):
    btype = rng.choice(TOP_TYPES if depth == 0 else CHILD_TYPES)
    if btype == "divider":
        return {"type": "divider", "divider": {}}
    if btype == "heading":
        lvl = 1 + int(rng.random() * 3)
        data = {"rich_text": _no_trailing_space(gen_text_items(rng))}
        if depth < 2 and rng.random() < 0.35:
            data["children"] = [gen_block(rng, depth + 1)
                                for _ in range(1 + int(rng.random() * 2))]
        return {"type": f"heading_{lvl}", f"heading_{lvl}": data}
    if btype == "code":
        body = rng.choice(CODE_BODIES)
        body = "\n".join(ln for ln in body.split("\n")
                         if not ln.strip().startswith("```")) or "x"
        lang = rng.choice(("python", "bash", "json", "plain text"))
        code = {"rich_text": mb.rich(body), "language": lang}
        if rng.random() < 0.3:
            code["caption"] = gen_text_items(rng, 2)
        return {"type": "code", "code": code}
    if btype == "equation":
        expr = rng.choice(("E = mc^2", "a \\ne b", "\\sum_{i}\nx_i"))
        return {"type": "equation", "equation": {"expression": expr}}
    if btype == "table":
        width = 1 + int(rng.random() * 3)
        rows = []
        for _ in range(1 + int(rng.random() * 3)):
            cells = [gen_text_items(rng, 2) if rng.random() < 0.8 else [item("c")]
                     for _ in range(width)]
            # a row of separator-shaped cells is the one row the parser drops
            if all(_SEPARATOR_CELL.fullmatch(
                    "".join(i["text"]["content"] for i in c).strip()) for c in cells):
                cells[0] = [item("c")]
            rows.append({"table_row": {"cells": cells}})
        return {"type": "table", "table": {
            "table_width": width, "has_column_header": True, "has_row_header": False,
            "children": rows}}
    return gen_rich_block(rng, btype, depth)


def canon(block):
    """A block reduced to what the round trip promises to preserve."""
    t = block["type"]
    d = block[t]
    if t == "divider":
        return ("divider",)
    if t == "code":
        text = "".join(i["text"]["content"] for i in d["rich_text"])
        lang = d.get("language") or "plain text"
        return ("code", lang, text, tuple(char_map(d.get("caption", ()))))
    if t == "equation":
        return ("equation", d.get("expression", ""))
    if t == "table":
        width = d["table_width"]
        rows = []
        for row in d["children"]:
            cells = [tuple(char_map(c)) for c in row["table_row"]["cells"]]
            rows.append(tuple((cells + [()] * width)[:width]))
        return ("table", tuple(rows))
    extra = ()
    if t == "to_do":
        extra = (bool(d.get("checked")),)
    if t == "callout":
        extra = ((d.get("icon") or {}).get("emoji"),)
    kids = tuple(canon(k) for k in d.get("children", ()))
    return (t, tuple(char_map(d.get("rich_text", ()))), extra, kids)


class BlockRoundTripFuzz(unittest.TestCase):
    def test_random_block_trees_survive_render_and_reparse(self):
        failures = []
        for case in range(BLOCK_CASES):
            rng = random.Random(10_000 + case)
            blocks = [gen_block(rng) for _ in range(1 + int(rng.random() * 4))]

            def fail(problem, detail):
                failures.append(f"case {case}\n  blocks={blocks!r}\n  {problem}: {detail}")

            try:
                md = render_blocks(blocks)
                parsed = mb.lines_blocks(md)
                want = [canon(b) for b in blocks]
                got = [canon(b) for b in parsed]
                if got != want:
                    fail("block tree changed across render/reparse",
                         f"md={md!r}\n  want={want!r}\n  got={got!r}")
                # convergence, not a strict fixed point — see the span suite
                md2 = render_blocks(parsed)
                parsed2 = mb.lines_blocks(md2)
                if [canon(b) for b in parsed2] != got:
                    fail("block tree drifted on the second cycle",
                         f"md={md!r}\n  md2={md2!r}")
                elif render_blocks(parsed2) != md2:
                    fail("rendering does not converge",
                         f"md2={md2!r} md3={render_blocks(parsed2)!r}")
            except Exception as e:  # noqa: BLE001 — a Refused here is the parser
                fail("threw", repr(e))     # rejecting its own renderer's output
        self.assertEqual([], failures[:10], f"{len(failures)} failing cases")


class PropertyRoundTripFuzz(unittest.TestCase):
    """The property path — `prop_rich`, the entry point a row's Description, DoD
    and Updates go through — over the same random runs as the span suite.

    It is a different reader from `inline_md` (it promotes a bare page id to a
    live mention) and the block suites never touched it, which is how a
    precedence bug lived there: the mention scan ran ahead of the parser and
    split the string at every id, so any span containing one lost its markers
    to literal text. What is asserted here is what the property path *can*
    promise: every span survives, and a mention appears only where the text
    was plain. A mention itself does not round-trip — Notion returns it as its
    page title — so the generator keeps ids out of styled runs and the
    comparison treats a mention as its id.
    """

    PAGE_ID = "3a000000000000000000000000000000"

    def _signature(self, items):
        out = []
        for it in items:
            if it.get("type") == "mention":
                out.append(("@" + it["mention"]["page"]["id"], frozenset(), None))
                continue
            annots = frozenset(k for k, v in (it.get("annotations") or {}).items() if v)
            href = ((it.get("text") or {}).get("link") or {}).get("url")
            for ch in (it.get("text") or {}).get("content", ""):
                if not ch.isspace():
                    out.append((ch, annots, href))
        return out

    def test_random_property_values_keep_their_spans(self):
        """Two independent claims, neither of them a re-implementation of the
        code under test: every styled character keeps its styling, and the
        text comes back whole once each mention is read as the id it replaced.
        """
        failures = []
        for case in range(SPAN_CASES):
            rng = random.Random(500_000 + case)
            items = [it for it in gen_items(rng) if it.get("type") != "equation"]
            if not items:
                continue
            if rng.random() < 0.5:
                where = int(rng.random() * len(items))
                items[where]["text"]["content"] += f" {self.PAGE_ID} "
            items = mb._merge_runs(items)
            md = rich_md(_as_read_back(items))
            try:
                got = mb.prop_rich(md)
            except Exception as e:  # noqa: BLE001 — any throw is the finding
                failures.append(f"case {case} md={md!r} threw {e!r}")
                continue
            styled_want = [c for c in self._signature(_as_read_back(items))
                           if c[1] or c[2]]
            styled_got = [c for c in self._signature(got) if c[1] or c[2]]
            if styled_got != styled_want:
                failures.append(f"case {case} styling lost md={md!r}")
                continue
            want_text = "".join(
                ch for it in items for ch in it["text"]["content"] if not ch.isspace())
            got_text = "".join(
                (it["mention"]["page"]["id"].replace("-", "") if it.get("type") == "mention"
                 else "".join(c for c in it["text"]["content"] if not c.isspace()))
                for it in got)
            if got_text != want_text:
                failures.append(f"case {case} text changed md={md!r}\n"
                                f"  want={want_text!r}\n  got={got_text!r}")
        self.assertEqual([], failures[:6], f"{len(failures)} failing cases")

    def test_an_id_inside_a_span_stays_content(self):
        """The defect this suite was added for: a code span holding a page id
        used to lose its backticks to literal text, permanently, because the
        mention scan ran before the parser."""
        for md, annot in ((f"a (`{self.PAGE_ID}`) b", "code"),
                          (f"a **{self.PAGE_ID}** b", "bold"),
                          (f"a *{self.PAGE_ID}* b", "italic")):
            got = mb.prop_rich(md)
            self.assertEqual([], [i for i in got if i.get("type") == "mention"], md)
            styled = [i for i in got if (i.get("annotations") or {}).get(annot)]
            self.assertEqual([self.PAGE_ID],
                             [i["text"]["content"] for i in styled], md)

    def test_a_bare_id_is_still_promoted_to_a_mention(self):
        [pre, mention, post] = mb.prop_rich(f"see {self.PAGE_ID} there")
        self.assertEqual("mention", mention["type"])
        self.assertEqual("3a000000-0000-0000-0000-000000000000",
                         mention["mention"]["page"]["id"])
        self.assertEqual(["see ", " there"],
                         [pre["text"]["content"], post["text"]["content"]])

    def test_an_id_in_a_link_target_is_not_promoted(self):
        got = mb.prop_rich(f"see [the row](https://notion.so/{self.PAGE_ID}) now")
        self.assertEqual([], [i for i in got if i.get("type") == "mention"])
        self.assertEqual(f"https://notion.so/{self.PAGE_ID}",
                         got[1]["text"]["link"]["url"])


class RoundTripRegressions(unittest.TestCase):
    """Named pins for classes the fuzz surfaced, kept alive at every scale."""

    def test_adjacent_star_spans_round_trip(self):
        """Flat delimiter collisions: close yields to the complete next span."""
        for md, want in (
            ("*a***b**", [("a", {"italic"}), ("b", {"bold"})]),
            ("**a***b*", [("a", {"bold"}), ("b", {"italic"})]),
            ("*a***b***c*", [("a", {"italic"}), ("b", {"bold"}), ("c", {"italic"})]),
        ):
            got = [(r["text"]["content"],
                    {k for k, v in (r.get("annotations") or {}).items() if v})
                   for r in mb.inline_md(md)]
            self.assertEqual(want, got, md)

    def test_structured_star_boundaries_round_trip(self):
        """Spans carrying code spans, links, underline tags or nested wrappers
        flush against a star boundary — unreadable to any flat lookahead,
        exact under the delimiter stack."""
        cases = (
            ("**`</u>` *[\\`tick](https://e.org/b)***\\*Topic search\\**`pipe`*",
             [("</u>", {"bold", "code"}, None), (" ", {"bold"}, None),
              ("`tick", {"bold", "italic"}, "https://e.org/b"),
              ("*Topic search*", set(), None),
              ("pipe", {"code", "italic"}, None)]),
            ("**plain words***x:  <u>**trailing\\\\**</u>*",
             [("plain words", {"bold"}, None), ("x:  ", {"italic"}, None),
              ("trailing\\", {"bold", "italic", "underline"}, None)]),
            ("**plain words *[e\\~~f](mailto:x@e.org)***~~[note]~~***`*T*`***",
             [("plain words ", {"bold"}, None),
              ("e~~f", {"bold", "italic"}, "mailto:x@e.org"),
              ("[note]", {"strikethrough"}, None),
              ("*T*", {"bold", "code", "italic"}, None)]),
        )
        for md, want in cases:
            got = [(r["text"]["content"],
                    {k for k, v in (r.get("annotations") or {}).items() if v},
                    (r["text"].get("link") or {}).get("url"))
                   for r in mb.inline_md(md)]
            self.assertEqual(want, got, md)

    def test_backtick_bearing_code_runs_round_trip(self):
        """CommonMark's long-fence spelling: one more backtick than the
        content's longest run, space-padded, any fence length read back."""
        for content in ("a`b", "`tick", "tick`", "``", "a``b", "x```y"):
            items = [{"type": "text", "plain_text": content,
                      "annotations": {"code": True}}]
            md = rich_md(items)
            got = [(r["text"]["content"],
                    {k for k, v in (r.get("annotations") or {}).items() if v})
                   for r in mb.inline_md(md)]
            self.assertEqual([(content, {"code"})], got, md)

    def test_an_unclosed_bold_keeps_its_stars_and_its_prose(self):
        """A human-typed `**` with no close on the block must not donate its
        second star to a fake span assembled from the prose behind it — the
        adjacent-unit exception applies only flush after a consumed span."""
        runs = mb.inline_md("**E3 is registry-resolved *and* hand-adjudicated")
        got = [(r["text"]["content"],
                {k for k, v in (r.get("annotations") or {}).items() if v})
               for r in runs]
        self.assertEqual([("**E3 is registry-resolved ", set()),
                          ("and", {"italic"}),
                          (" hand-adjudicated", set())], got)

    def test_the_star_run_guards_still_hold(self):
        """The collision fix must not resurrect the bugs the guards exist for."""
        for md in ("2 * 3 * 4 = 24", "spaced ** double **"):
            [run] = mb.inline_md(md)
            self.assertEqual(md, run["text"]["content"])
            self.assertNotIn("annotations", run)

    def test_a_quote_region_carries_text_seam_and_children(self):
        """The three parts of a region, and the seam that keeps them distinct."""
        quote = {"type": "quote", "quote": {
            "rich_text": mb.rich("the quote"),
            "children": [{"type": "paragraph", "paragraph": {"rich_text": mb.rich("a child")}},
                         {"type": "bulleted_list_item",
                          "bulleted_list_item": {"rich_text": mb.rich("a bullet")}}]}}
        md = render_blocks([quote])
        self.assertIn("\n>\n", md, md)          # the seam is a bare `>`
        [back] = mb.lines_blocks(md)
        self.assertEqual("quote", back["type"])
        self.assertEqual("the quote",
                         "".join(r["text"]["content"] for r in back["quote"]["rich_text"]))
        self.assertEqual(["paragraph", "bulleted_list_item"],
                         [k["type"] for k in back["quote"]["children"]])

    def test_sibling_quotes_stay_siblings(self):
        """Two regions in a row are separated by a truly blank line — without it
        they read as one region whose second half became a child."""
        one = {"type": "quote", "quote": {"rich_text": mb.rich("first")}}
        two = {"type": "callout", "callout": {"rich_text": mb.rich("second")}}
        back = mb.lines_blocks(render_blocks([one, two]))
        self.assertEqual(["quote", "callout"], [b["type"] for b in back])
        self.assertEqual([[], []], [b[b["type"]].get("children", []) for b in back])

    def test_a_hard_break_inside_a_quote_survives(self):
        """An unmarked second `> ` line is a child block, not a continuation:
        only the two-space break joins lines into one run of text."""
        quote = {"type": "quote", "quote": {"rich_text": mb.rich("one\ntwo")}}
        md = render_blocks([quote])
        [back] = mb.lines_blocks(md)
        self.assertEqual("one\ntwo",
                         "".join(r["text"]["content"] for r in back["quote"]["rich_text"]))
        self.assertEqual([], back["quote"].get("children", []))

    def test_two_adjacent_tables_stay_two_tables(self):
        table = {"type": "table", "table": {
            "table_width": 1, "has_column_header": True, "has_row_header": False,
            "children": [{"table_row": {"cells": [[item("a")]]}},
                         {"table_row": {"cells": [[item("b")]]}}]}}
        md = render_blocks([table, table])
        self.assertIn("\n\n", md, md)
        parsed = mb.lines_blocks(md)
        self.assertEqual(["table", "table"], [b["type"] for b in parsed], md)


if __name__ == "__main__":
    unittest.main()
