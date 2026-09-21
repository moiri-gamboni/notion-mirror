"""The grammar of a mirror row's markdown file: the marker, the region delimiters,
and the comment-bullet trailer.

This is the one contract the mirror shares with a reader it does not control. tasksync
parses all of it — the marker to find where enrichment starts, the delimiters to take
the comments region without a body heading hijacking the match, and the cid trailer to
key comments by identity rather than by text. A second copy of any of these on the
reading side is the failure this module exists to prevent, so the parsers here are
borrowed rather than reimplemented (see `tasksync/tasksync/comments.py`).

What is deliberately NOT here: the merge policy (`still_live`, `merge_comment_bullets`,
`dedup_by_cid`). Those decide what the mirror *does* with a bullet, which is engine
behaviour and stays in `refresh.py`; this module only says what a bullet looks like.
"""
import re


MARKER = "<!-- body+comments fetched -->"

# Mirror-internal delimiters around the two enrichment regions. Additive: the
# `## Body` / `## Comments` headings stay exactly where they were, because they
# are what a human reads. The delimiters are what a parser keys on.
#
# They became necessary when row bodies started walking at indent 0.
# At indent 1 every rendered line carried two leading spaces, so a `## Comments`
# heading *inside* somebody's body rendered as `  ## Comments` and could not be
# confused with the enrichment's own. At indent 0 it renders at column 0 and is
# byte-identical to it — and `extract_comments_body` takes the *first* match, so
# a body containing that heading would hand its own text to the comment merge.
BODY_OPEN = "<!-- notion:body -->"
BODY_CLOSE = "<!-- notion:/body -->"
COMMENTS_OPEN = "<!-- notion:comments -->"
COMMENTS_CLOSE = "<!-- notion:/comments -->"


RESOLVED_MARK = re.compile(r" _\[resolved/deleted ≤\d{4}-\d{2}-\d{2}\]_")

# ------------------------------------------------------ comment identity (A5)
#
# Comments used to be keyed by their rendered text, which is not an identity:
# two people writing "TBD" in two threads on one page collapsed into one bullet,
# while one comment re-rendered slightly differently (a link expanded, a
# continuation line re-indented) split into two. ~315 rows carried duplicates
# from the second half of that. So every bullet now carries its comment id in a
# trailing HTML comment on its first line, and the merge keys on that.
#
# Grammar (binding — tasksync's live-comment layer parses exactly this):
#
#   <!-- notion:cid <comment_id32> d=<discussion_id32> -->   id known
#   <!-- notion:cid <comment_id32> -->                       id known, discussion not
#   <!-- notion:cid legacy -->                               no recoverable id
#
# `legacy` is the shim for a bullet whose id no source could recover — resolved
# threads the API will never return again, mostly. Those keep the old text key,
# marked as such so the two merge paths stay distinguishable; they are never
# dropped, because the mirror's comment record is append-only.
CID_LEGACY = "legacy"
CID_MARK = re.compile(r" ?<!-- notion:cid (legacy|[0-9a-f]{32})(?: d=([0-9a-f]{32}))? -->")


def cid_trailer(cid="", did=""):
    if not cid:
        return f" <!-- notion:cid {CID_LEGACY} -->"
    return f" <!-- notion:cid {cid}" + (f" d={did}" if did else "") + " -->"


def stamp_cid(bullet, cid="", did=""):
    """Attach the id trailer to a bullet's first line, leaving continuations
    alone — a reader scanning bullet starts sees one marker per comment.

    Idempotent: any trailer already on the line is replaced, so re-stamping a
    bullet (the migration upgrading a `legacy` shim once a live re-fetch finds
    its id) leaves exactly one marker rather than two."""
    first, sep, rest = bullet.partition("\n")
    return CID_MARK.sub("", first) + cid_trailer(cid, did) + sep + rest


def bullet_cid(bullet):
    """The bullet's comment id, or None for a legacy shim / an unstamped bullet
    (the corpus before the migration, and any row probed between the two)."""
    m = CID_MARK.search(bullet or "")
    return m.group(1) if m and m.group(1) != CID_LEGACY else None


def bullet_text_key(bullet):
    """The old text key: everything the bullet says, minus the annotations that
    are ours rather than the comment's."""
    return RESOLVED_MARK.sub("", CID_MARK.sub("", bullet or ""))


def annotate_resolved(bullet, today):
    """Mark a bullet the API stopped returning. The annotation goes before the
    id trailer so the trailer stays last on the line."""
    first, sep, rest = bullet.partition("\n")
    m = CID_MARK.search(first)
    mark = f" _[resolved/deleted ≤{today}]_"
    if m:
        first = first[:m.start()] + mark + first[m.start():]
    else:
        first += mark
    return first + sep + rest


def split_bullets(body, prefix="- **on**"):
    """Section body -> list of bullet blocks (each starts with prefix, keeps
    continuation lines)."""
    blocks = []
    cur = None
    for ln in (body or "").split("\n"):
        if ln.startswith(prefix):
            if cur is not None:
                blocks.append("\n".join(cur).rstrip("\n"))
            cur = [ln]
        elif cur is not None:
            cur.append(ln)
    if cur is not None:
        blocks.append("\n".join(cur).rstrip("\n"))
    return [b for b in blocks if b.strip()]


_DELIMITED_COMMENTS = re.compile(
    re.escape(COMMENTS_OPEN) + r"\n(.*?)\n?" + re.escape(COMMENTS_CLOSE), re.S)


def extract_comments_body(enrichment):
    """The bullet text of the comments region, or '' when there is none.

    Delimited files answer from the delimiters, so a `## Comments` line inside a
    body cannot hijack the match (see BODY_OPEN). The heading regex is the
    fallback for files the migration has not rewritten yet; on those, a body
    heading is still indented and cannot collide."""
    t = enrichment or ""
    m = _DELIMITED_COMMENTS.search(t)
    if m:
        return m.group(1)
    m = re.search(r"^## Comments\n(.*)\Z", t, re.S | re.M)
    return m.group(1) if m else ""


def strip_comments_section(text):
    """`text` with its whole comments region removed — heading, delimiters and
    bullets. Used by the one-shot repair scripts, which rebuild that region
    from scratch; without this they would leave an orphaned close delimiter
    behind on a migrated file."""
    t = text or ""
    m = _DELIMITED_COMMENTS.search(t)
    if m:
        head = t[:m.start()]
        h = head.rfind("\n## Comments\n")
        return t[:h] if h != -1 else head
    m = re.search(r"\n## Comments\n.*\Z", t, re.S)
    return t[:m.start()] if m else t
