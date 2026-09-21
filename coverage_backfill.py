#!/usr/bin/env python3
"""Fetch the coverage gap: every id the mirror references but never captured.

Input is the census work list (`_meta/coverage/census.json`) minus the triaged
exclusions (`_meta/coverage/exclusions.json`) — the same split
`coverage_census.py --report` prints. Output is the mirror's own artifacts,
written by the mirror's own code: a `child_database` lands as
`workspace/_databases/<Title> <id32>/` with `_schema.json`, `_schema.md`, the
CSV and one `.md` per row; a `sub_page` lands as a content-page `.md` beside its
parent, with its metadata row. Nothing here invents a format — the DB path runs
`refresh.refresh_db`, the page path runs `refresh.walk_content_page`, so a
backfilled artifact and a nightly-captured one are the same bytes.

Three properties this tool exists to have:

* **Resumable.** Per-id progress is written after *each* id to
  `_meta/state/coverage-backfill-progress.json` (gitignored, rebuildable), so an
  interrupted run loses at most the id in flight and a re-run re-fetches nothing.
* **Budget-capped.** `--budget` is required for a writing run and is the same
  request budget the nightly uses; a `Budget` stop is a normal exit, not a
  failure. The nightly is capped at 15,000 requests and already goes PARTIAL on
  a third of its runs — this must never be the thing that starves it, so it runs
  as its own job with its own budget rather than riding the nightly's.
* **Never silently dropping an id.** An id Notion refuses (404/403) moves into
  the exclusion file with a reason and a note recording the exact status and
  date, so it stops being retried *and* stays challengeable. Any other error is
  recorded as a failure and retried on the next run.

**Chain closure.** Backfilling a database captures its rows, and a row body can
carry its own inline database or sub-page — the very shape that produced this
gap. So after each capture the freshly written artifacts are re-scanned with the
census's own scanner (`coverage_census.scan_text`, no API cost) and anything
referenced-but-still-absent joins the queue. That is why `--only <db-id>` on a
notes database walks its whole chain of inline databases rather than one level
of it. Discovery is deliberately limited to the census's definition — a
stand-in in a mirrored file — and does not follow relation targets, which are
`refresh.phase_discovery`'s business.

This is a standalone mirror writer, so it takes `~/.locks/notion-mirror-internal`
itself (see the self-lock comment in `refresh.sh`); it never commits — slices are
committed by the operator.

Usage:
    coverage_backfill.py --dry-run              # what it would fetch, no API calls
    coverage_backfill.py --budget 4000          # a budgeted slice
    coverage_backfill.py --budget 500 --slice 20
    coverage_backfill.py --budget 2000 --only <id32> [<id32> ...]
    coverage_backfill.py --report               # done / remaining / excluded here
"""
import argparse
import csv
import datetime as dt
import fcntl
import io
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import coverage_census  # noqa: E402  (_tools is not a package)
import refresh  # noqa: E402

LOCK_PATH = os.path.expanduser("~/.locks/notion-mirror-internal")
PROGRESS_NAME = "coverage-backfill-progress.json"

# Databases first: the 1,473 unmirrored inline databases are the motivating gap,
# and capturing one often discovers the sub-pages below it anyway.
KIND_ORDER = {"child_database": 0, "sub_page": 1, "link_to_page": 2, "unknown": 3}

# A per-id 404/403 is a fact about that id; ten in a row is a fact about the
# token. Stop rather than write 1,800 exclusions the next reader has to un-review.
ACCESS_FAIL_LIMIT = 10


class Locked(Exception):
    pass


class Excluded(Exception):
    """Notion refused this id; it leaves the work list for the exclusion file.

    `ambiguous` marks refusals that could also be a symptom of the integration
    losing access wholesale (403s, 404s, archived pages) — those feed the
    consecutive-refusal breaker. A definitive per-id verdict delivered by a
    working token (the two permanent 400 classes) does not: unshared databases
    cluster in the queue, and counting them stopped a live slice at 43 requests.
    """

    def __init__(self, reason, note, ambiguous=True):
        self.reason, self.note = reason, note
        self.ambiguous = ambiguous
        super().__init__(f"{reason}: {note}")


def today():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------- lock & state

def take_lock(path=LOCK_PATH):
    """The mirror's internal lock, held for the life of the process.

    Standalone writers take this file directly (refresh.sh takes the same one for
    itself); the returned handle must stay referenced or the flock is released
    when it is garbage-collected."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    fh = open(path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise Locked(f"another mirror writer holds {path} — a nightly refresh or "
                     f"another backfill run is in progress")
    return fh


def partial_name(db_id):
    """Where a database is assembled before it is renamed into place.

    Two properties, both load-bearing, neither obvious from the name:

    * **It ends in `.tmp`, not in the id.** `refresh.ID32` and
      `coverage_census.ID_IN_NAME` both anchor an optional `.md`/`.csv` at the
      end, so a directory *ending* in the 32-hex id reads as a mirrored artifact
      — to the census, and to `refresh.db_dirs()`, which `phase_discovery` uses
      to decide an id needs no discovering.
    * **It is one level deeper than `_databases/`.** `refresh.row_md_global_index`
      and `refresh.build_comment_index` list `_databases/*` and then that
      directory's `.md` files, exactly one level. The first of those maps a row
      id to a *write destination*: `_dir_of_page` makedirs a folder beside the
      row and `walk_content_page` writes a content page into it. A nightly
      running between backfill slices would therefore file a real page inside an
      abandoned capture, and the next slice's sweep would delete it —
      uncommitted, because the path is gitignored. Nesting puts the rows below
      where either index looks, so neither can reach them.
    """
    return os.path.join(coverage_census.PARTIAL_PREFIX, f"{db_id}.tmp")


def sweep_partials():
    """Remove captures a previous run died inside — the lock is held, so nothing
    else can own one. `capture_database` only cleans the id it is working on,
    and a signal skips even that, so without this sweep a killed slice leaves a
    half-captured database on disk indefinitely. Returns what it removed."""
    root = os.path.join(refresh.DBS, coverage_census.PARTIAL_PREFIX)
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    shutil.rmtree(root, ignore_errors=True)
    return names


def progress_path():
    return os.path.join(refresh.STATE, PROGRESS_NAME)


def blank_progress():
    return {"started": refresh.now_iso(), "updated": "", "requests": 0,
            "done": {}, "excluded": {}, "failed": {}, "pending_extra": []}


def load_progress():
    p = refresh.jload(progress_path(), None)
    if not isinstance(p, dict):
        return blank_progress()
    for k, v in blank_progress().items():
        p.setdefault(k, v)
    return p


def save_progress(p):
    p["updated"] = refresh.now_iso()
    refresh.jsave(progress_path(), p)


def load_state():
    """The nightly's own state files, so a backfill leaves the mirror in the
    state the nightly would have left it in."""
    flags = refresh.jload(os.path.join(refresh.STATE, "db-flags.json"), {})
    return {
        "rows": refresh.jload(os.path.join(refresh.STATE, "rows-last-edited.json"), {}),
        "queue": refresh.jload(os.path.join(refresh.STATE, "probe-queue.json"), []),
        "comment_rows": refresh.jload(os.path.join(refresh.STATE, "comment-rows.json"), {}),
        "db404": flags.get("db404", {}),
        "not_a_db": flags.get("not_a_db", {}),
        # Databases the workspace has but the integration cannot see. Read by
        # `refresh.phase_discovery`, which would otherwise re-probe all ~1,300 of
        # them every night — and `capture_new_db`'s unqualified `except ApiError`
        # would file each one as `not_a_db`, a flag that is simply wrong for them.
        "unshared": flags.get("unshared", {}),
        "probe_policy": flags.get("probe_policy", {}),
    }


def save_state(state):
    refresh.jsave(os.path.join(refresh.STATE, "rows-last-edited.json"), state["rows"])
    refresh.jsave(os.path.join(refresh.STATE, "probe-queue.json"), state["queue"])
    refresh.jsave(os.path.join(refresh.STATE, "comment-rows.json"), state["comment_rows"])
    refresh.jsave(os.path.join(refresh.STATE, "db-flags.json"),
                  {"db404": state["db404"], "not_a_db": state["not_a_db"],
                   "unshared": state["unshared"],
                   "probe_policy": state["probe_policy"]})


# ---------------------------------------------------------------- the work list

def census_split(census_path, exclusions_path):
    """(to-backfill, already-excluded) exactly as coverage_census --report splits
    them — one definition of the work list, not two."""
    return coverage_census.report(census_path, exclusions_path, out=io.StringIO())


def settled(have, progress, item):
    """Is there nothing left to do for this item?

    `progress["done"]` is keyed by id, but an id can be referenced as both a
    sub-page and a child_database and then needs two different artifacts — the
    census keys `absent` by id too and takes the first-seen kind, so one work
    item can carry two presence requirements. Honouring `done` on its own makes
    the second half permanently unreachable, `--only` included, leaving the
    nightly to report it as a new gap every night with no way to close it short
    of hand-editing a gitignored progress file.

    So `done` is believed only where the artifact this kind needs is on disk. An
    id of unknown kind cannot be checked and is never settled, which is what
    makes `--only` an escape hatch rather than another way to hit the same wall.

    An **exclusion** still settles an id whatever kind asked, deliberately: it
    records what Notion answered about the object, not about one artifact of it.
    The exception is `capture_page`'s "this id is a database row" verdict, which
    is genuinely page-specific — so a mixed-kind id excluded that way is excluded
    as a database too. Left alone rather than half-fixed: it wants the exclusion
    record keyed by kind, which is a change to a tracked file's schema."""
    if item["id"] in progress["excluded"]:
        return True
    if item["id"] not in progress["done"]:
        return False
    return (item["kind"] in coverage_census.PRESENCE
            and coverage_census.is_present(item["kind"], item["id"], have))


def work_items(split, progress, have, only=None):
    """The queue: census leftovers plus anything an earlier run's chain scan
    found, minus what is already settled. `only` keeps the caller's order and
    admits ids the census never listed (kind resolved at fetch time)."""
    items, seen = [], set()

    def add(d):
        if d["id"] in seen:
            return
        seen.add(d["id"])
        items.append(d)

    for e in split["backfill"]:
        add({"id": e["id"], "kind": e["kind"], "title": e.get("title", "")})
    for e in progress["pending_extra"]:
        add({"id": e["id"], "kind": e.get("kind", "unknown"), "title": e.get("title", "")})

    if only:
        known = {i["id"]: i for i in items}
        return [known.get(o, {"id": o, "kind": "unknown", "title": ""}) for o in only]

    items = [i for i in items if not settled(have, progress, i)]
    items.sort(key=lambda i: (KIND_ORDER.get(i["kind"], 9), i["id"]))
    return items


class Ctx:
    """Everything a capture needs: the API, the mirror's state, and the two
    indexes that answer 'is this already mirrored'."""

    def __init__(self, api, users, state, report, args, exclusions_path, have=None):
        self.api, self.users = api, users
        self.state, self.report, self.args = state, report, args
        self.exclusions_path = exclusions_path
        self.exclusions = coverage_census.load_exclusions(exclusions_path)
        # A ~92k-file walk; the caller passes its own when it already has one.
        self.have = coverage_census.present_ids(refresh.WS) if have is None else have
        self.access_fails = 0
        # content-page indexes are expensive to build and only the sub_page path
        # needs them; build on first use.
        self._page_ctx = None

    def page_ctx(self):
        if self._page_ctx is None:
            meta, order = refresh.load_meta_jsonl()
            self._page_ctx = {"meta": meta, "order": order,
                              "page_index": refresh.index_workspace_pages(),
                              "row_index": refresh.row_md_global_index(),
                              "dirty": False}
        return self._page_ctx

    def flush_pages(self):
        pc = self._page_ctx
        if pc and pc["dirty"] and not self.args.dry_run:
            refresh.save_meta_jsonl(pc["meta"], pc["order"])
            pc["dirty"] = False


# ---------------------------------------------------------------- exclusion path

def classify_refusal(ctx, id32, kind, err):
    """Why Notion refused this id, in the census's closed vocabulary.

    A 404 on a database id is ambiguous — a deleted database and a *linked view*
    of one both refuse `/databases` — so one `GET /blocks` separates them: the
    view still exists as a block, the deleted database does not. The closed set
    has no bucket for 403, so a 403 takes the same bucket as a 404 and the note
    carries the real status; the note is the record a later reader challenges."""
    stamp = f"HTTP {err.code} during coverage backfill {today()}"
    if kind == "child_database":
        probe = ""
        if err.code == 404:
            try:
                ctx.api.get(f"/blocks/{refresh.dashed(id32)}")
                return "not_a_db", (f"{stamp}: /databases refused it but the block exists — "
                                    f"a linked view or other non-queryable child_database block")
            except refresh.ApiError as e2:
                probe = f"; GET /blocks also refused it (HTTP {e2.code})"
        return "db404", f"{stamp}{probe}: database deleted, or never shared with the integration"
    return "deleted", f"{stamp}: page deleted, in trash, or never shared with the integration"


def record_exclusion(ctx, progress, id32, reason, note):
    coverage_census.add_exclusion(id32, reason, note, path=ctx.exclusions_path)
    ctx.exclusions[id32] = {"reason": reason, "note": note, "added": today()}
    progress["excluded"][id32] = {"reason": reason, "note": note, "at": refresh.now_iso()}


# ---------------------------------------------------------------- captures

def csv_row_count(dirpath):
    for f in os.listdir(dirpath):
        if f.endswith(".csv") and coverage_census.ID_IN_NAME.search(f):
            with open(os.path.join(dirpath, f), newline="") as fh:
                return max(0, sum(1 for _ in csv.reader(fh)) - 1), os.path.join(dirpath, f)
    return 0, None


def seed_have(ctx, db_id, dirpath, names):
    """Mark a captured database and its rows present, and return the row files.

    A row `.md` satisfies a page reference, so both have to land or a chain scan
    treats an already-mirrored row as absent, queues it, and `/pages` answers
    with a database row — an exclusion about something that was never missing.
    One helper because `capture_database` reaches this from two branches and
    they used to disagree about the second half."""
    ctx.have["database"].add(db_id)
    written = []
    for f in names:
        if not f.endswith(".md") or f.startswith("_schema"):
            continue
        m = coverage_census.ID_IN_NAME.search(f)
        if m:
            ctx.have["page"].add(m.group(1))
        written.append(os.path.join(dirpath, f))
    return written


def capture_database(ctx, db_id):
    """First-time capture of one database -> the paths it wrote.

    Same artifacts as `refresh.capture_new_db`, with two deliberate differences:
    the rows are queried once rather than twice (that function queries for a row
    count and then again inside `refresh_db`), and the directory is built under a
    `.partial-` name and renamed only once it is complete. The rename matters:
    a half-written `_databases/<title> <id32>/` would make the next census report
    the database as *present* while its rows are still missing, which is exactly
    the hole being filled here being frozen into the definition of expected."""
    try:
        d = ctx.api.get(f"/databases/{refresh.dashed(db_id)}")
    except refresh.ApiError as e:
        if e.code in (403, 404):
            reason, note = classify_refusal(ctx, db_id, "child_database", e)
            if reason == "not_a_db":
                # keep the nightly's own flag in step, or phase_discovery retries
                # this id on every run forever
                ctx.state["not_a_db"][db_id] = refresh.now_iso()
            raise Excluded(reason, note)
        if e.code == 400 and "data sources accessible" in str(e):
            # Notion reports a database none of whose data sources are shared with
            # the integration as 400 validation_error, not 403 — an access verdict,
            # and a permanent one, so recording it as a retryable failure would put
            # it at the head of every future slice's queue forever.
            # Keep the nightly's own state in step, as both sibling paths do.
            ctx.state["unshared"][db_id] = refresh.now_iso()
            raise Excluded("no_access", f"HTTP 400 during coverage backfill {today()}: "
                                        f"no data source shared with the integration",
                           ambiguous=False)
        if e.code == 400 and "is a linked database" in str(e):
            # The other permanent 400: a linked view, which /databases refuses by
            # design. Same verdict the 404-then-block-probe path reaches, stated
            # directly by the API now.
            ctx.state["not_a_db"][db_id] = refresh.now_iso()
            raise Excluded("not_a_db", f"HTTP 400 during coverage backfill {today()}: "
                                       f"a linked database view, not queryable",
                           ambiguous=False)
        raise
    d.pop("request_id", None)
    title = refresh.plain(d.get("title")) or "Untitled"
    dirname = f"{refresh.sanitize(title)} {db_id}"
    final = os.path.join(refresh.DBS, dirname)
    if os.path.isdir(final):
        # a previous run captured it and died before recording progress
        return seed_have(ctx, db_id, final, sorted(os.listdir(final)))

    tmpname = partial_name(db_id)
    tmp = os.path.join(refresh.DBS, tmpname)
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    props = d.get("properties") or {}
    try:
        refresh.jsave(os.path.join(tmp, "_schema.json"),
                      {"id": db_id, "title": title, "database": d,
                       "data_sources": d.get("data_sources") or []})
        # a throwaway `discovered` set, as capture_new_db passes: what this run
        # follows is what the written artifacts reference, not relation targets
        refresh.refresh_db(ctx.api, ctx.users, db_id, tmpname, ctx.state,
                           ctx.report, ctx.args, set())
        nrows, csv_path = csv_row_count(tmp)
        if csv_path is None:
            raise RuntimeError("no CSV written — the row query failed; see report errors")
        with open(os.path.join(tmp, "_schema.md"), "w") as f:
            f.write(refresh.schema_md(title, db_id, nrows, props))
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    os.rename(tmp, final)
    refresh.update_all_schemas(title, db_id, nrows, props)

    return seed_have(ctx, db_id, final, sorted(os.listdir(final)))


def capture_page(ctx, pid):
    """First-time capture of one content page -> the path it wrote."""
    try:
        pg = ctx.api.get(f"/pages/{refresh.dashed(pid)}")
    except refresh.ApiError as e:
        if e.code in (403, 404):
            raise Excluded(*classify_refusal(ctx, pid, "sub_page", e))
        raise
    if pg.get("in_trash") or pg.get("archived"):
        raise Excluded("archived", f"archived/in_trash at coverage backfill {today()}")
    m = refresh.meta_of(pg, ctx.users)
    if m.get("parent_type") == "database_id":
        raise Excluded("deliberate", "this id is a database row, not a content page — "
                                     "the DB row sweep owns it, so the page mirror leaves it alone")
    pc = ctx.page_ctx()
    path, _w = refresh.walk_content_page(ctx.api, ctx.users, m, pc["page_index"],
                                         ctx.report, ctx.args, meta=pc["meta"],
                                         row_index=pc["row_index"])
    pc["meta"][pid] = m
    if pid not in pc["order"]:
        pc["order"].append(pid)
    pc["dirty"] = True
    ctx.have["page"].add(pid)
    ctx.report["pages"]["new"].append({"id": pid, "title": m.get("title", ""),
                                       "path": os.path.relpath(path, refresh.WS),
                                       "what": "new", "comments": 0})
    return [path]


def capture(ctx, item):
    """Dispatch on kind. `link_to_page` and `--only` ids of unknown kind carry a
    page id *or* a database id (notion_core/walker.py:194), so try both before giving up."""
    kind, id32 = item["kind"], item["id"]
    if kind == "child_database":
        return capture_database(ctx, id32)
    if kind == "sub_page":
        return capture_page(ctx, id32)
    try:
        return capture_page(ctx, id32)
    except Excluded as ex:
        if ex.reason != "deleted":  # archived, or a row: a real verdict, not "no such page"
            raise
        return capture_database(ctx, id32)


# ---------------------------------------------------------------- chain closure

def chain_refs(ctx, paths):
    """Ids referenced by artifacts just written that are still absent — the same
    scan the census runs, over the new files only."""
    out, seen = [], set()
    for p in paths:
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        for r in coverage_census.scan_text(os.path.relpath(p, refresh.WS), text):
            if r.id in seen or coverage_census.is_present(r.kind, r.id, ctx.have):
                continue
            if r.id in ctx.exclusions:
                continue
            seen.add(r.id)
            out.append({"id": r.id, "kind": r.kind, "title": r.title})
    return out


# ---------------------------------------------------------------- the run

def run(ctx, progress, items, budget_slice=None):
    """Work the queue until it empties, the budget runs out, or the slice ends.
    Returns a per-run tally. Progress is saved after every id."""
    tally = {"done": 0, "excluded": 0, "failed": 0, "stopped": ""}
    queue = list(items)
    queued = {i["id"] for i in queue}
    try:
        _work(ctx, progress, queue, queued, tally, budget_slice)
    finally:
        # pages-metadata.jsonl is a 24 MB rewrite, so it is written once per run
        # rather than once per page — but it must survive an exception, or a page
        # exists on disk with nothing tracking it.
        ctx.flush_pages()
    return tally


def _work(ctx, progress, queue, queued, tally, budget_slice):
    i = 0
    while i < len(queue):
        item = queue[i]
        i += 1
        id32 = item["id"]
        if settled(ctx.have, progress, item):
            continue
        if budget_slice is not None and (tally["done"] + tally["excluded"]) >= budget_slice:
            tally["stopped"] = "slice"
            break
        try:
            ctx.api.check_budget()
        except refresh.Budget:
            tally["stopped"] = "budget"
            break

        try:
            written = capture(ctx, item)
        except refresh.Budget:
            tally["stopped"] = "budget"
            break
        except Excluded as ex:
            record_exclusion(ctx, progress, id32, ex.reason, ex.note)
            tally["excluded"] += 1
            if ex.ambiguous:
                ctx.access_fails += 1
            save_progress(progress)
            if ctx.access_fails >= ACCESS_FAIL_LIMIT:
                tally["stopped"] = "access"
                break
            continue
        except (refresh.ApiError, OSError, RuntimeError) as e:
            f = progress["failed"].setdefault(id32, {"attempts": 0})
            f["attempts"] += 1
            f["error"] = str(e)[:300]
            f["at"] = refresh.now_iso()
            f["kind"] = item["kind"]
            tally["failed"] += 1
            save_progress(progress)
            continue

        ctx.access_fails = 0
        progress["failed"].pop(id32, None)
        extra = chain_refs(ctx, written)
        for e in extra:
            # `chain_refs` has already established each of these is absent under
            # the kind it was referenced as, so `settled` is the only thing left
            # that may drop one.
            if e["id"] in queued or settled(ctx.have, progress, e):
                continue
            queued.add(e["id"])
            queue.append(e)
            progress["pending_extra"].append(e)
        progress["done"][id32] = {"kind": item["kind"], "title": item.get("title", ""),
                                  "at": refresh.now_iso(), "artifacts": len(written),
                                  "chained": [e["id"] for e in extra]}
        progress["pending_extra"] = [e for e in progress["pending_extra"]
                                     if e["id"] not in progress["done"]
                                     and e["id"] not in progress["excluded"]]
        tally["done"] += 1
        save_progress(progress)
        refresh.log(f"backfilled {item['kind']} {id32[:8]} '{item.get('title', '')[:40]}' "
                    f"({len(written)} files, +{len(extra)} chained, req={ctx.api.n})")


# ---------------------------------------------------------------- reporting

def report_counts(split, progress):
    """The split as of now. `excluded here` is carved back out of the exclusion
    file's share: an id this backfill excluded left the work list, it was never
    outside it — so `to-backfill` stays the number the triage handed over."""
    here = set(progress["excluded"])
    pre = [e for e in split["excluded"] if e["id"] not in here]
    todo = [e for e in split["backfill"] if e["id"] not in progress["done"]]
    pending = [e for e in progress["pending_extra"]
               if e["id"] not in progress["done"] and e["id"] not in here]
    return {"absent": len(split["backfill"]) + len(split["excluded"]),
            "pre_excluded": len(pre),
            "to_backfill": len(split["backfill"]) + len(split["excluded"]) - len(pre),
            "done": len(progress["done"]),
            "excluded_here": len(progress["excluded"]),
            "failed": len(progress["failed"]),
            "remaining": len(todo),
            "chain_pending": len(pending)}


def print_report(c, out=sys.stdout):
    print(f"census absent      {c['absent']:6d}", file=out)
    print(f"  pre-excluded     {c['pre_excluded']:6d}", file=out)
    print(f"to-backfill        {c['to_backfill']:6d}", file=out)
    print(f"  done             {c['done']:6d}", file=out)
    print(f"  excluded here    {c['excluded_here']:6d}", file=out)
    print(f"  failed           {c['failed']:6d}", file=out)
    print(f"  remaining        {c['remaining']:6d}"
          + (f"  (+{c['chain_pending']} chain-discovered)" if c["chain_pending"] else ""),
          file=out)


# ---------------------------------------------------------------- entrypoint

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--budget", type=int, default=0,
                    help="max API requests for this run (required to write)")
    ap.add_argument("--slice", type=int, default=0, metavar="N",
                    help="stop after N ids are settled (done or excluded)")
    ap.add_argument("--only", nargs="+", metavar="ID32", default=None,
                    help="backfill exactly these ids (chain closure still applies)")
    ap.add_argument("--report", action="store_true",
                    help="print done / remaining / excluded-during-backfill and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be fetched; no API calls, no writes")
    ap.add_argument("--rps", type=float, default=3.0)
    ap.add_argument("--census", default=coverage_census.DEFAULT_OUT)
    ap.add_argument("--exclusions", default=coverage_census.DEFAULT_EXCLUSIONS)
    ap.add_argument("--lock", default=LOCK_PATH)
    args = ap.parse_args(argv)

    progress = load_progress()
    split = census_split(args.census, args.exclusions)

    if args.report:
        print_report(report_counts(split, progress))
        return 0

    if args.dry_run:
        # Same corpus walk the writing run does, so the preview and the run agree
        # about what is already captured. It costs ~15s of local reading and no
        # requests, which is the cheaper half of being honest.
        items = work_items(split, progress, coverage_census.present_ids(refresh.WS),
                           only=args.only)
        print(f"{len(items)} ids to fetch (no API calls made)")
        for it in items[:200]:
            print(f"  {it['kind']:15s} {it['id']}  {it.get('title', '')[:60]}")
        if len(items) > 200:
            print(f"  … +{len(items) - 200} more")
        return 0

    if not args.budget:
        ap.error("--budget is required for a writing run (use --dry-run to preview)")
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        print("NOTION_TOKEN not set", file=sys.stderr)
        return 2

    try:
        lock = take_lock(args.lock)
    except Locked as e:
        print(str(e), file=sys.stderr)
        return 1

    for name in sweep_partials():
        print(f"swept a partial capture left by an earlier run: {name}", file=sys.stderr)

    # Read the corpus under the lock and after the sweep: another writer holding
    # it a moment ago may have captured ids this run would otherwise re-fetch.
    have = coverage_census.present_ids(refresh.WS)
    items = work_items(split, progress, have, only=args.only)

    api = refresh.Api(token, args.rps, args.budget)
    users = refresh.Users(api)
    state = load_state()
    report = refresh.new_report("coverage-backfill")
    ctx = Ctx(api, users, state, report, args, args.exclusions, have=have)

    try:
        tally = run(ctx, progress, items, budget_slice=args.slice or None)
    finally:
        progress["requests"] = progress.get("requests", 0) + api.n
        save_progress(progress)
        save_state(state)
        users.save()
        lock.close()

    for e in report["dbs"]["errors"]:
        print(f"  ERROR {e['db']} [{e['op']}]: {e['error']}", file=sys.stderr)
    stopped = {"budget": "request budget exhausted — a normal stop, re-run to continue",
               "slice": "slice complete",
               "access": f"stopped after {ACCESS_FAIL_LIMIT} consecutive refusals — "
                         f"check the integration's access before excluding more ids"}
    if tally["stopped"]:
        print(stopped[tally["stopped"]], file=sys.stderr)
    print(f"this run: {tally['done']} backfilled, {tally['excluded']} excluded, "
          f"{tally['failed']} failed, {api.n} requests, {api.r429} 429s")
    print_report(report_counts(census_split(args.census, args.exclusions), progress))
    return 0


if __name__ == "__main__":
    sys.exit(main())
