"""Notion rich-text -> markdown: the write half of the round trip.

`md_blocks` is the read half and parses exactly what this writes, which is why the two
live in one package. The span machinery below exists for one reason: Notion splits a
single formatted run across as many items as it likes, and wrapping each item on its own
puts `*` against `*` at the seams — a boundary markdown reads back somewhere else, so
the body never matches itself and every push reports a row nobody touched as changed.
"""
import re

from . import md_blocks
from .md_blocks import _is_escaped, escape_md, starts_block


def plain(rts):
    return "".join(x.get("plain_text", "") for x in rts or [])


def absorbs_a_paragraph(lines):
    """Whether a paragraph written next would be read back into the last line here.

    ``md_blocks`` continues a paragraph across any following line that starts no
    block of its own, so the question is a property of the *line*, not of the block
    that wrote it. Asking it this way covers the cases a block-type test misses: a
    code block's caption, a table of contents and a template all render as ordinary
    prose and would swallow the block below them — the caption case losing both the
    caption and the paragraph — and a container that renders nothing at all
    (`column_list`, `synced_block`) leaves the line above it still open.

    ``md_blocks.starts_block`` is the shared definition of what opens a block,
    so this side and the reading side cannot drift: a line the parser would
    treat as a continuation is exactly a line a paragraph written next would be
    absorbed into.
    """
    return bool(lines) and bool(lines[-1].strip()) and not starts_block(lines[-1])


#: What `md_link` steps over when escaping a label's `]`: a code span (its
#: content is literal, the converter consumes it whole) or an escape pair. The
#: same two units the converter's label scan steps over (`md_blocks._scan_opaque`).
_LABEL_UNIT = re.compile(r"``(?:[^`]|`(?!`))+``|`[^`]*`|\\.")


def md_link(text, href, literal=False):
    """A markdown link whose label cannot end early: the label runs to the first
    unescaped `]`, so one inside the text would leave the rest as loose prose.

    ``literal`` is for a label that is a whole code span, where a backslash is
    content rather than an escape. Nothing is escaped there; the converter reads a
    code span inside a label whole instead, delimiters and all. A code span
    *among other spans* in the label gets the same treatment piecewise: the
    converter consumes it whole, so a `]` inside it never ends the label — and
    a backslash written before it would be read back as content, which is how
    ``a]b`` in linked code came back as ``a\\]b``.

    The scan consumes escape pairs whole, exactly as the converter's label rule
    does: the label is already ``escape_md``'d, so a plain run's backtick sits
    in it as ``\\` `` — treating that backtick as a code-span *opener* pairs it
    with the next real delimiter, and a ``]`` inside the phantom span then goes
    unescaped and ends the label early, turning the link into prose."""
    if not literal:
        out, pos = [], 0
        for m in _LABEL_UNIT.finditer(text):
            out.append(text[pos:m.start()].replace("]", r"\]"))
            out.append(m.group(0))
            pos = m.end()
        out.append(text[pos:].replace("]", r"\]"))
        text = "".join(out)
    return f"[{text}]({href})"


#: Outermost first — the order the wrappers nest in, and the order the markers are
#: applied from the outside in. `md_blocks` peels them in any order, so what this
#: fixes is only that one rendering is chosen and kept.
_WRAPPERS = (("underline", "<u>{}</u>"), ("strikethrough", "~~{}~~"),
             ("italic", "*{}*"), ("bold", "**{}**"))


def _wrap(text, template):
    """Wrap the whitespace-trimmed core, leaving the surrounding spaces outside.

    `**text **` violates CommonMark's flanking rule and renders literally, and a
    label of ` **x** ` splits into three runs that re-render as three links.

    A strikethrough core's edge tildes are escaped here rather than in
    `escape_md`, because only the wrap site knows a delimiter is about to land
    flush against them: the escaper escapes tilde *pairs* wherever they occur,
    but a lone content `~` at the core's edge merges into the `~~` delimiter
    (`~~x~~~` reads back as strike("x") plus a stray tilde) and no per-run rule
    can see that coming."""
    core = text.strip()
    if not core:
        return text
    if template == "~~{}~~":
        if core.startswith("~"):
            core = "\\" + core
        if core.endswith("~") and not _is_escaped(core, len(core) - 1):
            core = core[:-1] + "\\~"
    lead = text[:len(text) - len(text.lstrip())]
    return lead + template.format(core) + text[len(text.rstrip()):]


def _leaf(rt):
    """One item's own text: escaped, and code-wrapped if it is a code span.

    A code span is per item and innermost — its content is literal, so two adjacent
    code items are two spans, and CommonMark gives a backslash no meaning inside
    one, which is why an escape there would be a backslash the reader sees.

    ``_brackets`` is attached per item by `rich_md`: a bare `[` here pairs with the
    `](` of a *later* item's link, which a per-item rule cannot see by itself."""
    if (rt.get("annotations") or {}).get("code"):
        text = rt.get("plain_text", "")
        # a backtick in the content takes CommonMark's long-fence form: one
        # more backtick than the content's longest run, space-padded so the
        # content may itself start or end with a backtick; the converter
        # strips exactly that padding on the way back in
        runs = re.findall(r"`+", text)
        if runs:
            fence = "`" * (1 + max(len(run) for run in runs))
            return _wrap(text, fence + " {} " + fence)
        return _wrap(text, "`{}`")
    text = escape_md(rt.get("plain_text", ""), brackets=rt.get("_brackets", False))
    # An edge `~` beside a strikethrough neighbour merges into that span's `~~`
    # delimiter — `~x` before strike("y") renders `~x~~y~~`, which reads back as
    # one strike whose text is `x`. `escape_md` escapes only tilde *pairs* and
    # cannot see the neighbour; `rich_md` can, and marks the item, exactly as it
    # does for a later run's link brackets. Applied to the *escaped* text, or
    # the escaper would treat the backslash as content and double it.
    if rt.get("_lead_tilde") and text[:1] == "~":
        text = "\\" + text
    if rt.get("_trail_tilde") and text[-1:] == "~" and not _is_escaped(text, len(text) - 1):
        text = text[:-1] + "\\~"
    return text


def _outermost(rt, done, order):
    """The next wrapper still owed to this item, outermost first, or ``None``."""
    a = rt.get("annotations") or {}
    for name, _template in order:
        if name not in done and a.get(name):
            return name
    return "href" if rt.get("href") and "href" not in done else None


def _shares(rt, name, done):
    if name == "href":
        return "href" not in done and bool(rt.get("href"))
    return name not in done and bool((rt.get("annotations") or {}).get(name))


def _render_span(items, done, order=_WRAPPERS):
    """Consecutive rich-text items as markdown, wrappers nested rather than tiled.

    Notion splits one span across as many items as it likes — a bold sentence with
    an inline-code word in it arrives as three bold items, and an italic word inside
    a bold sentence as three more. Wrapping each item on its own puts `*` against
    `*` where two of them meet, and markdown reads that boundary somewhere else on
    the way back, so the body never matches itself: every push after the first
    reports a row nobody has touched as changed. Wrapping what the items *share*,
    once, and recursing on what they do not, is what makes the rendering a fixed
    point.
    """
    if not items:
        return ""
    for name, template in order:
        if name not in done and all((i.get("annotations") or {}).get(name) for i in items):
            return _wrap(_render_span(items, done | {name}, order), template)
    href = items[0].get("href") or ""
    if href and "href" not in done and all(i.get("href") == href for i in items):
        inner = _render_span(items, done | {"href"}, order)
        core = inner.strip()
        if not core:
            return inner
        # a label that is one whole code span is literal: a backslash in it is
        # content, and the converter reads the span whole rather than stopping at
        # the `]` inside it
        literal = len(items) == 1 and bool((items[0].get("annotations") or {}).get("code"))
        lead = inner[:len(inner) - len(inner.lstrip())]
        return lead + md_link(core, href, literal=literal) + inner[len(inner.rstrip()):]
    if len(items) == 1:
        return _leaf(items[0])
    # Nothing is owed by *every* item, so take whichever wrapper the first item is
    # owed that its neighbours share *furthest*, and let it carry that far.
    # Splitting on the exact set instead would separate a bold link from the bold
    # full stop after it, and the two `**` would meet — the boundary this whole
    # function exists to avoid. Furthest-shared rather than a fixed order for the
    # same reason: a bold-italic run beside a bold run used to split on the
    # italic (the fixed order's first pick), rendering `***X*****Y**` — five
    # stars nothing can read back — where sharing the bold writes `***X*Y**`.
    best_name, best_end = None, 1
    for name in [wrapper for wrapper, _template in order] + ["href"]:
        if not _shares(items[0], name, done):
            continue
        end = 1
        while end < len(items) and _shares(items[end], name, done) and (
                name != "href" or items[end].get("href") == items[0].get("href")):
            end += 1
        if best_name is None or end > best_end:
            best_name, best_end = name, end
    if best_name is None:
        return _leaf(items[0]) + _render_span(items[1:], done, order)
    return (_render_span(items[:best_end], done, order)
            + _render_span(items[best_end:], done, order))


#: The wrapper priorities `rich_md` may try. The first is the canonical order;
#: the rest move which wrapper is shared across a group, and with it where two
#: spans' delimiters end up flush — the only lever a renderer has over an
#: ambiguous seam, since the text itself may not change.
#: The last resort: tags for every wrapper, `<s>` included. Tags cannot
#: collide — no delimiter run is involved, a literal tag in content is escaped,
#: and the renderer nests them properly — so the all-tag orders are structurally
#: readable whatever the content. Reached only when every star spelling of a
#: group fails to read back: a seam where one span's close and the next one's
#: open merge into a run that pairs differently than meant, which is ambiguous
#: to CommonMark itself, so no cleverer star spelling exists to find.
_HTML_WRAPPERS = (("underline", "<u>{}</u>"), ("strikethrough", "<s>{}</s>"),
                  ("italic", "<i>{}</i>"), ("bold", "<b>{}</b>"))
_ORDERS = ((_WRAPPERS,)
           + tuple(_WRAPPERS[k:] + _WRAPPERS[:k] for k in range(1, len(_WRAPPERS)))
           + (_HTML_WRAPPERS,)
           + tuple(_HTML_WRAPPERS[k:] + _HTML_WRAPPERS[:k]
                   for k in range(1, len(_HTML_WRAPPERS))))


def _signature_of(items):
    """[(char, annotations, href)] over non-whitespace characters, plus a marker
    per equation. Whitespace is skipped because styling on it has no rendering
    (`_wrap` trims a group's core), which is by design, not a round-trip loss."""
    out = []
    for it in items or []:
        if it.get("type") == "equation":
            out.append(("=", (it.get("equation") or {}).get("expression", ""), None))
            continue
        text = it.get("plain_text")
        if text is None:
            text = (it.get("text") or {}).get("content", "")
        href = it.get("href") or ((it.get("text") or {}).get("link") or {}).get("url")
        annots = frozenset(k for k, v in (it.get("annotations") or {}).items() if v)
        out += [(ch, annots, href) for ch in text if not ch.isspace()]
    return out


def _strikes(item):
    return bool((item.get("annotations") or {}).get("strikethrough"))


def _render_all(items, order):
    out, index = [], 0
    while index < len(items):
        if items[index].get("type") == "equation":  # plain_text is the bare expression
            data = items[index].get("equation") or {}
            expr = data.get("expression", items[index].get("plain_text", ""))
            out.append(f"{md_blocks.EQ_OPEN}{expr}{md_blocks.EQ_CLOSE}")
            index += 1
            continue
        end = index
        while end < len(items) and items[end].get("type") != "equation":
            end += 1
        out.append(_render_span(items[index:end], frozenset(), order))
        index = end
    return "".join(out)


def rich_md(rts):
    raw = list(rts or [])
    # Whether to escape brackets is decided per item, ahead of any *later* item's
    # link, so the answer is attached before the span tree is built rather than
    # threaded through it. Escaping every `[` instead would put a backslash in
    # front of every `[note]` in a paragraph that happens to contain a link.
    items = []
    for i, item in enumerate(raw):
        before, after = raw[i - 1] if i else None, raw[i + 1] if i + 1 < len(raw) else None
        items.append(dict(
            item,
            _brackets=any(r.get("href") for r in raw[i + 1:]),
            _lead_tilde=bool(before is not None and _strikes(before) != _strikes(item)),
            _trail_tilde=bool(after is not None and _strikes(after) != _strikes(item)),
        ))

    # The rendering is *verified*, not assumed: a run that merges one span's
    # close with the next one's open can pair differently than intended under
    # any delimiter-run algorithm, this parser's and CommonMark's alike, and no
    # local rule at the seam can tell. So render, read the result back with the
    # real parser, and keep the first spelling that reproduces the runs. The
    # alternatives are the same content grouped under a different wrapper
    # priority, which moves where the seams fall; `_WRAPPERS` order comes first,
    # so the common case is unchanged and costs one extra parse.
    want = _signature_of(items)
    first = None
    for order in _ORDERS:
        rendered = _render_all(items, order)
        if first is None:
            first = rendered
        if _signature_of(md_blocks.inline_md(rendered)) == want:
            return rendered
    return first
