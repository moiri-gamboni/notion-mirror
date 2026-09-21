#!/usr/bin/env python3
"""Give every stored comment bullet its comment id.

The mirror used to key comments by their rendered text. That is not an
identity: two comments reading "TBD" in two threads on one page collapsed into
one bullet, and one comment whose rendering shifted (a link expanded, a
continuation line re-indented) split into two — ~315 rows carry duplicates from
the second half of that. `refresh.py` now writes an id trailer on every bullet
it produces and merges on the id. This tool does the same for what is already
on disk, so the first probe after it recognises a stored bullet instead of
annotating it resolved and adding a fresh twin beside it.

**It stamps ids; it never rewrites comment text.** That is the point of running
it in the same change as the switch to `rich_md` comment rendering: the next
probe re-renders the text (mentions become followable links), and because the
bullet already carries the id, that lands as an in-place update rather than as a
duplicate. Rewriting the text here would produce the same end state through a
much larger and unreviewable diff.

Where the ids come from, in order of trust:

1. `webhook-comments-capture.jsonl` — the receiver's own captures.
2. The same file's backfill entries (`captured_at: "backfill"`).
3. A live `GET /comments` on the row (`--refetch`), which returns the threads
   still open. Page-level only: a block-anchored comment would cost a full body
   walk to find, and the sources above already hold most of them.
4. `webhook-comments-capture.jsonl.pre-repair` — the capture log as it stood
   before the 2026-07-27 misattribution repair, so its page attributions are
   partly wrong. Used last, and only for ids the repaired log does not attribute
   to some *other* page; without that guard this source would re-import exactly
   the foreign ids the repair removed.

A bullet no source resolves keeps a `legacy` shim and is never dropped — the
mirror's comment record is append-only, and 26,907 of the resolved bullets on
disk are permanently invisible to the API.

Scope: DB row `.md` files. `workspace/_comments.md` is left alone unless
`--include-comments-md` is passed — it is one 6.1 MB file whose rewrite nobody
can review, and the row tier is what tasksync reads. The nightly stamps its
sections as it rescans them either way.

This is a standalone mirror writer, so a writing run takes
`~/.locks/notion-mirror-internal` itself (as `refresh.sh` and
`coverage_backfill.py` do); a dry run only reads and does not.

Usage:
    migrate_comment_ids.py                          # dry run: coverage, no writes
    migrate_comment_ids.py --apply
    migrate_comment_ids.py --apply --refetch --budget 3000
    migrate_comment_ids.py --apply --dbs Tasks
    migrate_comment_ids.py --report
"""
import argparse
import collections
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import coverage_backfill  # noqa: E402  (the engine dir is not a package) — the lock helper
import paths  # noqa: E402
import refresh  # noqa: E402

PROGRESS_NAME = "comment-id-migration-progress.json"
CAPTURE_NAME = paths.CAPTURE
PRE_REPAIR_NAME = paths.CAPTURE + ".pre-repair"

ROW_PREFIX = "- _"
CONTENT_PREFIX = "- **on**"

# "- _Who (2026-07-01):_ text"
ROW_BULLET = re.compile(r"^- _(?P<who>.*?) \((?P<date>\d{4}-\d{2}-\d{2})\):_ ?(?P<text>.*)\Z", re.S)
# '- **on** "anchor" — Who (2026-07-01): text'
CONTENT_BULLET = re.compile(
    r'^- \*\*on\*\* "(?P<anchor>.*?)" — (?P<who>.*?) \((?P<date>\d{4}-\d{2}-\d{2})\): ?(?P<text>.*)\Z',
    re.S)

SECTION = re.compile(r"^## (.*?)  `([0-9a-f]{32})`\n(.*?)(?=^## |\Z)", re.M | re.S)

SOURCES = ("capture", "backfill", "live", "pre_repair", "legacy")


def norm(s):
    """Match key for a comment's text. Whitespace is normalised because the two
    tiers indent continuation lines differently and always have."""
    return " ".join((s or "").split())


# ---------------------------------------------------------------- the id sources

class Sources:
    """Every (page, comment) pair we can name, indexed for matching.

    Keyed twice per entry: by (author, date, text) and by (date, text). The
    author name on disk was resolved through `users.json` at write time and a
    renamed or deleted user drifts, so the looser key is the fallback rather
    than the primary."""

    def __init__(self, state_dir, use_pre_repair=True):
        self.by_page = collections.defaultdict(lambda: (collections.defaultdict(list),
                                                        collections.defaultdict(list)))
        self.users = refresh.jload(os.path.join(state_dir, paths.USERS), {})
        self.claimed = {}       # comment id -> page, from the repaired log only
        self.counts = collections.Counter()
        self._read(os.path.join(state_dir, CAPTURE_NAME), trusted=True)
        if use_pre_repair:
            self._read(os.path.join(state_dir, PRE_REPAIR_NAME), trusted=False)

    def _read(self, path, trusted):
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                try:
                    e = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                page = (e.get("page_id") or "").replace("-", "")
                if not page:
                    continue
                backfill = e.get("captured_at") == "backfill"
                src = ("backfill" if backfill else "capture") if trusted else "pre_repair"
                for c in e.get("comments", []):
                    cid = (c.get("id") or "").replace("-", "")
                    if not cid:
                        continue
                    if trusted:
                        self.claimed.setdefault(cid, page)
                    elif self.claimed.get(cid, page) != page:
                        # the repair moved this comment to another page; taking
                        # it here would put the misattribution back
                        self.counts["pre_repair_rejected"] += 1
                        continue
                    self._add(page, cid, c, src)

    def _add(self, page, cid, c, src):
        who = self.users.get((c.get("author_id") or ""), "")
        date = (c.get("created_time") or "")[:10]
        entry = {"cid": cid, "did": (c.get("discussion_id") or "").replace("-", ""),
                 "src": src}
        # The stored corpus was rendered with `plain`; captures written from
        # 2026-08-06 store `rich_md` in `text` and the raw items beside it. Index
        # under every rendering the entry can produce, so a bullet matches
        # whichever era it was written in.
        texts = {norm(c.get("text", "")), norm(refresh.plain(c.get("rich_text")))}
        with_author, without = self.by_page[page]
        for t in texts:
            if not t:
                continue
            with_author[(who, date, t)].append(entry)
            without[(date, t)].append(entry)
        self.counts[src] += 1

    def add_live(self, page, comments, users_map):
        """Results of a live `GET /comments` on one page."""
        for c in comments:
            cid = (c.get("id") or "").replace("-", "")
            if not cid:
                continue
            self._add(page, cid, {
                "id": cid, "discussion_id": c.get("discussion_id", ""),
                "author_id": (c.get("created_by") or {}).get("id", ""),
                "created_time": c.get("created_time", ""),
                "text": refresh.plain(c.get("rich_text")),
            }, "live")
            users_map.name(c.get("created_by"))  # keep users.json in step

    def take(self, page, who, date, text):
        """Claim an id for one bullet, or None. Claimed entries are consumed, so
        N identical bullets and N identical captured comments pair up one to one
        instead of all taking the first id."""
        with_author, without = self.by_page.get(page, (None, None))
        if with_author is None:
            return None
        for bucket, key in ((with_author, (who, date, norm(text))),
                            (without, (date, norm(text)))):
            while bucket.get(key):
                entry = bucket[key].pop(0)
                if entry.get("used"):
                    continue
                entry["used"] = True
                return entry
        return None


# ---------------------------------------------------------------- bullet parsing

def bullet_spans(lines, start, end, prefix):
    """-> [(first_line_index, whole_bullet_text)] for bullets inside a slice.

    Line indices rather than rewritten blocks: only the first line of an
    unstamped bullet changes, and everything else in the file — blank lines,
    the header, the truncation appendix — stays byte-identical."""
    out = []
    cur_i, cur = None, []
    for i in range(start, end):
        if lines[i].startswith(prefix):
            if cur_i is not None:
                out.append((cur_i, "\n".join(cur).rstrip("\n")))
            cur_i, cur = i, [lines[i]]
        elif cur_i is not None:
            cur.append(lines[i])
    if cur_i is not None:
        out.append((cur_i, "\n".join(cur).rstrip("\n")))
    return [(i, b) for i, b in out if b.strip()]


def parse_bullet(bullet, prefix):
    """-> (who, date, text) or None if the bullet is not in the shape we write.

    Continuation lines are de-indented back to what the comment said, which is
    what the capture log holds."""
    b = refresh.RESOLVED_MARK.sub("", refresh.CID_MARK.sub("", bullet))
    m = (ROW_BULLET if prefix == ROW_PREFIX else CONTENT_BULLET).match(b)
    if not m:
        return None
    return m.group("who"), m.group("date"), m.group("text").replace("\n  ", "\n")


# ---------------------------------------------------------------- one file

def stamp_region(lines, start, end, prefix, page, sources, tally):
    """Stamp every not-yet-identified bullet in a slice. -> list of unresolved.

    A `legacy` shim is *not* "already done": it records that no source had the
    id last time, and a later run — typically the first one passing `--refetch`
    — may well find it. Only a bullet carrying a real id is skipped, so the
    order an operator happens to run the modes in cannot strand a bullet as
    legacy forever."""
    unresolved = []
    for i, bullet in bullet_spans(lines, start, end, prefix):
        tally["bullets"] += 1
        if refresh.bullet_cid(bullet):
            tally["already"] += 1
            continue
        parsed = parse_bullet(bullet, prefix)
        if parsed is None:
            tally["unparsed"] += 1
            unresolved.append((i, None))
            continue
        entry = sources.take(page, *parsed)
        if entry is None:
            unresolved.append((i, parsed))
            continue
        lines[i] = refresh.stamp_cid(lines[i], entry["cid"], entry["did"])
        tally[entry["src"]] += 1
    return unresolved


def shim(lines, unresolved, tally):
    for i, _ in unresolved:
        lines[i] = refresh.stamp_cid(lines[i])
        tally["legacy"] += 1


def row_files(dbs_filter=None, only=None):
    """Every DB row `.md` that carries a comments section."""
    out = []
    if not os.path.isdir(refresh.DBS):
        return out
    for d in sorted(os.listdir(refresh.DBS)):
        dd = os.path.join(refresh.DBS, d)
        if not os.path.isdir(dd):
            continue
        if dbs_filter and dbs_filter.lower() not in d.lower():
            continue
        for f in sorted(os.listdir(dd)):
            if not f.endswith(".md") or f.startswith("_schema"):
                continue
            m = refresh.ID32.search(f)
            if not m or (only and m.group(1) not in only):
                continue
            out.append((m.group(1), os.path.join(dd, f)))
    return out


def migrate_row(path, rid, sources, tally, refetch=None):
    """-> (new_text or None if nothing changed, unresolved_count)."""
    with open(path, encoding="utf-8", errors="replace") as f:
        txt = f.read()
    if refresh.MARKER not in txt:
        return None, 0
    m = re.search(r"^## Comments\n", txt.split(refresh.MARKER, 1)[1], re.M)
    if not m:
        return None, 0
    head, tail = txt.split(refresh.MARKER, 1)
    lines = tail.split("\n")
    start = tail[:m.end()].count("\n")
    unresolved = stamp_region(lines, start, len(lines), ROW_PREFIX, rid, sources, tally)
    if unresolved and refetch is not None:
        refetch(rid, sources)
        unresolved = [(i, p) for i, p in unresolved
                      if p is None or not _retry(lines, i, p, rid, sources, tally)]
    shim(lines, unresolved, tally)
    new = head + refresh.MARKER + "\n".join(lines)
    return (new if new != txt else None), len(unresolved)


def _retry(lines, i, parsed, page, sources, tally):
    entry = sources.take(page, *parsed)
    if entry is None:
        return False
    lines[i] = refresh.stamp_cid(lines[i], entry["cid"], entry["did"])
    tally[entry["src"]] += 1
    return True


def migrate_comments_md(sources, tally):
    """`--include-comments-md`: the content tier, from the offline sources only.

    No refetch here — a content page's comments hang off its blocks, so finding
    them live means walking the page, which is the nightly's job and not worth a
    second implementation."""
    path = os.path.join(refresh.WS, "_comments.md")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        txt = f.read()
    limit = txt.find(refresh.APPENDIX_MARK)
    limit = len(txt) if limit == -1 else limit
    lines = txt.split("\n")
    for sm in SECTION.finditer(txt):
        if sm.start() >= limit:
            break
        start = txt[:sm.start(3)].count("\n")
        end = txt[:sm.end(3)].count("\n") + 1
        shim(lines, stamp_region(lines, start, min(end, len(lines)),
                                 CONTENT_PREFIX, sm.group(2), sources, tally), tally)
    new = "\n".join(lines)
    return new if new != txt else None


# ---------------------------------------------------------------- progress

def progress_path():
    return os.path.join(refresh.STATE, PROGRESS_NAME)


def load_progress():
    """What survives an interrupted run.

    Not a per-file ledger: the stamped file *is* the record, so a re-run skips
    what it already did by reading it, and a ledger over 83,097 row files
    re-serialised after each one would cost more than the migration. What must
    not be repeated is the part that spends requests, so the rows already
    re-fetched are tracked by id in `refetched` and never asked twice.

    A row is recorded there only after its file is written, so an interrupt
    between the request and the write costs one repeated request rather than
    silently shimming bullets whose ids that request had already found."""
    p = refresh.jload(progress_path(), None)
    if not isinstance(p, dict):
        p = {}
    p.setdefault("started", refresh.now_iso())
    p.setdefault("updated", "")
    p.setdefault("requests", 0)
    p.setdefault("files", 0)
    p.setdefault("totals", {})
    p.setdefault("refetched", {})
    return p


def save_progress(p):
    p["updated"] = refresh.now_iso()
    refresh.jsave(progress_path(), p)


def print_tally(t, out=sys.stdout):
    t = collections.Counter(t)
    print(f"bullets seen        {t['bullets']:7d}", file=out)
    print(f"  already stamped   {t['already']:7d}", file=out)
    for s in SOURCES:
        print(f"  from {s:<13} {t[s]:7d}", file=out)
    if t["unparsed"]:
        print(f"  unparsed shape    {t['unparsed']:7d}  (shimmed, never dropped)", file=out)
    resolved = sum(t[s] for s in SOURCES if s != "legacy")
    seen = t["bullets"] - t["already"]
    if seen:
        print(f"coverage            {resolved / seen:7.1%} of {seen} unstamped bullets", file=out)


# ---------------------------------------------------------------- entrypoint

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="write the stamped files (default is a dry run)")
    ap.add_argument("--refetch", action="store_true",
                    help="ask Notion for the still-open comments on rows the "
                         "offline sources did not fully resolve (needs --budget)")
    ap.add_argument("--budget", type=int, default=0,
                    help="max API requests for this run; a budget stop is a normal exit")
    ap.add_argument("--rps", type=float, default=3.0)
    ap.add_argument("--dbs", default="", metavar="SUBSTR",
                    help="only DB directories whose name contains this")
    ap.add_argument("--only", nargs="+", metavar="ID32", default=None,
                    help="only these row ids")
    ap.add_argument("--include-comments-md", action="store_true",
                    help="also stamp workspace/_comments.md (one 6.1 MB diff)")
    ap.add_argument("--no-pre-repair", action="store_true",
                    help="ignore the pre-repair capture log entirely")
    ap.add_argument("--report", action="store_true",
                    help="print the last run's totals and exit")
    ap.add_argument("--lock", default=coverage_backfill.LOCK_PATH)
    args = ap.parse_args(argv)

    progress = load_progress()
    if args.report:
        if not progress["totals"]:
            print("no migration run recorded yet")
            return 0
        print(f"last run {progress['updated']}, {progress['files']} files stamped, "
              f"{progress['requests']} requests, "
              f"{len(progress['refetched'])} rows re-fetched")
        print_tally(progress["totals"])
        return 0

    if args.refetch and not args.budget:
        ap.error("--refetch needs a --budget (it makes one request per unresolved row)")
    if args.refetch and not args.apply:
        ap.error("--refetch without --apply would spend requests and throw the answers away")

    lock = None
    if args.apply:
        try:
            lock = coverage_backfill.take_lock(args.lock)
        except coverage_backfill.Locked as e:
            print(str(e), file=sys.stderr)
            return 1

    sources = Sources(refresh.STATE, use_pre_repair=not args.no_pre_repair)
    refresh.log(f"id sources: {sources.counts['capture']} captured, "
                f"{sources.counts['backfill']} backfilled, "
                f"{sources.counts['pre_repair']} pre-repair "
                f"({sources.counts['pre_repair_rejected']} rejected as another page's)")

    api = users = None
    refetch = None
    if args.refetch:
        token = os.environ.get("NOTION_TOKEN")
        if not token:
            print("NOTION_TOKEN not set", file=sys.stderr)
            return 2
        api = refresh.Api(token, args.rps, args.budget)
        users = refresh.Users(api)

        def refetch(rid, src, _api=api, _users=users):
            """One live `GET /comments` per unresolved row, asked once ever. The
            id lands in `spent` rather than in `progress` directly, so the main
            loop can record it only once the row's file is on disk."""
            if rid in progress["refetched"]:
                return
            _api.check_budget()
            try:
                src.add_live(rid, list(_api.paginate(
                    "GET", "/comments", params={"block_id": refresh.dashed(rid)})), _users)
            except refresh.ApiError as e:
                refresh.log(f"refetch of {rid[:8]} failed (HTTP {e.code}) — shimming its bullets")
            spent.append(rid)

    tally = collections.Counter()
    spent = []
    stopped = ""
    files = row_files(args.dbs or None, set(args.only) if args.only else None)
    refresh.log(f"{len(files)} row files in scope")
    try:
        for n, (rid, path) in enumerate(files, 1):
            seen_before = tally["bullets"]
            try:
                new, left = migrate_row(path, rid, sources, tally, refetch=refetch)
            except refresh.Budget:
                stopped = "budget"
                break
            if new is not None and args.apply:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new)
            if tally["bullets"] > seen_before:
                progress["files"] += 1
            if spent:
                # only rows that actually cost requests are checkpointed, and
                # only now that their file is written
                for r in spent:
                    progress["refetched"][r] = refresh.now_iso()
                del spent[:]
                if args.apply:
                    save_progress(progress)
            if n % 5000 == 0:
                refresh.log(f"{n}/{len(files)} files, {tally['bullets']} bullets"
                            + (f", req={api.n}" if api else ""))
        else:
            if args.include_comments_md:
                new = migrate_comments_md(sources, tally)
                if new is not None and args.apply:
                    with open(os.path.join(refresh.WS, "_comments.md"), "w",
                              encoding="utf-8") as f:
                        f.write(new)
    finally:
        if api:
            progress["requests"] = progress.get("requests", 0) + api.n
            users.save()
        progress["totals"] = dict(tally)
        if args.apply:
            save_progress(progress)
        if lock:
            lock.close()

    if stopped == "budget":
        print("request budget exhausted — a normal stop, re-run to continue", file=sys.stderr)
    print_tally(tally)
    if not args.apply:
        print("\ndry run: nothing written (--apply to write"
              + (", --refetch --budget N to also ask Notion)" if not args.refetch else ")"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
