"""markdown -> Notion rich_text and blocks.

Moved out of `notes/infra-task-triage/proposals/push.py` (2026-08-10) and taught
the block types the mirror renders but the old converter could not read back.

Every parser matches the exact string the renderers beside it emit for that block
type (`walker.py` at the block tier, `richtext.py` at the span tier) — the two are a round trip, so a shape it emits and this
cannot read is a page that gets corrupted on push.

**The keep-sentinel decides, and `standin_extent` checks it.** A line the renderer
writes for a block the converter cannot re-derive on its own carries a sentinel naming
that block's type, and this converter answers with what that type renders as — one
line for most, a `$$` fence for an equation, a bracketed region for a container the
renderer walks through. Neither half alone is enough: trusting the type would let a
sentinel that drifted onto prose swallow it, and matching the line alone cannot tell a
`bookmark` from an `embed` from a `link_preview`, which render identically.

Three dispositions, and every type has exactly one:

- **Rebuilt** — the line carries the whole block, so a replace deletes it and writes it
  back where it stood: externally hosted media, and (sentinelled) `bookmark` and
  `embed`. A bookmark's caption does not survive, the renderer writing the URL alone,
  which `push` reports before it writes.
- **Kept** — the block exists only inside Notion or the API cannot create it, so the
  pair is stepped over and the block stays: sub-pages, databases, page links,
  Notion-hosted media, `equation`, `table_of_contents`, `template`, `link_preview`,
  the region containers, and any type this renderer has no branch for.
- **Refused** — a `- 🔖`/`- 🖼️`/`- 👁️` stand-in with no sentinel to prove a block is
  really there, a sentinel over a line its type does not render, an unclosed region,
  the mirror's own diagnostics, nesting past two levels.

A bare `[url](url)` is now an ordinary paragraph. It used to refuse, because a
bookmark, an embed and a link preview all rendered as exactly that and nothing said
which; the renderer marks all three now, so the only thing left wearing that shape is
a link someone typed.

A quote or callout is a *region* of `> `-prefixed lines: its own text first, a
bare `>` as the seam, then its children rendered in full below — the structure
markdown blockquotes always had, and which this converter used to flatten into
indistinguishable sibling quotes. Sibling regions are separated by a truly
blank line, exactly as sibling paragraphs are. `pull` still declines to
*sentinel* blocks inside a region, so a kept-only child there (a sub-page, a
hosted image) renders as its bare stand-in and the interior parse refuses it
loudly — safe, and mendable from `pull`'s side now that the structure carries.

Both directions are the identity on that surface, and both halves are needed for
it: `escape_md` here writes plain text that cannot be misread as a marker, and
`rich_md` there calls it before wrapping a run in any. A literal `*Topic search*`
someone typed in Notion is escaped on the way out and unescaped on the way back,
instead of being read as emphasis and rewritten into the page as italics.

Two adjacent lines used to be ambiguous — two sibling paragraphs and one
soft-wrapped paragraph are written the same way in GFM, so the second block was
read back into the first. The renderer now puts a blank line between consecutive
paragraphs; hand-written soft wrapping still merges, which is what an author
means by it.

Every shape the renderer emits reads back as the blocks and runs it came from.
The fuzz suite beside this file asserts that, and shields exactly two things,
each for a reason it names at the shielding site: an **empty paragraph**, which
is a Notion spacer and renders as a blank line with nothing to rebuild it from
(the one entry in `test_md_blocks.LOSSES`), and text that **ends a line in
whitespace**, which no markdown line can carry — every reader rstrips it, and so
would any editor that touched the file.
Getting there took a spelling for each ambiguity markdown left open, and the
pairs below are the contract — a marker written on one side and not read on the
other is a page that gets corrupted on push:

- Emphasis is paired with CommonMark's **delimiter-stack** algorithm
  (`_pair_stars`), so adjacent spans, chains and structured boundaries resolve
  globally rather than by a local guess. Where even that is ambiguous — a run
  merging one span's close with the next one's open, which CommonMark itself
  reads either way — `rich_md` **verifies its own output** against this parser
  and falls back to the `<b>`/`<i>` spelling, which no star run can collide
  with.
- A **code run holding a backtick** takes CommonMark's long fence: one more
  backtick than the content's longest run, space-padded, read back at any
  length (`_code_span_end`).
- A **paragraph whose text starts with a block marker** carries one backslash
  (`escape_leading_marker`); a **one-line block whose text starts with another
  type's marker** — a bullet reading `[x] `, a quote reading as a callout icon
  — carries one too (`escape_inner_marker`). Both strip exactly one on the way
  back, so text that genuinely starts with a backslash keeps its own.
- An **inline equation** is carried by an `<eq>` tag. `$expr$` was the older
  spelling and could not be read back: prose is full of dollar pairs
  (`$250k / 20 = ~$12.5k`), and escaping every `$` that might pair would have
  rewritten them across the mirror to buy a rare block type.
- A **code block's caption** is its own marked line under the fence
  (`CAPTION_MARK`), reattached to the block rather than left as a paragraph.
- **Sibling tables** are separated by a blank line, and a **content tilde** at
  the edge of a strikethrough span — the run's own, or a neighbouring run's —
  is escaped so it cannot merge into the `~~` delimiter.
- A **quote or callout renders as a region** (see above): text, a bare-`>`
  seam, children; the region reader reassembles all three.

One backstop remains, and it is crash-safety rather than a known shape: a
rendering **no candidate spelling verifies** makes `rich_md` return its first
attempt. The final candidates spell every wrapper as a tag (`<b>`, `<i>`,
`<s>`, `<u>`), which cannot collide — no delimiter run is involved, a literal
tag in content is escaped, and nesting is proper — so nothing is known or
expected to reach the fallback, and the fuzz has not produced such a span.

A literal marker *inside* an emphasis span is the one ambiguity markdown itself
cannot resolve: `*"a 'slug*' b"*` closes the emphasis at `slug`, in this parser and
in every CommonMark one. It stops mattering once a body has been pulled, because
the renderer escapes a literal marker on the way out.
"""
import re
import unicodedata

CODE_LANGS = {"py": "python", "python": "python", "js": "javascript", "javascript": "javascript",
              "sh": "bash", "bash": "bash", "json": "json", "yaml": "yaml", "sql": "sql", "": "plain text"}

# ---------------- markdown -> Notion rich_text / blocks ----------------
#: What a backslash escapes. Only these: a backslash before anything else stays a
#: literal backslash, so a regex or a Windows path in prose keeps its own spelling
#: — except where the escaper has to protect the *next* character, which is why
#: `C:\temp` renders unchanged but `C:\*` renders as `C:\\\*`.
ESCAPABLE = "\\*`~<[]"

#: A backslash is escaped only where the unescaper would otherwise eat it — before
#: another escapable character, before a line break, or at the end of the run, where
#: the next character is a delimiter this converter wrote rather than content. The
#: end includes trailing whitespace, because `rich_md` wraps a run's *trimmed* core:
#: a backslash that was harmless mid-run ends up against the closing marker, and
#: `**path C:\ **` is then read as an italic run and the backslash is lost.
_ESCAPE_BODY = r"""
    \\(?=[\\*`~<\[\]\n]|\s*\Z)
  | \*
  | `
  | ~(?=~)                    # strikethrough opens on the pair, so a lone ~ is prose
  | <(?=/?(?:[bius]|eq)>)      # the renderer's own tags, not every angle bracket
  | \[{bracket}
"""
#: Four spellings of one pass, never two passes. A second pass over an
#: already-escaped string cannot tell the backslash it wrote for a literal
#: backslash from one written for the bracket after it, so it leaves that bracket
#: bare — and a link in a later run then swallows it.
#:
#: `bracket` widens one arm from "only where this run alone is ambiguous" to
#: "always", for the case a per-run rule cannot see: a `[` pairs with the `](`
#: of a *later* run's link.
_ESCAPE_RES = {
    brackets: re.compile(
        _ESCAPE_BODY.format(bracket="" if brackets else r"(?=[^\]\n]*\]\()"),
        re.VERBOSE)
    for brackets in (False, True)
}
_UNESCAPE_RE = re.compile(r"\\([\\*`~<\[\]])")
def escape_md(text, brackets=False):
    """Plain text as markdown that parses back to exactly this text.

    The inverse of ``unescape_md``, and the reason a Notion paragraph reading
    ``*Topic search*`` survives a pull-push round trip instead of coming back as
    italics — or, before this existed, as literal asterisks Notion then showed to
    everyone. Minimal on purpose: only what the inline grammar below would misread
    is escaped, so the mirror stays readable.

    ``brackets`` escapes every ``[``, not only one already followed by a link's
    ``](``. A caller passes it when some *other* run of the same paragraph carries
    a link, because that run's ``](`` is what a bare bracket here would pair with —
    a per-run rule cannot see it, and the two runs together read as one long link.
    """
    return _ESCAPE_RES[bool(brackets)].sub(lambda m: "\\" + m.group(0), text)


def unescape_md(text):
    return _UNESCAPE_RE.sub(r"\1", text)


def rich(text, annotations=(), link=None):
    out = []
    for chunk in (text[i:i+1990] for i in range(0, len(text), 1990)):
        t = {"type": "text", "text": {"content": chunk}}
        if link: t["text"]["link"] = {"url": link}
        if annotations: t["annotations"] = {a: True for a in annotations}
        out.append(t)
    return out


#: The inline tier: a tokenizer plus CommonMark's delimiter-stack emphasis
#: pairing. The star family used to be parsed with close-hunting regexes, and
#: every hard bug in this file's history was that architecture hitting its
#: wall: a lazy close taking the wrong star of a run, a lookahead matching a
#: fake span across intermediate content, an atomic unit inflated by
#: backtracking. Pairing delimiter *runs* globally — the algorithm CommonMark
#: specifies — is exact where every local lookahead was approximate, so
#: adjacent spans with code spans, links or nested wrappers at the boundary
#: (`**A *[x](u)***~~s~~`) now parse instead of degrading to literal stars.
#:
#: The containers — code spans, `~~`, `<u>…</u>`, links — are consumed whole
#: during tokenizing and their bodies parsed in their own context, so a
#: delimiter inside one can never pair with one outside, matching how the
#: renderer nests wrappers. A code span inside any container body is opaque:
#: its content is literal, so a delimiter inside it closes nothing — which is
#: why the container closers are found with `_scan_opaque`, a walk that steps
#: over escape pairs and whole code spans rather than a character class that
#: could step over a bare backtick and land inside one.
_BT_RUN = re.compile(r"`+")
_STAR_RUN = re.compile(r"\*+")
#: The tag containers, and the annotation each one carries.
_TAG_ANNOT = {"u": "underline", "b": "bold", "i": "italic", "s": "strikethrough"}
#: An inline equation. `$expr$` was the old spelling and could not be read back:
#: prose is full of dollar pairs (`$250k / 20 = ~$12.5k`), and escaping every
#: `$` that might pair would rewrite them across the whole mirror. A tag cannot
#: collide — a literal `<eq>` in prose is escaped like any other of the
#: renderer's own tags — so the expression is carried by one.
EQ_OPEN, EQ_CLOSE = "<eq>", "</eq>"
_URL_TAIL = re.compile(r"\(([^)]+)\)")

#: The characters a token can start with. Nothing else opens anything, so
#: prose between candidates is skipped in one search rather than a character
#: at a time.
_CANDIDATE = re.compile(r"[\\*`~<\[]")


def _code_span_end(s, i):
    """(end, content) for the code span opening at ``i``, or (-1, "").

    CommonMark's fence arithmetic with one dialect widening: the close is the
    first backtick run of length **at least** the opening fence, its leftmost
    ``n`` backticks consumed and the rest left for the next token. Exactly-n
    (CommonMark's rule) breaks the renderer's own seams — two adjacent code
    spans of different styles put close and open flush, and `` ``x```y` ``'s
    three-run is a two-close plus a one-open. The widening is sound for
    renderer output because a fence is always one longer than the content's
    longest run, so the first long-enough run really is the close.
    """
    n = _BT_RUN.match(s, i).end() - i
    j = i + n
    while True:
        k = s.find("`", j)
        if k <= i:
            return -1, ""
        run_end = k
        while run_end < len(s) and s[run_end] == "`":
            run_end += 1
        if run_end - k >= n and k > i + n:
            return k + n, s[i + n:k]
        j = run_end


def _scan_opaque(s, i, end_token):
    """Index of ``end_token`` at or after ``i``, escape pairs and whole code
    spans stepped over, or -1. The body between must be non-empty."""
    start = i
    while i < len(s):
        if s.startswith(end_token, i):
            return i if i > start else -1
        ch = s[i]
        if ch == "\\":
            i += 2
        elif ch == "`":
            end, _content = _code_span_end(s, i)
            i = end if end != -1 else i + 1
        else:
            i += 1
    return -1


def _is_escaped(s, i):
    """Whether ``s[i]`` is preceded by an odd run of backslashes."""
    j = i - 1
    while j >= 0 and s[j] == "\\":
        j -= 1
    return (i - 1 - j) % 2 == 1


def _flanking(s, start, end):
    """(can_open, can_close) for the star run ``s[start:end]``.

    Whitespace flanking only: the run opens when a non-space follows it and
    closes when a non-space precedes it, string edges counting as whitespace.
    This is what keeps `2 * 3 * 4 = 24` prose — space-flanked runs neither
    open nor close, and without that the stars paired up and the characters
    were *deleted* from what the push writes — while still letting an
    intraword star open, which is how a human types `*a*b`.

    Deliberately not CommonMark's full rule: its punctuation clause (a run
    after punctuation cannot close before a letter) rejects what the renderer
    itself emits — ``*`code`*e`` is italic-code flush against prose, and the
    close star sits between a backtick and a letter. The renderer has only
    ever guaranteed whitespace flanking (`_wrap` trims the core), so that is
    the dialect the parser reads.
    """
    before = s[start - 1] if start > 0 else " "
    after = s[end] if end < len(s) else " "
    return not after.isspace(), not before.isspace()


class _Delim:
    """One star run: original length for the mod-3 rule, a live count the
    pairing spends, and the open/close events pairing records against it."""

    __slots__ = ("count", "orig", "can_open", "can_close", "opens", "closes")

    def __init__(self, count, can_open, can_close):
        self.count = count
        self.orig = count
        self.can_open = can_open
        self.can_close = can_close
        self.opens = []
        self.closes = []


def _tokenize(s):
    """[(kind, …)] tokens: ("text", raw), ("code", lead, core, trail),
    ("cont", annotations, body, href) for the containers, and _Delim runs.

    An escape pair stays inside its text token — `unescape_md` resolves it at
    emit — which is what keeps `\\[a](b)` prose and an escaped star content."""
    tokens, last, i, n = [], 0, 0, len(s)

    def flush(upto):
        if upto > last:
            tokens.append(("text", s[last:upto]))

    while i < n:
        hit = _CANDIDATE.search(s, i)
        if hit is None:
            break
        i = hit.start()
        ch = s[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "*":
            m = _STAR_RUN.match(s, i)
            flush(i)
            tokens.append(_Delim(m.end() - m.start(), *_flanking(s, i, m.end())))
            last = i = m.end()
            continue
        if ch == "`":
            # `_code_span_end` carries the fence arithmetic. The renderer
            # space-pads any multi-backtick form, and exactly that padding is
            # stripped here; nothing is stripped from the single-backtick
            # spelling, whose human-typed edge spaces have always hoisted.
            #
            # Boundary whitespace is hoisted out of the annotation into plain
            # runs, because that is how Notion stores it: writing
            # code("[AI] ") reads back as code("[AI]") + " ", and parsing to
            # any other shape makes a correct push compare as a mismatch
            # (observed live 2026-08-16). A code span's content is literal
            # all the way down: no marker inside it opens anything, and a
            # backslash is a backslash — which is why the renderer does not
            # escape one on the way out either.
            end, body = _code_span_end(s, i)
            if end != -1:
                n_open = _BT_RUN.match(s, i).end() - i
                if n_open > 1 and body[:1] == " " and body[-1:] == " " and body.strip():
                    body = body[1:-1]
                core = body.strip()
                lead = body[:len(body) - len(body.lstrip())]
                flush(i)
                tokens.append(("code", lead, core, body[len(lead) + len(core):]))
                last = i = end
                continue
            i += _BT_RUN.match(s, i).end() - i
            continue
        elif ch == "~":
            if s.startswith("~~", i) and i + 2 < n and not s[i + 2].isspace():
                close = _scan_opaque(s, i + 2, "~~")
                # the close must not sit after whitespace — the same guard the
                # renderer's trimmed-core wrapping upholds — so slide forward
                # over any that does
                while close != -1 and s[close - 1].isspace():
                    close = _scan_opaque(s, close + 1, "~~")
                if close != -1:
                    flush(i)
                    tokens.append(("cont", ("strikethrough",), s[i + 2:close], None))
                    last = i = close + 2
                    continue
        elif ch == "<":
            # `<b>`/`<i>` are the unambiguous spelling `rich_md` falls back to
            # when a star seam cannot be read back; `<u>` has always been the
            # only spelling for underline, which markdown has no marker for.
            if s.startswith(EQ_OPEN, i):
                close = _scan_opaque(s, i + len(EQ_OPEN), EQ_CLOSE)
                if close != -1:
                    flush(i)
                    tokens.append(("eq", s[i + len(EQ_OPEN):close]))
                    last = i = close + len(EQ_CLOSE)
                    continue
            tag = s[i + 1:i + 2]
            if s[i + 2:i + 3] == ">" and tag in _TAG_ANNOT:
                close = _scan_opaque(s, i + 3, f"</{tag}>")
                if close != -1:
                    flush(i)
                    tokens.append(("cont", (_TAG_ANNOT[tag],), s[i + 3:close], None))
                    last = i = close + 4
                    continue
        elif ch == "[":
            close = _scan_opaque(s, i + 1, "]")
            m = _URL_TAIL.match(s, close + 1) if close != -1 else None
            if m:
                flush(i)
                tokens.append(("cont", (), s[i + 1:close], m.group(1)))
                last = i = m.end()
                continue
        i += 1
    flush(n)
    return tokens


def _pair_stars(tokens):
    """CommonMark's process-emphasis over the star runs, in place.

    Closers scan the opener stack top-down; a pairing spends 2 stars from each
    side when both have 2 (strong), else 1 (em); openers between the pair are
    dropped (nothing can pair across an emphasis span); the mod-3 rule keeps a
    run that can both open and close from splitting where the spec says the
    boundary is ambiguous. Whatever never pairs stays literal stars.
    """
    openers = []
    for tok in tokens:
        if not isinstance(tok, _Delim):
            continue
        if tok.can_close:
            j = len(openers) - 1
            while tok.count > 0 and j >= 0:
                o = openers[j]
                if ((o.can_close or tok.can_open)
                        and (o.orig + tok.orig) % 3 == 0
                        and not (o.orig % 3 == 0 and tok.orig % 3 == 0)):
                    j -= 1
                    continue
                use = 2 if o.count >= 2 and tok.count >= 2 else 1
                o.opens.append(use)
                tok.closes.append(use)
                o.count -= use
                tok.count -= use
                del openers[j + 1:]
                if o.count == 0:
                    del openers[j]
                    j = len(openers) - 1
        if tok.count > 0 and tok.can_open:
            openers.append(tok)


def _inline(s, annotations, link):
    tokens = _tokenize(s)
    _pair_stars(tokens)
    out, stack = [], []

    def active():
        return annotations + tuple(a for tup in stack for a in tup)

    for tok in tokens:
        if isinstance(tok, _Delim):
            # source order within one run: its leftmost stars close (pairing
            # spends inner ends first), unpaired stars stay literal in the
            # middle, its rightmost stars open — and multiple opens apply
            # outer-first, i.e. in reverse pairing order, because the later,
            # wider pairing wraps the earlier one (`***a* b**`).
            for _use in tok.closes:
                if stack:
                    stack.pop()
            if tok.count:
                out += rich("*" * tok.count, active(), link)
            for use in reversed(tok.opens):
                stack.append(("italic",) if use == 1 else ("bold",))
            continue
        kind = tok[0]
        if kind == "eq":
            eq = {"type": "equation", "equation": {"expression": tok[1]}}
            if active():
                eq["annotations"] = {a: True for a in active()}
            if link:
                eq["href"] = link
            out.append(eq)
        elif kind == "text":
            out += rich(unescape_md(tok[1]), active(), link)
        elif kind == "code":
            _kind, lead, core, trail = tok
            if lead:
                out += rich(lead, active(), link)
            if core:
                out += rich(core, active() + ("code",), link)
            if trail:
                out += rich(trail, active(), link)
        else:  # a container: its body parses in its own context, under
            #    whatever emphasis is active at this position outside it
            _kind, kinds, body, href = tok
            out += _inline(body, active() + kinds, href or link)
    return out


def inline_md(s):
    return _merge_runs(_inline(s, (), None)) or rich("")


def _merge_runs(runs):
    """Adjacent runs with the same annotations and link fold into one.

    The code-span branch hoists boundary whitespace into plain runs beside the span,
    which leaves ``" "`` sitting next to ``" properties"`` — two runs where Notion
    stores one, so a round trip would read back as a different rich-text shape.
    Chunked runs are left alone: two same-style neighbours are only merged when the
    result still fits in one ``rich`` chunk, so a 4,000-char paragraph keeps the
    split Notion requires."""
    out = []
    for run in runs:
        prev = out[-1] if out else None
        if (prev is not None and run.get("type") == "text"
                and prev.get("type") == "text"
                and run.get("annotations") == prev.get("annotations")
                and run["text"].get("link") == prev["text"].get("link")
                and len(prev["text"]["content"]) + len(run["text"]["content"]) <= 1990):
            prev["text"]["content"] += run["text"]["content"]
            continue
        out.append(run)
    return out


MAX_NESTING = 2          # Notion accepts two levels of nested children per request
CHUNK = 90               # blocks per create/append request

ID32 = r"[0-9a-f]{32}"
KEEP_RE = re.compile(r"^<!--\s*notion:keep block=(" + ID32 + r") type=(\w+)\s*-->$")
KEEP_END_RE = re.compile(r"^<!--\s*notion:keep-end block=(" + ID32 + r")\s*-->$")
#: Containers `walker.py:191` renders no line for, walking through to their children
#: instead. A sentinel naming one opens a *region* closed by `KEEP_END_RE` with the same
#: id: everything between is the container's own content, already on the page, and
#: re-emitting it wrote a second copy on every push.
REGION_TYPES = {"column_list", "synced_block"}
# Blocks the renderer emits *nothing* for and that carry no region markers either
# (walker.py:186-187,191-192). With no line there is no stand-in to sentinel, and no way
# to tell from markdown that one was there at all; only a live child-type check can see them.
UNRENDERED_TYPES = {"column", "breadcrumb"}
DIAGNOSTICS = {  # the mirror's own notes to the reader, not page content
    "<!-- child blocks inaccessible here: HTTP":
        "the mirror's diagnostic for a container it could not read (walker.py:102)",
    "<!-- unhandled block type:":
        "the mirror's diagnostic for a block type it does not render (walker.py:203)",
}
# Stand-in lines. The fuller catalogue of historical mirror forms is in coverage_census.py.
STANDIN = re.compile(r"^- [📄🗄🔗🔖🖼👁📑🧩]️?(?: |$)")   # an untitled page renders as `- 📄 `
#: The three url-only blocks, as `walker.URL_BLOCK_MARK` writes them: a marker saying
#: which of the three, then the caption if it has one and the URL if it does not. The
#: two halves are one definition — a marker added there and not here is a page that
#: gets corrupted on push.
URL_BLOCK_RE = re.compile(r"^- ([🔖🖼👁])️? \[(.*)\]\((\S+)\)$")
URL_BLOCK_KIND = {"🔖": "bookmark", "🖼": "embed", "👁": "link_preview"}
#: The marker `walker.py` writes for each of the three id-bearing stand-ins. Keyed per
#: type rather than tested against `STANDIN`, which matches *any* marked line: a
#: `child_page` sentinel over a table of contents would otherwise convert cleanly
#: instead of refusing as drifted, and the marker is what says which type a line is.
PAGE_MARK = {"child_page": "📄", "child_database": "🗄", "link_to_page": "🔗"}
MEDIA_KEPT = re.compile(r"^!\[[^\]]*\]\(ATTACH:" + ID32 + r"\)$")
#: The caption slot is non-empty on purpose. `walker.py:176` substitutes the block
#: type when a media block has no caption, so it never emits `![](url)` — a line in
#: that shape is literal markdown inside a paragraph's text (64 of them in the
#: mirror, all prose), and converting it would replace the paragraph with an image.
MEDIA_EXTERNAL = re.compile(r"^!\[([^\]]+)\]\((https?://[^)]+)\)$")
MEDIA_UNCAPTIONED = re.compile(r"^!\[\]\(https?://")
MEDIA_ANY = re.compile(r"^!\[[^\]]*\]\(")
#: `walker.py:176` renders a captionless media block as `![{block type}](url)`, so
#: these five words in the caption slot mean "no caption" rather than a caption.
MEDIA_TYPE_WORDS = frozenset({"image", "file", "pdf", "video", "audio"})
AUTOLINK = re.compile(r"^\[(\S+)\]\((\S+)\)$")
#: What a bookmark stand-in's target has to look like before it is sent. `AUTOLINK`
#: accepts any non-space token, and the renderer only ever writes a web URL there.
HTTP_URL = re.compile(r"https?://")
#: `flatten.md_cell` escapes a pipe inside a cell as `\|`, and splitting on it
#: anyway cuts the cell in two and grows the table by a column. The renderer pads
#: every real separator with spaces, so the character before one is never a
#: backslash of its own.
CELL_SPLIT = re.compile(r"(?<!\\)\|")
#: What starts a block rather than continuing the paragraph above it. `![` belongs
#: here because a media block gets no blank line of its own from the renderer:
#: without it a media line under prose is read as that paragraph's wrapped text,
#: and 410 of the mirror's 561 external-media lines sit exactly there.
#: The markers' trailing space is optional, because an *empty* block is just
#: its marker: a line that is only `-` or `#` starts a block, and a paragraph
#: that ran into one used to swallow it as a soft-wrapped continuation.
BLOCK_START = re.compile(
    r"^\s*(#{1,6}(?: |$)|-(?: |$)|\d+\.(?: |$)|>(?: |$)|\||```|\$\$|!\[|---\s*$|<)")
#: `BLOCK_START`'s `<` aims at `<details>` and `<!--`. Every other tag the
#: renderer writes — `<u>`, and the `<b>`/`<i>`/`<s>`/`<eq>` spellings — opens
#: a span inside a paragraph, so a line starting with one *continues* the
#: paragraph above it rather than beginning a block.
_INLINE_TAG_LEAD = re.compile(r"\s*<(?!details|!--)")


def starts_block(line):
    """Whether this line begins a block rather than continuing the text above.

    One definition, three readers: the paragraph continuation in `_flat`, the
    text of a quote/callout region, and `richtext.absorbs_a_paragraph` on the
    writing side. They have to agree — a line one of them calls a block and
    another calls a continuation is a body that changes shape on every pass.
    """
    return bool(BLOCK_START.match(line)) and not _INLINE_TAG_LEAD.match(line)

#: The line-leading escape, both halves. A Notion paragraph whose text *starts*
#: with a block marker (`- not a bullet`) used to come back as the block that
#: marker names — a silent type change on push. The renderer now prefixes one
#: backslash when a paragraph line opens with a marker, or with a backslash run
#: that itself precedes one (so content that genuinely starts `\- ` gains a
#: backslash and loses it again here, instead of losing its own); the parser
#: strips exactly one from any line in that shape. Backtick fences and `<` are
#: deliberately absent: a backslash before a fence re-forms a shorter fence
#: inside the text, and a `<`-leading paragraph already parses as a paragraph.
#: A `\*`-leading line — an escaped star, the commonest leading backslash in
#: real bodies — matches neither half and is untouched.
_LEAD_MARKER = r"(?:#{1,6}(?: |$)|-(?: |$)|\d+\.(?: |$)|>(?: |$)|\||\$\$|!\[|---\s*$)"
_LEAD_NEEDS_ESCAPE = re.compile(r"\\*" + _LEAD_MARKER)
_LEAD_ESCAPED = re.compile(r"\\(?=\\*" + _LEAD_MARKER + r")")


def escape_leading_marker(line):
    """The renderer's half: one backslash in front of a marker-opening line."""
    return "\\" + line if _LEAD_NEEDS_ESCAPE.match(line) else line


def _strip_lead_escape(line):
    return line[1:] if _LEAD_ESCAPED.match(line) else line


#: The same positional trick one level in, for the markers that live *inside* a
#: one-line block rather than at the start of the line. A bulleted item whose
#: own text begins `[x] ` re-read as a to-do, one beginning `▸ ` as a toggle,
#: and a quote whose text begins with an emoji-and-space (or a space) re-read as
#: a callout with that icon — each a silent type change on push. The renderer
#: prefixes one backslash, `_one_line_block` strips exactly one, and text that
#: genuinely starts with such a backslash gains and loses one more.
#:
#: The emoji arm carries no character class: `split_callout_icon` decides what
#: an icon is, so this asks it, and a backslash in front is enough to make it
#: answer "no icon" — the backslash then never reaches `unescape_md`, which
#: would have left it standing (an emoji is not escapable).
#: `📝` joins them because `CAPTION_MARK` is a bullet: a bulleted item whose own
#: text starts with it, written under a code block, was reattached as that
#: block's caption and vanished as a block.
_INNER_MARKER = re.compile(r"\[[ xX]\](?: |$)|▸(?: |$)|📝(?: |$)")


def _is_inner_ambiguous(text):
    return bool(_INNER_MARKER.match(text)) or split_callout_icon(text) is not None


def escape_inner_marker(text):
    """The renderer's half: one backslash in front of block-marker-shaped text."""
    probe = text.lstrip("\\")
    return "\\" + text if len(text) > len(probe) and _is_inner_ambiguous(probe) \
        or _is_inner_ambiguous(text) else text


def _strip_inner_escape(text):
    if text.startswith("\\") and _is_inner_ambiguous(text.lstrip("\\")):
        return text[1:]
    return text
CHILD_BEARING = {"paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item",
                 "numbered_list_item", "to_do", "toggle", "quote", "callout"}
TOC_LINE = "- 📑 (table of contents)"
#: `walker.CAPTION_MARK`, the line a code block's caption renders as. The two
#: halves are one definition: a marker written there and not read here is a
#: caption that comes back as a bullet.
CAPTION_MARK = "- 📝"
TEMPLATE_RE = re.compile(r"^- 🧩 \(template: .*\)$")
UNHANDLED_RE = re.compile(r"^<!-- unhandled block type: \w+ -->$")
#: Media types, as `walker.py:173` groups them.
MEDIA_TYPES = frozenset({"image", "file", "pdf", "video", "audio"})
#: Sentinelled types whose rendered line embeds the block's **own** id, so a sentinel
#: naming a different one has drifted. Deliberately partial: `link_to_page` renders the
#: id of its *target*, and a bookmark's URL can carry 32 hex of its own, so checking
#: either against the sentinel refuses correct bodies.
ID_IN_STANDIN = MEDIA_TYPES | {"child_page", "child_database"}


class Refused(Exception):
    """A line that cannot be converted without corrupting the page it came from.

    Every one of these is a shape the mirror emits but markdown cannot carry back:
    a block that survives only as a stand-in, a diagnostic comment, or nesting
    deeper than one request can express. Converting them anyway would duplicate a
    block, silently change its type, or publish collapsed content — all invisible
    until someone reads the page. Refusing names the line instead."""

    def __init__(self, line_no, line, reason):
        self.line_no, self.line, self.reason = line_no, line, reason
        super().__init__(f"line {line_no}: {reason}: {line.strip()!r}")


def chunks(blocks, size=CHUNK):
    """Split a block list into batches Notion accepts in one request.

    Both write paths need this. The create path used to send `children[:100]`,
    which silently dropped everything past the hundredth block of a long body."""
    return [blocks[i:i + size] for i in range(0, len(blocks), size)]


_EMOJI_JOINERS = {"️", "︎", "‍"}


def _is_emoji_char(ch):
    return (ch in _EMOJI_JOINERS or unicodedata.category(ch) in ("So", "Sk")
            or 0x1F000 <= ord(ch) <= 0x1FAFF)


def split_callout_icon(s):
    """`> ` content -> (icon, text) when the line is a callout, else None.

    `walker.py` renders a callout as `> {icon} {text}` and a quote as `> {text}`,
    so an emoji followed by a space is the callout signal — and since `icon` is
    the empty string when the callout has none, a leading space is one too. A
    bullet glyph like `•` is punctuation, not an emoji, and stays a quote."""
    if s.startswith(" "):
        return "", s[1:]
    n = 0
    while n < len(s) and _is_emoji_char(s[n]):
        n += 1
    if n and s[n:n + 1] in (" ", ""):
        return s[:n], s[n + 1:]
    return None


def _bookmark_url(t):
    m = AUTOLINK.match(t)
    return m.group(1) if m and m.group(1) == m.group(2) else None


#: The two url-only types a replace rebuilds from their stand-in rather than keeping:
#: the line carries everything the block is made of, so it can be deleted and put back
#: where it stood — the arrangement externally hosted media already has. `link_preview`
#: renders identically and is deliberately absent: the API cannot create one
#: (developers.notion.com/reference/block: "The API does not support creating or
#: appending `link_preview` blocks"), so it is kept in place instead.
REBUILT_TYPES = frozenset({"bookmark", "embed"})


def url_block(kind, label, url):
    """`- 🔖 [label](url)` as the block that marker names.

    Keeping these instead would drag them to the top of the page, since a replace can
    only append, and the line carries the whole block: which of the three it is, its
    URL, and its caption. The marker is what carries the first of those — all three
    used to render as a bare `[url](url)`, indistinguishable from each other and from
    a link someone typed — and the caption is what the old rendering dropped.

    ``label`` is the caption when the block has one and the URL itself when it does
    not, which is what the renderer writes and what keeps a captionless block from
    coming back with its own URL as a caption.
    """
    payload = {"url": url}
    if kind != "link_preview":
        payload["caption"] = [] if label == url else inline_md(label)
    return {"type": kind, kind: payload}


def external_media_block(caption, url):
    """`![cap](https://…)` as a Notion media block hosting the file externally.

    The only media form markdown carries whole: the bytes live at the URL, so the
    block can be deleted and rebuilt from this line alone. That is what keeps it out
    of `_standin_refusal` and, downstream, out of the pusher's mid-body refusal.

    The caption slot doubles as the block type when it holds one of the five words
    the renderer puts there for a block with no caption — the only way markdown can
    tell an external video from an external image, and exact for the captionless
    case, which is the one the renderer produces. Anything else is an image with
    that caption, because nothing in the line says otherwise.
    """
    placeholder = caption in MEDIA_TYPE_WORDS
    btype = caption if placeholder else "image"
    return {"type": btype, btype: {"type": "external", "external": {"url": url},
                                   "caption": [] if placeholder else inline_md(caption)}}


def standin_extent(kind, lines, j):
    """How many of ``lines[j:]`` are the stand-in ``walker.py`` writes for ``kind``, or 0.

    The table half of the round trip, and the reason a keep-sentinel can be *checked*
    rather than trusted: every entry names the branch of the renderer that writes it,
    and a sentinel whose covered lines do not match its own type has drifted off them.
    Trusting the type alone would let a drifted sentinel swallow a line of real prose;
    matching the line alone is what could not tell a bookmark from an embed.

    Returns a count rather than a bool because one block does not always render as one
    line: an ``equation`` is a ``$$`` fence around its expression.
    """
    line = lines[j].strip()
    if kind == "equation":
        if line != "$$":
            return 0
        for k in range(j + 1, len(lines)):
            if lines[k].strip() == "$$":
                return k - j + 1
        return 0                                  # unclosed: not what the renderer writes
    if kind in MEDIA_TYPES:
        # two forms, both hosted: `![cap](ATTACH:<id32>)` in a row body, and the local
        # filename the mirror's own tree carries once the file has been downloaded
        return 1 if MEDIA_KEPT.match(line) or (MEDIA_ANY.match(line)
                                               and not MEDIA_EXTERNAL.match(line)) else 0
    if kind in PAGE_MARK:
        return 1 if line.startswith(f"- {PAGE_MARK[kind]}") else 0
    if kind in URL_BLOCK_KIND.values():
        m = URL_BLOCK_RE.match(line)
        return 1 if m and URL_BLOCK_KIND[m.group(1)] == kind else 0
    if kind == "table_of_contents":
        return 1 if line == TOC_LINE else 0
    if kind == "template":
        return 1 if TEMPLATE_RE.match(line) else 0
    return 1 if UNHANDLED_RE.match(line) else 0   # a type the renderer has no branch for


def _refuse_if_buried(kind, covered_lines, ln, s):
    """Refuse a kept block that sits inside one the replace is going to delete.

    Deleting a block deletes its subtree, and the replace rebuilds the parent from
    markdown that carries no kept child — so a sub-page, a synced block or a hosted
    image nested under a toggle came off the page as a side effect of editing text
    somewhere else, silently, with only a post-write body mismatch to show for it.

    Indentation is the whole signal, and it is exact: every type that can hold children
    (`CHILD_BEARING`) is archivable, `pull` writes each sentinel at column 0 whatever
    the depth of the block it covers, so an indented *covered* line means a parent the
    replace deletes. A rebuilt block does not care — it is written back from this same
    markdown, under the rebuilt parent — which is why only kept ones are refused.
    """
    if kind in REBUILT_TYPES:
        return
    for line in covered_lines:
        if line.strip() and _indent(line):
            raise Refused(ln, s, f"a `{kind}` sits inside a block this push rewrites, and "
                                 "deleting that block would delete this one with it. Move it to "
                                 "the top level of the page in Notion, or make this edit in Notion")


def _standin_refusal(t):
    """The reason this line cannot be re-emitted as a block, or None.

    A keep-sentinel clears the ones that name a block id — including a bookmark,
    which `_flat` rebuilds from the line rather than skipping."""
    if URL_BLOCK_RE.match(t):
        return ("a bookmark, embed or link-preview stand-in with no keep-sentinel above "
                "it, so nothing proves it stands for a block that is really there")
    if MEDIA_KEPT.match(t):
        return "a Notion-hosted media block, not covered by a keep-sentinel"
    if MEDIA_ANY.match(t) and not MEDIA_EXTERNAL.match(t):
        if MEDIA_UNCAPTIONED.match(t):
            return ("a media line with an empty caption, which the renderer never writes — "
                    "a block with no caption renders as `![{type}](url)` — so this is "
                    "literal markdown inside a paragraph rather than a block of its own")
        return ("media downloaded into the mirror, which carries no block id, so no "
                "keep-sentinel can cover it and the file cannot be recreated from markdown")
    if t == TOC_LINE or TEMPLATE_RE.match(t):
        return ("a table-of-contents or template stand-in with no keep-sentinel above it, "
                "so nothing proves it stands for a block that is really there")
    if STANDIN.match(t):
        if re.search(ID32, t):
            return "a sub-page, database or page-link stand-in, not covered by a keep-sentinel"
        return ("a sub-page or database stand-in in the id-less legacy form "
                "(`- 🗄️ Title` with no id, as the first build's fetchers wrote it), "
                "which no keep-sentinel can cover")
    return None


def _scan_details(lines, i, offset=0):
    """(summary, inner lines, offset of the first inner line, index past `</details>`).

    Balanced, because the old converter split on the first `<details>` and the last
    `</details>` — which renders a nested toggle's whole body as literal text."""
    if not lines[i].lstrip().startswith("<details>"):
        raise Refused(offset + i + 1, lines[i], "a `<details>` tag with attributes — this converter "
                                                "reads the bare form the mirror and the proposals use")
    m = re.search(r"<summary>(.*?)</summary>", lines[i])
    summary = m.group(1) if m else "Details"
    trailing = lines[i][m.end():] if m else lines[i].split("<details>", 1)[-1]
    depth, j = 1, i + 1
    while j < len(lines):
        depth += lines[j].count("<details") - lines[j].count("</details>")
        if depth == 0:
            break
        j += 1
    if j >= len(lines):
        raise Refused(offset + i + 1, lines[i], "a `<details>` that is never closed, so everything "
                                                "after it would silently move inside the toggle")
    head = lines[j].split("</details>")[0]
    inner, offset = lines[i + 1:j], i + 1
    if head.strip():
        inner = inner + [head]
    if trailing.strip():
        inner, offset = [trailing] + inner, i
    return summary, inner, offset, j + 1


def _one_line_block(t, ln, s):
    """(type, text, extra fields) for the single-line block types, or None."""
    m = re.match(r"^- \[([ xX])\](?: (.*))?$", t)
    if m:
        return "to_do", _strip_inner_escape(m.group(2) or ""), {"checked": m.group(1) != " "}
    m = re.match(r"^- ▸(?: (.*))?$", t)
    if m:
        return "toggle", _strip_inner_escape(m.group(1) or ""), {}
    m = re.match(r"^-(?: (.*))?$", t)
    if m:
        return "bulleted_list_item", _strip_inner_escape(m.group(1) or ""), {}
    m = re.match(r"^\d+\.(?: (.*))?$", t)
    if m:
        return "numbered_list_item", _strip_inner_escape(m.group(1) or ""), {}
    return None


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _flat(lines, offset=0, max_nesting=MAX_NESTING, soft_wrap=True):
    """[(indent, block, line_no, raw)] in document order, multi-line blocks consumed whole."""
    out, i = [], 0
    # Which sentinels open a region, read off the closing markers rather than off the
    # type: what a block renders decides it (`pull.is_region`) — a `template` with
    # children and an `equation`'s fence are regions the same way a column list is.
    region_ids = set()
    for line in lines:
        end = KEEP_END_RE.match(line.strip())
        if end:
            region_ids.add(end.group(1))
    while i < len(lines):
        s, ln = lines[i].rstrip(), offset + i + 1
        if not s.strip():
            i += 1; continue
        ind, t = _indent(s), s.strip()

        keep = KEEP_RE.match(t)
        if keep:
            block_id, kind = keep.groups()
            if kind in UNRENDERED_TYPES:
                raise Refused(ln, s, f"keep-sentinel names `{kind}`, which the mirror renders as "
                                     "nothing, so the stand-in line it claims to cover cannot exist")
            if block_id in region_ids:
                # everything to the closing marker is the block's own content, which the
                # page already holds; matching on the id is what lets regions nest
                for k in range(i + 1, len(lines)):
                    end = KEEP_END_RE.match(lines[k].strip())
                    if end and end.group(1) == block_id:
                        _refuse_if_buried(kind, lines[i + 1:k], ln, s)
                        i = k + 1
                        break
                else:
                    raise Refused(ln, s, f"a keep-sentinel opening a `{kind}` region that is "
                                         "never closed, so everything below it would be read as "
                                         "content the page does not have")
                continue
            if kind in REGION_TYPES:
                raise Refused(ln, s, f"a `{kind}` region that is never closed — the mirror renders "
                                     "no line of its own for one, so with no closing marker there "
                                     "is nothing to say where its content ends")
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j >= len(lines):
                raise Refused(ln, s, "keep-sentinel has no stand-in line after it")
            covered, cln = lines[j].strip(), offset + j + 1
            span = standin_extent(kind, lines, j)
            if not span:
                raise Refused(cln, lines[j], f"a keep-sentinel naming `{kind}` over a line that is "
                                             "not what the mirror renders one as — the sentinel has "
                                             "drifted, and skipping the line would drop it from the "
                                             "push while its block stayed on the page")
            if kind in ID_IN_STANDIN:
                ids = re.findall(ID32, covered)
                if ids and block_id not in ids:
                    raise Refused(cln, lines[j], "the line after a keep-sentinel does not carry the "
                                                 "sentinel's block id")
            _refuse_if_buried(kind, lines[j:j + span], ln, s)
            if kind in REBUILT_TYPES:
                _mark, label, url = URL_BLOCK_RE.match(covered).groups()
                if not HTTP_URL.match(url):
                    raise Refused(cln, lines[j], f"a {kind} stand-in whose target is not an http(s) "
                                                 "URL — a replace deletes the old blocks before it "
                                                 "appends, so a payload Notion rejects leaves the "
                                                 "page empty")
                # at the stand-in's indent, not the sentinel's: `pull.render_body`
                # writes every sentinel at column 0, so the covered line is the only
                # thing that says how deep the block sits
                out.append((_indent(lines[j]), url_block(kind, label, url), cln, lines[j]))
            i = j + span; continue

        if t.startswith("<!--"):
            for token, reason in DIAGNOSTICS.items():
                if t.startswith(token):
                    raise Refused(ln, s, reason)
            raise Refused(ln, s, "an HTML comment this converter does not recognise")

        if t.startswith("<details"):
            summary, inner, inner_offset, i = _scan_details(lines, i, offset)
            out.append((ind, {"type": "toggle", "toggle": {
                "rich_text": inline_md(summary.strip()),
                "children": _assemble(_flat(inner, inner_offset, max_nesting),
                                      max_nesting)}}, ln, s))
            continue

        refusal = _standin_refusal(t)
        if refusal:
            raise Refused(ln, s, refusal)

        external = MEDIA_EXTERNAL.match(t)
        if external:
            out.append((ind, external_media_block(external.group(1), external.group(2)), ln, s))
            i += 1; continue

        if t == "$$":
            buf = []
            i += 1
            while i < len(lines) and lines[i].strip() != "$$":
                buf.append(lines[i][ind:].rstrip()); i += 1
            i += 1
            out.append((ind, {"type": "equation", "equation": {"expression": "\n".join(buf)}}, ln, s))
            continue

        if t.startswith("```"):
            lang = CODE_LANGS.get(t[3:].strip().lower(), "plain text")
            buf = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i][ind:].rstrip("\n")); i += 1
            i += 1
            code = {"rich_text": rich("\n".join(buf)), "language": lang}
            # the caption the renderer wrote under the fence goes back on the
            # block rather than becoming a paragraph of its own
            if i < len(lines) and lines[i].strip().startswith(CAPTION_MARK + " "):
                code["caption"] = inline_md(lines[i].strip()[len(CAPTION_MARK) + 1:])
                i += 1
            out.append((ind, {"type": "code", "code": code}, ln, s))
            continue

        if t.startswith("|") and t.endswith("|"):
            rows = []
            # entry and continuation must agree on how they strip, or every indented
            # table keeps its header row and drops its body
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [c.strip().replace("\\|", "|")
                         for c in CELL_SPLIT.split(lines[i].strip().strip("|"))]
                if not all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            if not rows:
                raise Refused(ln, s, "a table with a separator row and no cells")
            width = max(len(r) for r in rows)
            out.append((ind, {"type": "table", "table": {
                "table_width": width, "has_column_header": True, "has_row_header": False,
                "children": [{"type": "table_row", "table_row":
                              {"cells": [inline_md(r[c]) if c < len(r) else rich("") for c in range(width)]}}
                             for r in rows]}}, ln, s))
            continue

        if t == ">" or t.startswith("> "):
            # A quote/callout *region*: every consecutive line at this indent
            # whose content starts `>` belongs to it — bare `>` is a blank line
            # inside, and a truly blank line (or anything else) ends it, which
            # is what keeps sibling quotes distinct (the renderer separates them).
            # The stripped interior is a document of its own: leading lines are
            # the block's rich text under the paragraph continuation rules, and
            # everything after parses recursively as the block's children — the
            # structure markdown blockquotes always had and this converter
            # used to flatten into indistinguishable siblings.
            start = i
            while i < len(lines):
                cur = lines[i]
                probe = cur.strip()
                if not probe or _indent(cur) != ind or not (probe == ">" or probe.startswith("> ")):
                    break
                i += 1
            interior = []
            for line in lines[start:i]:
                after = line[ind + 1:]
                interior.append(after[1:] if after[:1] == " " else after)
            first = interior[0]
            typ, extra = "quote", {}
            split = split_callout_icon(first)
            if split is not None:
                emoji, first = split
                typ = "callout"
                if emoji:
                    extra = {"icon": {"type": "emoji", "emoji": emoji}}
            # a lone backslash is how the renderer spells "this quote's first
            # line is empty", which whitespace cannot say — `>  ` is already
            # the empty-icon callout
            raw_first = first
            first = "" if first == "\\" else _strip_lead_escape(_strip_inner_escape(first))
            # `_refuse_if_buried` reads indentation, and a region's interior is
            # dedented to column 0 before it is parsed — so a keep-sentinel in
            # here would be stepped over silently and the block it stands for
            # deleted with the rewritten region. `pull` declines to sentinel
            # inside a region, so this is a hand-edited or legacy body.
            for probe in interior:
                if KEEP_RE.match(probe.strip()):
                    raise Refused(ln, s, "a keep-sentinel inside a quote or callout — the "
                                         "block it stands for cannot be distinguished from the "
                                         "region's own content, and a push would delete it "
                                         "with the region. Move it to the top level of the "
                                         "page in Notion, or make this edit in Notion")
            # a first line that opens a block is a block, not the region's text:
            # the renderer escapes a marker it means literally, so this is
            # hand-written markdown and its first bullet stays a bullet
            # on the *raw* line: the renderer escapes a marker it means as
            # text, so an unescaped one here is markdown somebody typed
            if starts_block(raw_first):
                first, k_start = "", 0
            else:
                k_start = 1
            # the trailing two spaces are the hard break's marker, not content:
            # they decide the separator and are then dropped, exactly as the
            # paragraph path drops them by rstripping its line first
            # Only an explicit hard break continues the block's own text. The
            # renderer never writes two `> ` lines in a row without either that
            # marker or a bare-`>` seam, so this is exact for its output — and
            # it is the safe reading of the legacy bodies written before
            # regions existed, where consecutive `> ` lines were separate
            # blocks: they come back as children rather than being flattened
            # into one soft-wrapped sentence.
            text, k = first.rstrip(), k_start
            while k and k < len(interior) and interior[k].strip() and interior[k - 1].endswith("  ") \
                    and not starts_block(interior[k]) and _indent(interior[k]) == 0:
                text += "\n" + _strip_lead_escape(interior[k].strip())
                k += 1
            block = {"rich_text": inline_md(text), **extra}
            if k < len(interior):
                # `soft_wrap=False` inside the region: the renderer separates
                # sibling paragraphs here with a bare `>` and marks a real line
                # break with two spaces, so an unmarked line is always its own
                # block — and the legacy bodies written before regions existed
                # keep their line-per-block structure instead of collapsing
                block["children"] = _assemble(
                    _flat(interior[k:], offset + start + k, max_nesting, soft_wrap=False),
                    max_nesting)
            out.append((ind, {"type": typ, typ: block}, ln, s))
            continue

        if t == "---":
            out.append((ind, {"type": "divider", "divider": {}}, ln, s))
            i += 1; continue

        m = re.match(r"^(#{1,6})(?: (.*))?$", t)
        if m:
            lvl = min(len(m.group(1)), 3)   # Notion has three heading levels, no more
            text = _strip_lead_escape(m.group(2) or "")
            while i + 1 < len(lines) and lines[i].endswith("  ") and lines[i + 1].strip() \
                    and not starts_block(lines[i + 1]) and _indent(lines[i + 1]) <= ind:
                i += 1; text += "\n" + _strip_lead_escape(lines[i].strip())
            out.append((ind, {"type": f"heading_{lvl}",
                              f"heading_{lvl}": {"rich_text": inline_md(text)}}, ln, s))
            i += 1; continue

        found = _one_line_block(t, ln, s)
        if found:
            typ, text, extra = found
            # a two-space break continues the block's own text; anything else
            # on the next line is the next block (or its child, by indent)
            while i + 1 < len(lines) and lines[i].endswith("  ") and lines[i + 1].strip() \
                    and not starts_block(lines[i + 1]) and _indent(lines[i + 1]) <= ind:
                i += 1; text += "\n" + _strip_lead_escape(lines[i].strip())
            out.append((ind, {"type": typ, typ: dict(rich_text=inline_md(text), **extra)}, ln, s))
            i += 1; continue

        text = _strip_lead_escape(t)
        while i + 1 < len(lines) and lines[i + 1].strip() and not starts_block(lines[i + 1]) \
                and _indent(lines[i + 1]) <= ind \
                and (soft_wrap or lines[i].endswith("  ")):
            # soft wrap: a line at this level continues the paragraph, an indented
            # one is a child block. Two trailing spaces are how `walker.py` writes
            # a shift+enter break inside one paragraph.
            sep = "\n" if lines[i].endswith("  ") else " "
            i += 1; text += sep + _strip_lead_escape(lines[i].strip())
        out.append((ind, {"type": "paragraph", "paragraph": {"rich_text": inline_md(text)}}, ln, s))
        i += 1
    return out


def nesting_depth(block):
    """Levels of children below this block. A table's rows are not nesting."""
    kids = block[block["type"]].get("children") if block["type"] != "table" else None
    return 1 + max((nesting_depth(k) for k in kids), default=0) if kids else 0


def _assemble(flat, max_nesting=MAX_NESTING):
    """Indentation is nesting: the mirror indents a child block two spaces per level.

    ``max_nesting=None`` builds the whole tree the markdown describes. That is for
    a caller that splits the tree across requests itself — the write path appends
    the deeper levels in follow-up requests, so for it the limit is a property of
    one request rather than of the document.
    """
    root, stack = [], []
    for ind, block, ln, raw in flat:
        while stack and stack[-1][0] >= ind:
            stack.pop()
        if max_nesting is not None and len(stack) + nesting_depth(block) > max_nesting:
            # counted at the root too: a `<details>` arrives as a toggle that already
            # carries its own children, and Notion rejects the whole request after the
            # pusher has deleted the page body
            raise Refused(ln, raw, f"nesting runs deeper than the {max_nesting} levels "
                                   "Notion accepts in one request")
        if stack:
            parent = stack[-1][1]
            if parent["type"] not in CHILD_BEARING:
                raise Refused(ln, raw, f"a {parent['type']} block cannot hold children")
            parent[parent["type"]].setdefault("children", []).append(block)
        else:
            root.append(block)
        stack.append((ind, block, ln, raw))
    return root


def lines_blocks(md, max_nesting=MAX_NESTING):
    return _assemble(_flat(md.split("\n"), max_nesting=max_nesting), max_nesting)


def md_blocks(md, max_nesting=MAX_NESTING):
    """(visible blocks, blocks for the `Details` toggle or None) — push.py's two layers."""
    lines = md.split("\n")
    for i, line in enumerate(lines):
        if line.lstrip().startswith("<details"):
            summary, inner, inner_offset, end = _scan_details(lines, i)
            visible = (_assemble(_flat(lines[:i], max_nesting=max_nesting), max_nesting)
                       + _assemble(_flat(lines[end:], end, max_nesting), max_nesting))
            return visible, _assemble(_flat(inner, inner_offset, max_nesting), max_nesting)
    return lines_blocks(md, max_nesting), None


UUID_RE = re.compile(r'\b([0-9a-f]{8})-?([0-9a-f]{4})-?([0-9a-f]{4})-?([0-9a-f]{4})-?([0-9a-f]{12})\b')


def prop_rich(text):
    """rich_text for a property. Properties hold no blocks, but they do hold inline
    formatting: bold, code and links render, so reuse the body parser for those, and
    turn a bare Notion page id into a page *mention* (a live chip that follows renames).
    Without this, `code` and **bold** reach Notion as literal backticks and asterisks.

    The parse runs **first** and the mentions are promoted out of what it produced.
    Scanning the raw string ahead of it — which this did until 2026-08-27 — splits the
    string at every id and parses the fragments separately, so any span containing one
    is destroyed: a code span loses its backticks to literal text, a bold run loses its
    asterisks, and only the link case was patched (by a `](`-lookback that the item
    shapes make unnecessary now). The damage outlived the push, since Notion stored the
    orphaned markers and every later comparison of the two projections disagreed.

    A mention is promoted only out of unannotated, unlinked text: an id inside a code
    span is content and stays one — which is also the only round-trippable reading,
    because a mention comes back from Notion as its page *title*, with no id to
    recover. That asymmetry is the price of the live chip, and it is why the promotion
    is kept as narrow as it can be.
    """
    out = []
    for item in inline_md(text):
        content = (item.get("text") or {}).get("content")
        if (item.get("type") != "text" or item.get("annotations")
                or (item.get("text") or {}).get("link") or not content):
            out.append(item)
            continue
        pos = 0
        for m in UUID_RE.finditer(content):
            if m.start() > pos:
                out += rich(content[pos:m.start()])
            out.append({"type": "mention", "mention": {
                "page": {"id": "-".join(m.group(i) for i in range(1, 6))}}})
            pos = m.end()
        if pos:
            if pos < len(content):
                out += rich(content[pos:])
        else:
            out.append(item)
    return out or inline_md(text)
