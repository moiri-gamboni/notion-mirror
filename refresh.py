#!/usr/bin/env python3
"""Incremental full-fidelity refresh of the notion/ mirror.

Keeps workspace/ (pages, _databases/, _comments.md), _meta/ (pages-metadata.jsonl,
content-pages.tsv) current against the live Notion workspace, touching only
what changed so git diffs stay meaningful and API usage stays polite.

Design:
  * DB rows: full per-DB /query sweep every run (~800 req for 74k rows). /v1/search
    provably misses whole DBs (64 of 407 at build time), so per-DB queries are the
    only reliable change/deletion source. CSVs re-render preserving existing column
    and row order (new rows appended by created_time) -> minimal diffs. A per-row
    last_edited_time state file triggers body re-probes for changed rows.
  * Content pages: a full /v1/search sweep every run rebuilds pages-metadata.jsonl
    + content-pages.tsv and detects deletions (verified via GET /pages before
    deleting anything locally). Changed pages are re-walked notion_walk-style with
    attachment download in the same pass. Automation subtrees are captured once
    and never re-walked.
  * Comments: do NOT bump last_edited_time and the API has no global listing, so
    reading them costs one request per block. The webhook capture log is folded
    in every run at no cost; a rolling per-block audit sized by count (1/3 of the
    human pages, 1/4 of the comment-bearing rows per night) catches resolution,
    which no event reports. Body probes carry comments over. Resolved comments
    are kept, annotated.
  * No request budget: the run does what the mirror needs, paced at --rps with
    Retry-After honoured, and the report says where every request went.

Formats replicate the Jul-08..10 build exactly (see README.md):
  _databases/<Title> <dbid32>/{_schema.json,_schema.md,<Title> <dbid32>.csv,
  <RowTitle> <rowid32>.md}; row .md = header comment + props table (non-empty,
  schema order) + '<!-- body+comments fetched -->' + optional ## Body / ## Comments.

Env: NOTION_TOKEN required. Stdlib only, plus the sibling `coverage_census`
module for the coverage assert.
"""
import argparse
import collections
import concurrent.futures
import contextlib
import csv
import datetime as dt
import hashlib
import io
import json
import math
import os
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request

# Sibling module in the same directory (`notion-mirror` is not a package, so this
# resolves off sys.path[0] however the script is invoked). It holds the one
# definition of what counts as a reference and as a captured artifact; the
# coverage assert reuses it rather than growing a second copy that could drift
# away from the census and the backfill.
import coverage_census
# The primitives, the HTTP client, the two renderers, the row-file grammar and the run
# config live in `notion_core/` — one definition each, importable without this module.
# tasksync imports them from there directly; they are re-exported here because every
# sibling script and every test reads them off `refresh.`, and because the tests patch
# some of them (`refresh.Api`, `refresh.expand_truncated_props`) as module globals,
# which only works while the name is a global of this module.
from notion_core.api import (API, VER, VER_LATEST, Api, ApiError, Budget,  # noqa: F401
                             Truncated)
from notion_core.flatten import (PAGINATED_PROP_TYPES, cell, expand_truncated_props,  # noqa: F401
                                 fmt_date, fmt_num, md_cell)
from notion_core.richtext import absorbs_a_paragraph, md_link, plain, rich_md  # noqa: F401
from notion_core.rowmd import (BODY_CLOSE, BODY_OPEN, CID_LEGACY, CID_MARK,  # noqa: F401
                               COMMENTS_CLOSE, COMMENTS_OPEN, MARKER, RESOLVED_MARK,
                               annotate_resolved, bullet_cid, bullet_text_key, cid_trailer,
                               extract_comments_body, split_bullets, stamp_cid,
                               strip_comments_section)
from notion_core.runcfg import parse_row_ids  # noqa: F401
from notion_core.util import UTC, dashed, log, undash  # noqa: F401
from notion_core.walker import Comment, Walker  # noqa: F401
# The data roots. Importing this asserts the mirror: the engine is its own clone, so
# where the code sits says nothing about where the mirror is, and a wrong root would be
# materialised rather than refused (every writer below `makedirs` what it is handed).
import mirror_root
import paths

NOTION = paths.NOTION
WS = paths.WS
DBS = paths.DBS
META = paths.META
STATE = paths.STATE

ID32 = re.compile(r"([0-9a-f]{32})(?:\.md|\.csv)?$")

# Set by main() for `--dry-run`. The phases guard their own writes with
# args.dry_run; this reaches the helpers several calls below any `args` — the
# attachment download, a page's folder, the schema files, the consumed webhook
# DB events — so a dry run leaves the mirror exactly as it found it (its own
# `last-run-report.dry-run.*` aside) while still issuing every read a real run
# would, which is what makes its request counts an estimate of one.
DRY_RUN = False


def now_iso():
    return dt.datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# ---------------------------------------------------------------- state & utils

def jload(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def jsave(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def sanitize(title, cap=150):
    t = (title or "untitled").strip()
    t = re.sub(r'[/\\|:*?"<>\x00-\x1f]', " ", t)
    t = t.strip().strip(".")
    return (t or "untitled")[:cap].rstrip()


class Users:
    def __init__(self, api):
        self.api = api
        self.path = os.path.join(STATE, paths.USERS)
        self.map = jload(self.path, {})

    def name(self, ref):
        if not ref:
            return ""
        uid = ref.get("id", "") if isinstance(ref, dict) else ref
        if not uid:
            return ""
        if isinstance(ref, dict) and ref.get("name"):
            self.map[uid] = ref["name"]
            return ref["name"]
        if uid not in self.map:
            try:
                d = self.api.get(f"/users/{uid}")
                # build convention: unresolvable name -> the full dashed uuid
                self.map[uid] = d.get("name") or ("(bot)" if d.get("type") == "bot" else uid)
            except ApiError:
                # cache the fallback: deleted users 404 forever — never re-fetch
                self.map[uid] = uid
        return self.map[uid]

    def save(self):
        jsave(self.path, self.map)


# ---------------------------------------------------------------- attachments

def download_attachments(walker, lines, dest_dir, prefix_for, report):
    """Resolve ATTACH: placeholders -> relative filenames, downloading new files."""
    if not walker.attachments or DRY_RUN:
        return [ln for ln in lines]
    os.makedirs(dest_dir, exist_ok=True)
    existing = os.listdir(dest_dir)
    mapping = {}
    for i, (bid, kind, url, cap) in enumerate(walker.attachments, 1):
        pref = prefix_for(bid, i)
        hit = next((f for f in existing if f.startswith(pref)), None)
        if hit:
            mapping[bid] = hit
            continue
        base = urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]) or f"{kind}.bin"
        name = f"{pref}{sanitize(base, 80)}"
        target = os.path.join(dest_dir, name)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "notion-mirror-refresh"})
            with urllib.request.urlopen(req, timeout=300) as r, open(target + ".part", "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    if f.tell() > 800 * (1 << 20):
                        raise OSError("attachment exceeds 800MB cap")
            os.replace(target + ".part", target)
            mapping[bid] = name
            sz = os.path.getsize(target)
            report["attachments"]["downloaded"].append({"file": os.path.join(os.path.basename(dest_dir), name), "bytes": sz})
            if sz > 100 * (1 << 20):
                gi = os.path.join(NOTION, ".gitignore")
                cur = open(gi).read() if os.path.exists(gi) else ""
                if name not in cur:
                    with open(gi, "a") as f:
                        f.write(f"{name}\n")
                report["notes"].append(f"attachment >100MB kept on disk, gitignored: {name}")
        except Exception as e:  # noqa: BLE001 - network/S3 failures must not kill the run
            for suffix in (".part", ""):
                try:
                    os.remove(target + suffix)
                except OSError:
                    pass
            report["attachments"]["failed"].append({"block": bid, "error": str(e)[:200]})
            # keep the URL rather than nothing, but strip signed-credential query
            # params (X-Amz-Signature etc.) — they expire in 1h and are short-lived
            # credentials we must not commit
            pu = urllib.parse.urlsplit(url)
            keep = [(k, v) for k, v in urllib.parse.parse_qsl(pu.query)
                    if not k.lower().startswith("x-amz") and k.lower() not in
                    ("signature", "sig", "policy", "key-pair-id", "token", "file_token")]
            mapping[bid] = urllib.parse.urlunsplit(
                (pu.scheme, pu.netloc, pu.path, urllib.parse.urlencode(keep), ""))
    out = []
    for ln in lines:
        m = re.search(r"\(ATTACH:([0-9a-f]{32})\)", ln)
        if m:
            ref = mapping.get(m.group(1), "")
            ln = ln.replace(f"ATTACH:{m.group(1)}", urllib.parse.quote(ref) if not ref.startswith("http") else ref)
        out.append(ln)
    return out


# ---------------------------------------------------------------- DB mirror

def db_dirs():
    out = {}
    for d in sorted(os.listdir(DBS)):
        full = os.path.join(DBS, d)
        if not os.path.isdir(full):
            continue
        m = ID32.search(d)
        if m:
            out[m.group(1)] = d
    return out


def read_csv(path):
    if not os.path.exists(path):
        return [], {}
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return [], {}
    header = rows[0]
    data = {}
    for r in rows[1:]:
        if r:
            data[r[0]] = r
    return header, data


def row_title(page, users):
    for pv in (page.get("properties") or {}).values():
        if pv.get("type") == "title":
            return plain(pv.get("title"))
    return ""


def index_row_mds(dirpath):
    out = {}
    for f in os.listdir(dirpath):
        if f.endswith(".md") and not f.startswith("_schema"):
            m = ID32.search(f)
            if m:
                out[m.group(1)] = f
    return out


def row_render_target(dirname, mds):
    """Locate a row's mirror directory: (path, DB title, row-md index).

    `mds` is a caller-owned dirpath -> index cache, populated here: a per-row
    listdir is O(n^2) on a big DB. Title falls back to the dir name minus its
    id suffix, for a dir whose _schema.json has not been written yet.

    Pure — no API calls, so it cannot raise ApiError. Every request
    a row costs stays visible at the call site, where the recovery lives.
    """
    dirpath = os.path.join(DBS, dirname)
    title = jload(os.path.join(dirpath, "_schema.json"), {}).get("title") \
        or dirname.rsplit(" ", 1)[0]
    idx = mds.get(dirpath)
    if idx is None:
        idx = mds[dirpath] = index_row_mds(dirpath)
    return dirpath, title, idx


def body_section_lines(body):
    """The `## Body` region as `parts` entries (see probe_row)."""
    return ["", "## Body", "", BODY_OPEN, *body.split("\n"), BODY_CLOSE]


def comments_section_lines(bullets):
    """The `## Comments` region as `parts` entries (see probe_row), one bullet per
    comment (`reconcile_bullets`)."""
    return ["", "## Comments", "", COMMENTS_OPEN, *reconcile_bullets(bullets), COMMENTS_CLOSE]


def body_section(body):
    """The exact bytes probe_row emits for a body, as a standalone string.
    The migration and the repair scripts render through here so they cannot
    drift from the renderer."""
    return "\n" + "\n".join(body_section_lines(body))


def comments_section(bullets):
    """The exact bytes probe_row emits for a comment list, as a string."""
    return "\n" + "\n".join(comments_section_lines(bullets))


def has_comments(enrichment):
    """Does this enrichment (or whole row file) carry a comments region?

    Delimiter-first: in a delimited file the delimiters are the whole answer, so
    a body that merely *contains* the text `## Comments` can no longer enrol its
    row in the comment-audit pool for good. The heading test is the fallback for
    files written before the migration."""
    t = enrichment or ""
    if COMMENTS_OPEN in t:
        return True
    if BODY_OPEN in t:
        return False
    return "\n## Comments" in t


def stored_comments_body(enrichment):
    """The stored comments region's bullet text. A delimited file with no comments
    region has none: `extract_comments_body` would otherwise fall back to the
    `## Comments` heading regex, which matches a heading inside the body and
    reads body lines as comments."""
    t = enrichment or ""
    if COMMENTS_OPEN not in t and BODY_OPEN in t:
        return ""
    return extract_comments_body(t)


def has_enrichment(txt):
    """Does this row file carry a body or comments region? (db_probe_policy's
    'this row is worth probing' rule.)"""
    t = txt or ""
    return (COMMENTS_OPEN in t or BODY_OPEN in t
            or "\n## Body" in t or "\n## Comments" in t)


# Every row file says it is generated and where the working copy lives. Mirror
# rows and tasks/<slug>/task.md bodies look alike, so an agent (or a person) will
# eventually edit the mirror copy expecting it to reach Notion; nothing pushes it
# and the next probe overwrites it without a word.
#
# The stamp is the row's own last_edited_time, NOT the time of the run that wrote
# the file. That is load-bearing in three places: render_row_md stays a pure
# function of `page`, so the golden fixtures can pin exact bytes; `--mode validate`
# compares a fresh render against the stored file with no masking, which a clock
# would make fail on every row; and upsert_row_md writes only when the rendered
# bytes differ, so a clock would rewrite all ~83k rows nightly. What this stamp
# cannot tell you is when the mirror last *looked* at the row — that is the
# hourly job's dead-man in health-check.sh, and _meta/state/last-run.json.
GENERATED_HEADER_RE = re.compile(r"^<!-- notion:generated .*-->$", re.M)


def generated_header(page):
    le = (page or {}).get("last_edited_time") or "unknown"
    return (f"<!-- notion:generated row_last_edited={le} | generated file: edits here are "
            "overwritten on the next mirror run. If tasks/ holds a folder for this row, "
            "tasks/<slug>/task.md is the working copy. -->")


def render_row_md(page, db_id, db_title, cols, users, enrichment):
    """enrichment = the verbatim text that follows the marker (starting with its
    newline), or ''/None for none. Preserved byte-exactly across re-renders."""
    pid = undash(page["id"])
    props = page.get("properties") or {}
    title = row_title(page, users)
    lines = [f"<!-- notion db row | id: {pid} | db: {db_id} ({db_title}) -->",
             generated_header(page),
             f"# {title or 'untitled'}", "",
             "| Property | Value |", "|---|---|"]
    for c in cols:
        val = md_cell(cell(props.get(c), users))
        if val:
            lines.append(f"| {md_cell(c)} | {val} |")
    lines.append("")
    lines.append(MARKER)
    head = "\n".join(lines)
    return head + (enrichment if enrichment else "\n")


def existing_enrichment(path):
    """Verbatim text after the marker (including its leading newline), or None."""
    try:
        txt = open(path).read()
    except OSError:
        return None
    if MARKER not in txt:
        return None
    return txt.split(MARKER, 1)[1]


# A row whose probe didn't complete keeps its stored body/comments — otherwise a
# transient 500 would blank enrichment the mirror can't cheaply re-fetch. Without
# a marker, "unchanged since last run" and "we never got to look" are the same
# bytes on disk, so a row can sit stale (or, after a format migration, reverted)
# indefinitely with nothing to show for it.
PROBE_FAIL_RE = re.compile(r"^_\[probe failed (\d{4}-\d{2}-\d{2})\]_$", re.M)
_ENRICH_HEADING = re.compile(r"^## (?:Body|Comments)$", re.M)


def probe_annotation(enrichment):
    """Date carried by the probe-failure annotation, or None if there is none."""
    m = PROBE_FAIL_RE.search(enrichment or "")
    return m.group(1) if m else None


def strip_probe_annotation(enrichment):
    """`enrichment` without the probe-failure annotation (and the blank line that
    follows it). Idempotent, and safe on text carrying none.

    Anything measuring body shape must call this first: the annotation is
    refresh metadata, not body content, so leaving it in skews any measurement
    taken over the body's lines.

    Bounded to the one line `annotate_probe_failure` writes into, rather than
    scanning the whole text: since the indent-0 walk (A3) a body line sits at the
    same left margin as the annotation, so an unbounded strip silently deletes a
    real body line that happens to read like one.

    That slot is the first non-blank line, or the one after it when the first is
    an enrichment heading. Both forms occur: callers pass whole enrichment
    (heading first), and anything splitting the enrichment into regions passes
    fragments with the heading already consumed (annotation first). Deciding on
    the first non-blank line rather than on a heading found anywhere matters — a
    fragment can carry a later heading of its own, and keying off that one skips
    past the annotation leading the fragment."""
    if not enrichment or "_[probe failed " not in enrichment:
        return enrichment
    lines = enrichment.split("\n")
    filled = [i for i, ln in enumerate(lines) if ln.strip()]
    if not filled:
        return enrichment
    at = filled[1] if _ENRICH_HEADING.match(lines[filled[0]]) and len(filled) > 1 else filled[0]
    if not PROBE_FAIL_RE.match(lines[at]):
        return enrichment
    del lines[at]
    if at < len(lines) and not lines[at].strip():
        del lines[at]
    return "\n".join(lines)


def annotate_probe_failure(enrichment, day):
    """Mark stored enrichment as carried over from a probe that didn't complete.
    Replaces any earlier annotation, so re-marking a still-failing row moves the
    date rather than stacking a second line.

    The line goes immediately under the first `## Body`/`## Comments` heading —
    above any body delimiter, so it stays outside the rendered-body region.
    Enrichment with neither heading is returned unchanged: there is no stale
    state to flag, and text after the marker is exactly what db_probe_policy's
    enriched-only rule reads as "this row has a body, keep probing it" — which
    would put every feed row that rule exists to skip back into the probe set."""
    base = strip_probe_annotation(enrichment)
    m = _ENRICH_HEADING.search(base or "")
    if not m:
        return enrichment if base is None else base
    head, tail = base[:m.end()], base[m.end():].lstrip("\n")
    return f"{head}\n\n_[probe failed {day}]_\n" + (f"\n{tail}" if tail else "")


def probe_row(api, users, page_id, dest_dir, report, old_comments_body="",
              max_blocks=800, discovered=None, comments="scan", last_scan="", pool=None):
    """Fetch body + comments for a DB row -> enrichment string ('' if none).

    `comments="scan"` reads them from the API: page-level, then one request per
    block, however many blocks there are. Old comments never vanish: ones the
    API stopped listing are kept, annotated resolved/deleted.

    `comments="carry"` reads none: the stored comments come over from disk with
    the row's webhook captures folded in (`fold_bullets`, against `last_scan`),
    the way a props probe carries the body. A body edit says nothing about the
    row's comments, which reach the mirror through the capture log and, for
    resolution, the rolling audit."""
    w = Walker(api, users, max_blocks=max_blocks)
    body_lines = []
    # indent 0: a row body's top level is the file's left margin, so leading
    # spaces mean nesting depth and nothing else. Walking at 1 put every line
    # two spaces in and made depth read one level too deep for anything parsing
    # the mirror back into blocks.
    w.walk(page_id, body_lines, 0)
    # An inline database in a row body is a real database with real rows, and
    # until it reaches `discovered` the mirror renders a stand-in pointing at
    # nothing it ever captured — the class behind the coverage census. Harvest
    # here rather than after the comment scan, so a comment-side failure cannot
    # cost us the discovery. `phase_content` does the same for content pages.
    if discovered is not None:
        for did, _t in w.child_dbs:
            discovered.add("db:" + did)
    parts = []
    body = "\n".join(body_lines).strip("\n")
    if body.strip():
        parts += body_section_lines(body)
    today = dt.datetime.now(UTC).strftime("%Y-%m-%d")
    if comments == "carry":
        flat, _st = fold_bullets(split_bullets(old_comments_body, prefix="- _"),
                                 webhook_captures().get(undash(page_id), []), last_scan,
                                 users, True, {}, today)
        return _finish_row_probe(w, parts, flat, page_id, dest_dir, report, max_blocks)
    page_comments = w.comments_for(page_id)
    block_comments, _capped = w.harvest_comments(pool=pool)
    flat = []
    for _anchor, cs in ([("(page)", page_comments)] if page_comments else []) + block_comments:
        for c in cs:
            t = c.text.replace("\r", "").replace("\n", "\n  ")
            flat.append(stamp_cid(f"- _{c.who} ({c.when[:10]}):_ {t}", c.cid, c.did))
    # a comment reachable both page-level and through its anchor block is one
    # comment: with ids that is now decidable, and the invariant this whole
    # change buys is that no two surviving bullets on a row share an id
    flat = dedup_by_cid(flat)
    flat = union_captured(flat, undash(page_id), users, row_format=True)
    flat, kept = merge_comment_bullets(old_comments_body, flat, today, prefix="- _")
    report["comments"]["retained"] += kept
    return _finish_row_probe(w, parts, flat, page_id, dest_dir, report, max_blocks)


def _finish_row_probe(w, parts, flat, page_id, dest_dir, report, max_blocks):
    """The enrichment string from a walked body and its final comment bullets."""
    if flat:
        parts += comments_section_lines(flat)
    if w.truncated:
        report["notes"].append(f"row {undash(page_id)[:8]} body truncated at {max_blocks} blocks")
    if w.attachments:
        parts = download_attachments(
            w, parts, dest_dir,
            prefix_for=lambda bid, i, pid=undash(page_id)[:8]: f"{pid}-{i}_",
            report=report)
    if not parts:
        return ""
    return "\n\n" + "\n".join(parts).strip("\n") + "\n"


def db_probe_policy(dirpath, nrows, state, db_id):
    """Probe mode for a DB's changed rows: 'all' (small DBs, or ≥5% of rows carry
    Body/Comments — meeting notes, templates, contact-style tables) or 'enriched'
    (big DBs where enrichment is rare — job feeds, signups, click logs: probe only rows
    whose .md already carries enrichment, so feed churn costs zero probes).
    Cached 7 days in state."""
    cache = state["probe_policy"]
    ent = cache.get(db_id)
    if ent and isinstance(ent.get("mode"), str) \
            and ent.get("ts", "") > (dt.datetime.now(UTC) - dt.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S"):
        return ent["mode"]
    mode = "all"
    if nrows >= 300:
        total = enriched = 0
        for f in os.listdir(dirpath):
            if f.endswith(".md") and not f.startswith("_schema"):
                total += 1
                try:
                    txt = open(os.path.join(dirpath, f)).read()
                except OSError:
                    continue
                if has_enrichment(txt):
                    enriched += 1
        if total and enriched / total < 0.05:
            mode = "enriched"
    cache[db_id] = {"mode": mode, "ts": now_iso()}
    return mode


def load_source_catalog(api, report):
    """Every shared data source, by database id32, from one data-source search:
    `api.catalog` for the rest of the run. A search result is the whole object
    `GET /data_sources/{id}` returns, so a database the catalog lists with one
    source needs no read of its own for its source id (`query_db_rows`), its
    schema (`get_database`) or its discovery (`phase_discovery`). A failed
    search leaves no catalog, and every database is read directly."""
    cat = {}
    try:
        for r in api.paginate("POST", "/search", body={
                "filter": {"value": "data_source", "property": "object"},
                "sort": {"timestamp": "last_edited_time", "direction": "descending"},
                "page_size": 100}):
            did = undash(((r.get("parent") or {}).get("database_id")) or r["id"])
            cat.setdefault(did, []).append(r)
    except ApiError as e:
        api.catalog = None
        report["notes"].append(f"data-source search failed, every database read directly: {e}"[:200])
        return
    api.catalog = cat


def catalog_source(api, db_id):
    """The one data source the run's catalog lists for a database, or None: no
    catalog, not listed, or more than one source (those are read directly, so a
    multi-source database's container title and source order stay authoritative)."""
    cat = getattr(api, "catalog", None)
    srcs = cat.get(undash(db_id)) if isinstance(cat, dict) else None
    return srcs[0] if srcs and len(srcs) == 1 else None


def query_db_rows(api, db_id):
    """All rows of a DB: every data source it holds, queried in turn (at
    2025-09-03 and later a database is a container and rows live in its data
    sources; `GET /databases/{id}` lists them, unless the run's catalog already
    names the one source).

    Complete past Notion's 10,000-results-per-query cap: `Api.query_rows` windows
    by created_time, so a big DB's overflow rows can no longer be mistaken for
    deletions (which is how ~630 live rows of a signups feed got tombstoned in
    Aug 2026). An unwindowable truncation raises Truncated instead of returning
    a short set, and so does a multi-source database that lists no data source
    to query: the empty row set that produced would diff every live row of it
    as deleted, which is the same failure by a different road.

    A wiki database lists its child databases among its rows, each as the
    child's data source; `as_database` puts one back under its database's id,
    as 2022-06-28 listed it, so its row file keeps its name and its body read
    does not 404 on a data-source id."""
    src = catalog_source(api, db_id)
    if src:
        d, srcs = None, [{"id": src["id"]}]
    else:
        d = api.get(f"/databases/{dashed(db_id)}")
        srcs = [s for s in d.get("data_sources") or [] if isinstance(s, dict) and s.get("id")]
    if not srcs:
        # the report line this lands in is itself truncated at 200 chars
        raise Truncated(f"/databases/{db_id}: the database lists no data source to query")
    rows = []
    for s in srcs:
        for r in api.query_rows(f"/data_sources/{s['id']}/query"):
            if r.get("object") == "data_source":
                r = as_database(r, (r.get("parent") or {}).get("database_id") or r["id"],
                                r.get("database_parent"))
            rows.append(r)
    return rows, {"data_sources": srcs, "database": d}


def schema_md(title, db_id, nrows, props, sources=None):
    lines = [f"# Schema — {title}", "",
             f"`db {db_id}` · {nrows} rows · {len(props)} properties", ""]
    if sources and len(sources) > 1:
        # only a multi-source database carries this line; its properties below
        # are every source's, merged in source order
        lines += ["Data sources: " + ", ".join(f"{s.get('name') or 'untitled'} `{undash(s['id'])}`"
                                               for s in sources), ""]
    lines += ["| Property | Type | Detail |", "|---|---|---|"]
    for name, spec in props.items():
        t = spec.get("type", "")
        detail = ""
        if t == "relation":
            detail = f"→ db {undash((spec.get('relation') or {}).get('database_id', ''))}"
        elif t in ("select", "multi_select", "status"):
            opts = (spec.get(t) or {}).get("options") or []
            if opts:
                detail = "options: " + ", ".join(o.get("name", "") for o in opts)
        elif t == "formula":
            detail = (spec.get("formula") or {}).get("expression", "") or ""
            if detail:
                detail = f"= {detail}"
        elif t == "rollup":
            r = spec.get("rollup") or {}
            detail = f"{r.get('function', '')} of {r.get('relation_property_name', '')}.{r.get('rollup_property_name', '')}".strip()
        elif t == "dual_property":
            detail = ""
        lines.append(f"| {name.replace('|', ' ')} | {t} | {detail.replace('|', '/')[:2000]} |")
    return "\n".join(lines) + "\n"


def data_source_stubs(stored, fresh=None):
    """A database's data sources, by id and nothing else.

    Schemas do not belong here. An *id* is safe to carry: it is fixed for the
    life of the data source, and it is what the block is read for, since rows
    are addressed by data source. A *schema* is not. The blocks written before this engine existed
    held whole `GET /v1/data_sources/{id}` responses, properties included, and
    `old.get("data_sources") or …` kept them: measured 2026-09-22, 100 of the
    401 blocks no longer matched the `database.properties` sitting beside them
    — one short by 46 of that database's 91 properties, the furthest 208 days
    behind the database's own last edit — with nothing in the file saying which
    of the two a reader should believe. A name would go the same way
    (a data source can be renamed) and is not kept either, which also keeps the
    block identical whichever caller writes it — `phase_dbs` runs before
    `phase_schema_sweep` in the same process, so a field only one of them can
    supply would be written and then stripped again before the run commits.

    An empty `fresh` falls back to `stored`: every Notion database has at least
    one data source, so an empty live list is a bad answer, not news, and it
    must not erase ids nothing else can re-fetch.
    """
    return [{"id": s["id"]} for s in (fresh or stored or [])
            if isinstance(s, dict) and s.get("id")]


def as_database(ds, db_id, parent):
    """A data source object under its database's id and parent: the shape of a
    2022-06-28 database object, field for field and in the same order, but for
    `archived` (removed by 2026-03-11; `in_trash` carries it)."""
    v = {k: val for k, val in ds.items() if k not in ("database_parent", "request_id")}
    v.update(object="database", id=db_id, parent=parent)
    return v


def get_database(api, db_id):
    """A database's schema -> (database block, its live data sources).

    `GET /databases/{id}` is the container: it lists the data sources, and its
    title, icon and description are the container's own, not the collection's
    the mirror has always shown — a blank title reads "New database", and a
    database mention reads as a page mention "Untitled". A single-source
    database's block is therefore its data source, via `as_database`: the block
    2022-06-28 served. A multi-source database's is the container, with every
    source's properties merged in source order under `properties` (first source
    wins a shared name), which is what the row query's merged rows and the CSV
    columns need; `schema_md` names its sources. A single source the run's
    catalog lists costs no request: the search returned the same object."""
    src = catalog_source(api, db_id)
    if src:
        return (as_database(src, dashed(db_id), src.get("database_parent")),
                [{"id": src["id"], "name": plain(src.get("title"))}])
    d = api.get(f"/databases/{dashed(db_id)}")
    sources = [s for s in d.get("data_sources") or [] if isinstance(s, dict) and s.get("id")]
    if not sources:
        # an empty schema would blank the schema files; keep the stored one
        raise Truncated(f"/databases/{db_id}: the database lists no data source")
    if len(sources) == 1:
        ds = api.get(f"/data_sources/{sources[0]['id']}")
        return as_database(ds, d.get("id") or dashed(db_id), ds.get("database_parent") or d.get("parent")), sources
    props = {}
    for s in sources:
        ds = api.get(f"/data_sources/{s['id']}")
        for name, spec in (ds.get("properties") or {}).items():
            props.setdefault(name, spec)
    d["properties"] = props
    return d, sources


def refresh_schema_files(api, dirpath, db_id, title, nrows, report, force=False,
                         data_sources=None):
    """GET schema; rewrite _schema.json/_schema.md if content changed.
    Returns (props, live_title).

    `data_sources` is a live list for a caller that has one — `query_db_rows`
    fetches it for every multi-source database — and None for a caller that
    does not, which carries the recorded ids over. See `data_source_stubs`."""
    spath = os.path.join(dirpath, "_schema.json")
    old = jload(spath, {})
    try:
        d, sources = get_database(api, db_id)
    except ApiError as e:
        report["dbs"]["errors"].append({"db": title, "op": "schema", "error": str(e)[:200]})
        return (old.get("database") or {}).get("properties") or {}, title
    d.pop("request_id", None)
    live_title = plain(d.get("title")) or title
    # fresh first, recorded second: the old merge had both arms the other way
    # round, so the recorded value won and the response's was never reached.
    new = {"id": db_id, "title": live_title, "database": d,
           "data_sources": data_source_stubs(old.get("data_sources"),
                                             data_sources or sources)}
    oldn = dict(old.get("database") or {})
    oldn.pop("request_id", None)
    # data_sources is in the predicate, not just in `new`: without it a corrected
    # block would be computed and dropped on every database whose schema is steady.
    if force or oldn != d or old.get("title") != live_title \
            or old.get("data_sources") != new["data_sources"]:
        props = d.get("properties") or {}
        if oldn.get("properties") != d.get("properties"):
            report["dbs"]["schema_changed"].append(live_title)
        if not DRY_RUN:
            jsave(spath, new)
            with open(os.path.join(dirpath, "_schema.md"), "w") as f:
                f.write(schema_md(live_title, db_id, nrows, props, sources))
            update_all_schemas(live_title, db_id, nrows, props, sources)
    return d.get("properties") or {}, live_title


def update_all_schemas(title, db_id, nrows, props, sources=None):
    path = os.path.join(DBS, "_ALL-SCHEMAS.md")
    txt = open(path).read() if os.path.exists(path) else "# All database schemas\n\n"
    body = schema_md(title, db_id, nrows, props, sources).split("\n", 2)[2]  # drop '# Schema —' header
    section = f"## {title}  `{db_id}`  ({nrows} rows, {len(props)} props)\n{body}"
    pat = re.compile(r"^## .*`" + db_id + r"`.*?(?=^## |\Z)", re.M | re.S)
    if pat.search(txt):
        txt = pat.sub(lambda _m: section + "\n", txt, count=1)
    else:
        txt = txt.rstrip("\n") + "\n\n" + section + "\n"
    with open(path, "w") as f:
        f.write(txt)


def refresh_db(api, users, db_id, dirname, state, report, args, discovered):
    dirpath = os.path.join(DBS, dirname)
    schema = jload(os.path.join(dirpath, "_schema.json"), {})
    title = schema.get("title") or dirname.rsplit(" ", 1)[0]
    csv_path = None
    for f in os.listdir(dirpath):
        if f.endswith(".csv") and ID32.search(f):
            csv_path = os.path.join(dirpath, f)
            break
    header, old_rows = read_csv(csv_path) if csv_path else ([], {})

    try:
        rows, ds_extra = query_db_rows(api, db_id)
    except Truncated as e:
        # An incomplete row set must never reach the deletion diff below — every
        # missing row would be tombstoned and its artifacts removed.
        report["dbs"]["errors"].append({"db": title, "op": "query",
                                        "error": f"row sweep truncated, refresh skipped (nothing diffed as deleted): {e}"[:200]})
        return
    except ApiError as e:
        if e.code == 404:
            strikes = state["db404"].get(db_id, 0) + 1
            state["db404"][db_id] = strikes
            if strikes >= 2:
                report["dbs"]["deleted"].append({"title": title, "id": db_id,
                                                 "note": "404 twice — deleted or un-shared; local dir removed"})
                if not args.dry_run:
                    shutil.rmtree(dirpath, ignore_errors=True)
                    remove_all_schemas_section(db_id)
                state["rows"].pop(db_id, None)
            else:
                report["dbs"]["errors"].append({"db": title, "op": "query", "error": "404 (strike 1 — will remove on 2nd)"})
            return
        report["dbs"]["errors"].append({"db": title, "op": "query", "error": str(e)[:200]})
        return
    state["db404"].pop(db_id, None)

    # relation targets in the schema -> database discovery
    for spec in ((schema.get("database") or {}).get("properties") or {}).values():
        if spec.get("type") == "relation":
            tgt = undash((spec.get("relation") or {}).get("database_id", ""))
            if tgt:
                discovered.add("db:" + tgt)

    by_id = {}
    for r in rows:
        by_id[undash(r["id"])] = r

    # columns: keep existing order; add new property keys (schema order if known)
    prop_names = []
    seen = set()
    schema_props = list(((schema.get("database") or {}).get("properties") or {}).keys())
    for r in rows:
        for k in (r.get("properties") or {}).keys():
            if k not in seen:
                seen.add(k)
                prop_names.append(k)
    cols = [c for c in header[1:] if c in seen or not rows] if header else []
    missing_cols = [c for c in (header[1:] if header else []) if c not in seen and rows]
    ordered_new = [k for k in schema_props if k in seen and k not in cols] + \
                  [k for k in prop_names if k not in cols and k not in schema_props]
    if missing_cols:
        # property removed from schema? verify before dropping. The row query
        # has already paid for a live data-source list if this database has more
        # than one; hand it over rather than let the schema file carry old ids.
        props, title = refresh_schema_files(api, dirpath, db_id, title, len(rows), report,
                                            data_sources=(ds_extra or {}).get("data_sources"))
        still = [c for c in missing_cols if c in props]
        cols += still  # keep (rows just don't carry it); drop the truly-removed
        if len(still) < len(missing_cols):
            report["dbs"]["schema_changed"].append(f"{title} (columns dropped: {sorted(set(missing_cols) - set(still))})")
    cols += ordered_new
    if not header:
        cols = [k for k in schema_props if k in seen] + [k for k in prop_names if k not in schema_props]

    rstate = state["rows"].get(db_id, {})
    new_rstate = {}
    is_existing_csv = bool(header)
    probe_mode = db_probe_policy(dirpath, len(rows), state, db_id)
    probes_skipped = 0
    md_idx = index_row_mds(dirpath)  # hoisted: per-row listdir on big DBs is O(n^2)
    changed, added, deleted = [], [], []
    out_rows = []
    new_ids = [i for i in by_id if i not in old_rows]
    kept_ids = [r[0] for r in old_rows.values() if r and r[0] in by_id]
    order = kept_ids + sorted(new_ids, key=lambda i: by_id[i].get("created_time", ""))

    for rid in order:
        page = by_id[rid]
        expand_truncated_props(api, page, title, report)
        vals = [rid] + [cell((page.get("properties") or {}).get(c), users) for c in cols]
        old = old_rows.get(rid)
        le = page.get("last_edited_time", "")
        prev_le = rstate.get(rid)
        line_changed = (old is None) or (old != vals)
        # NB: prev_le None (state seeding / first sight) deliberately does NOT
        # probe — automation-churned feeds would otherwise trigger thousands of
        # pointless body fetches. From the next run on, le drift drives probes.
        le_changed = prev_le is not None and prev_le != le
        if old is None and is_existing_csv:
            added.append(rid)
        elif line_changed or le_changed:
            changed.append(rid)
        new_rstate[rid] = le
        out_rows.append(vals)
        # a dry run still probes (upsert_row_md writes nothing under it), so its
        # request count is the real run's
        if old is None or line_changed or le_changed:
            need_probe = (old is None) or le_changed
            if need_probe and probe_mode == "enriched":
                old_md = md_idx.get(rid)
                enr = existing_enrichment(os.path.join(dirpath, old_md)) if old_md else None
                # the one edit the policy must not skip: a body written into a row
                # whose file holds none, which the receiver logs as content_updated
                if not (enr and enr.strip()) and content_edits().get(rid, "") <= (prev_le or ""):
                    probes_skipped += 1
                    need_probe = False
            upsert_row_md(api, users, page, db_id, title, cols, dirpath, need_probe, state, report, args,
                          md_idx=md_idx, discovered=discovered)

    deleted = [rid for rid in old_rows if rid not in by_id]

    csv_changed = False
    if not args.dry_run:
        new_csv = os.path.join(dirpath, f"{sanitize(title)} {db_id}.csv")
        target = csv_path or new_csv
        old_blob = open(target, "rb").read() if os.path.exists(target) else b""
        buf = io.StringIO()
        wtr = csv.writer(buf)
        wtr.writerow(["_row_id"] + cols)
        for vals in out_rows:
            wtr.writerow(vals)
        blob = buf.getvalue().encode()
        if blob != old_blob:
            with open(target, "wb") as f:
                f.write(blob)
            csv_changed = True
        for rid in deleted:
            if rid in md_idx:
                try:
                    os.remove(os.path.join(dirpath, md_idx[rid]))
                except OSError:
                    pass
        state["rows"][db_id] = new_rstate

    if probes_skipped:
        report["notes"].append(f"{title}: {probes_skipped} body/comment probes skipped (enriched-only policy; rows have no stored body/comments)")
    if added or changed or deleted:
        log(f"db '{title[:40]}': +{len(added)} ~{len(changed)} -{len(deleted)} (req={api.n})")
    if added or changed or deleted or csv_changed:
        titles = {rid: (row_title(by_id[rid], users) or "untitled") for rid in (added + changed)[:40]}
        report["dbs"]["changed"].append({
            "title": title, "id": db_id, "rows_total": len(rows),
            "added": [{"id": i, "title": titles.get(i, "")} for i in added[:25]],
            "added_n": len(added),
            "changed": [{"id": i, "title": titles.get(i, "")} for i in changed[:25]],
            "changed_n": len(changed),
            "deleted": deleted[:25], "deleted_n": len(deleted),
        })


def upsert_row_md(api, users, page, db_id, db_title, cols, dirpath, probe, state, report, args,
                  md_idx=None, discovered=None, pool=None):
    """Re-render one row file. `probe`: False carries the enrichment over
    verbatim; True walks the body and carries the comments (scanning them only
    on a row never enriched before); "scan" also reads the comments per block."""
    rid = undash(page["id"])
    idx = index_row_mds(dirpath) if md_idx is None else md_idx
    old_name = idx.get(rid)
    title = row_title(page, users)
    new_name = f"{sanitize(title)} {rid}.md"
    path = os.path.join(dirpath, new_name)
    enrichment = None
    stale = None      # why this row's enrichment is not a fresh read
    first_missed = None   # date of the annotation already on disk, if any
    fully_read = False
    if probe:
        stored = (existing_enrichment(os.path.join(dirpath, old_name)) or "") if old_name else ""
        old_cb = stored_comments_body(stored)
        # read before probing: the date a still-failing row was first missed
        # survives only on disk
        first_missed = probe_annotation(stored)
        # A row the mirror has never enriched is scanned: whatever comments it
        # already carries predate any capture and would otherwise never be seen.
        # Once it has been, a body-triggered probe carries its comments over.
        mode = "scan" if probe == "scan" or not has_enrichment(stored) else "carry"
        scans = state.setdefault("comment_scans", {})
        started = now_iso()  # a comment made mid-scan is newer than the scan
        try:
            enrichment = probe_row(api, users, page["id"], dirpath, report,
                                   old_comments_body=old_cb, discovered=discovered,
                                   comments=mode, last_scan=scans.get(rid, ""), pool=pool)
            report.setdefault("comments", {}).setdefault("row_probes", {"carry": 0, "scan": 0})[mode] += 1
            if mode == "scan":
                scans[rid] = started
            fully_read = True
        except ApiError as e:
            report["dbs"]["errors"].append({"db": db_title, "op": f"probe {rid[:8]}", "error": str(e)[:160]})
            enrichment = None
            stale = f"probe failed: {e}"[:120]
    if enrichment is None:
        enrichment = existing_enrichment(os.path.join(dirpath, old_name)) if old_name else ""
        if enrichment is None:
            enrichment = ""
    if stale:
        # The date is when the row was FIRST missed, not when it was last checked
        # — the same ≤date semantics the retention stamp carries. Re-stamping
        # today's churned the tree: a row that fails on every probe would be
        # rewritten nightly with the date as its entire diff. Kept regardless of
        # WHY the row is stale, since the annotation records no reason to compare
        # against. That reads staler than it is, the safe direction, and the
        # run's own reason is in the report.
        enrichment = annotate_probe_failure(
            enrichment, first_missed or dt.datetime.now(UTC).strftime("%Y-%m-%d"))
        if probe_annotation(enrichment):
            report["dbs"].setdefault("probe_annotated", []).append(
                {"row": rid, "db": db_title, "why": stale})
    elif fully_read:
        enrichment = strip_probe_annotation(enrichment)
    # neither: probe=False covers the enriched-only policy skip, a property-only
    # change and first sight during state seeding alike, so it can't tell an
    # existing annotation to go
    txt = render_row_md(page, db_id, db_title, cols, users, enrichment)
    if args.dry_run:
        return
    if old_name and old_name != new_name:
        try:
            os.remove(os.path.join(dirpath, old_name))
        except OSError:
            pass
    old_txt = open(path).read() if os.path.exists(path) else None
    if old_txt != txt:
        with open(path, "w") as f:
            f.write(txt)
    idx[rid] = new_name
    if has_comments(txt):
        state["comment_rows"].setdefault(rid, "")  # newly-commented rows join the audit pool


def remove_all_schemas_section(db_id):
    path = os.path.join(DBS, "_ALL-SCHEMAS.md")
    if not os.path.exists(path):
        return
    txt = open(path).read()
    pat = re.compile(r"^## .*`" + db_id + r"`.*?(?=^## |\Z)", re.M | re.S)
    new = pat.sub("", txt, count=1)
    if new != txt:
        with open(path, "w") as f:
            f.write(new)


def capture_new_db(api, users, db_id, state, report, args):
    """First-time capture of a newly discovered database."""
    try:
        d, sources = get_database(api, db_id)
    except ApiError as e:
        # A 4xx is a verdict about the id — not a database, deleted, or not
        # shared — so record it and stop asking. Anything else is a verdict
        # about the moment: this flag is permanent and `phase_discovery` skips
        # on it, so flagging a 5xx retires a live database from the mirror for
        # good, on one bad night. Unflagged, the id stays in pending-discovery
        # and the next run retries it. It must not propagate either:
        # nothing above catches ApiError, so an escaping one would unwind past
        # main's try and lose the night's uncommitted work.
        if e.code in (400, 403, 404):
            state["not_a_db"][db_id] = now_iso()
        else:
            report["dbs"]["errors"].append(
                {"db": db_id, "op": "capture_new_db", "error": str(e)[:160]})
        return
    d.pop("request_id", None)
    title = plain(d.get("title")) or "Untitled"
    dirname = f"{sanitize(title)} {db_id}"
    dirpath = os.path.join(DBS, dirname)
    try:
        rows, _ = query_db_rows(api, db_id)
    except ApiError as e:
        if e.code == 404:
            # The database answers but its rows do not: its data source is not
            # shared with the integration (a linked database somewhere else, or
            # sharing withdrawn). Capturing it anyway made an empty directory
            # that the row sweep deleted again after two 404s, every night; the
            # `unshared` bucket (shared with coverage_backfill) stops the asking.
            state["unshared"][db_id] = now_iso()
            state["db404"].pop(db_id, None)
            report["notes"].append(f"database '{title[:40]}' ({db_id[:8]}) is visible but its rows "
                                   "are not shared with the integration — not captured")
            return
        rows = []
        report["dbs"]["errors"].append({"db": title, "op": "new-capture", "error": str(e)[:200]})
    if args.dry_run:
        report["dbs"]["new"].append({"title": title, "id": db_id, "rows": len(rows), "dry_run": True})
        return
    os.makedirs(dirpath, exist_ok=True)
    jsave(os.path.join(dirpath, "_schema.json"),
          {"id": db_id, "title": title, "database": d,
           "data_sources": data_source_stubs(None, fresh=sources)})
    props = d.get("properties") or {}
    with open(os.path.join(dirpath, "_schema.md"), "w") as f:
        f.write(schema_md(title, db_id, len(rows), props, sources))
    update_all_schemas(title, db_id, len(rows), props, sources)
    refresh_db(api, users, db_id, dirname, state, report, args, set())
    report["dbs"]["new"].append({"title": title, "id": db_id, "rows": len(rows)})


# ---------------------------------------------------------------- content pages

def load_meta_jsonl():
    path = os.path.join(META, "pages-metadata.jsonl")
    out = {}
    order = []
    if os.path.exists(path):
        for ln in open(path):
            try:
                m = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if m.get("id") not in out:
                order.append(m["id"])
            out[m["id"]] = m
    return out, order


META_KEYS = ["id", "title", "parent_type", "parent_id", "created_time", "last_edited_time",
             "created_by", "last_edited_by", "archived", "in_trash", "url", "public_url",
             "icon", "has_cover"]


def meta_of(r, users):
    par = r.get("parent", {}) or {}
    pt = par.get("type", "")
    if pt == "data_source_id":
        # a row: the mirror keys rows by database, which the parent still names
        pt, par = "database_id", {"database_id": par.get("database_id")}
    ic = r.get("icon")
    icon = (ic.get("emoji") if ic.get("type") == "emoji" else ic.get("type", "")) if ic else ""
    return {"id": undash(r["id"]), "title": page_title_of(r), "parent_type": pt,
            "parent_id": undash(par.get(pt)) if isinstance(par.get(pt), str) else "",
            "created_time": r.get("created_time"), "last_edited_time": r.get("last_edited_time"),
            "created_by": users.name(r.get("created_by")), "last_edited_by": users.name(r.get("last_edited_by")),
            # `archived` is gone from 2026-03-11 responses; it always meant trashed
            "archived": r.get("archived", r.get("in_trash", False)),
            "in_trash": r.get("in_trash", False),
            "url": r.get("url", ""), "public_url": r.get("public_url"),
            "icon": icon, "has_cover": bool(r.get("cover"))}


def page_title_of(r):
    for pv in (r.get("properties") or {}).values():
        if pv.get("type") == "title":
            return plain(pv.get("title"))
    return ""


def save_meta_jsonl(meta, order):
    path = os.path.join(META, "pages-metadata.jsonl")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for i in order:
            f.write(json.dumps(meta[i], ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    # content-pages.tsv (content pages only, same enrichment as the build)
    cpath = os.path.join(META, "content-pages.tsv")
    with open(cpath + ".tmp", "w") as f:
        f.write("id\tparent_type\tcreated\tlast_edited\tcreated_by\tlast_edited_by\tarchived\turl\ttitle\n")
        for i in order:
            m = meta[i]
            if m.get("parent_type") in ("page_id", "block_id", "workspace"):
                f.write(f"{m['id']}\t{m['parent_type']}\t{m['created_time']}\t{m['last_edited_time']}\t"
                        f"{m['created_by']}\t{m['last_edited_by']}\t{m['archived']}\t{m['url']}\t{m['title']}\n")
    os.replace(cpath + ".tmp", cpath)


def index_workspace_pages():
    """id32 -> path for content-page .md files (excluding _databases)."""
    out = {}
    for root, dirs, files in os.walk(WS):
        if os.path.abspath(root) == os.path.abspath(DBS):
            dirs[:] = []
            continue
        for f in files:
            if f.endswith(".md"):
                m = ID32.search(f)
                if m:
                    out.setdefault(m.group(1), os.path.join(root, f))
    return out


def row_md_global_index():
    """rowid32 -> (db dirname, row filename) across every _databases dir."""
    out = {}
    for d in os.listdir(DBS):
        dd = os.path.join(DBS, d)
        if not os.path.isdir(dd):
            continue
        for f in os.listdir(dd):
            if f.endswith(".md") and not f.startswith("_schema"):
                m = ID32.search(f)
                if m:
                    out[m.group(1)] = (d, f)
    return out


def resolve_dest_dir(m, meta, page_index, row_index, api, users, depth=0):
    """Directory where page m belongs: its parent page's folder, its parent DB
    row's folder, or the nearest locatable ancestor's folder (flattening gaps
    where intermediate parents aren't mirrored/visible). None if unresolvable.
    Costs 0-2 API requests per unresolved hop (block/page lookups), bounded."""
    if depth > 5 or not m:
        return None
    pt, par = m.get("parent_type"), m.get("parent_id", "")
    if pt == "workspace":
        return WS
    if pt == "database_id":
        return None  # rows are the DB sweep's business, not page placement
    if not par:
        return None
    if pt == "block_id":
        try:
            b = api.get(f"/blocks/{dashed(par)}")
        except ApiError:
            return None
        bp = b.get("parent", {}) or {}
        bpt = bp.get("type", "")
        ref = undash(bp.get(bpt)) if isinstance(bp.get(bpt), str) else ""
        if bpt == "page_id":
            return _dir_of_page(ref, meta, page_index, row_index, api, users, depth + 1)
        return resolve_dest_dir({"parent_type": bpt, "parent_id": ref},
                                meta, page_index, row_index, api, users, depth + 1)
    if pt == "page_id":
        return _dir_of_page(par, meta, page_index, row_index, api, users, depth + 1)
    return None


def _dir_of_page(pid, meta, page_index, row_index, api, users, depth):
    """The folder that belongs to page/row pid (created beside its .md), else
    a climb to ITS parent."""
    path = page_index.get(pid)
    if path:
        d = path[:-3]
        if not DRY_RUN:
            os.makedirs(d, exist_ok=True)
        return d
    hit = row_index.get(pid)
    if hit:
        dirname, fname = hit
        d = os.path.join(DBS, dirname, fname[:-3])
        if not DRY_RUN:
            os.makedirs(d, exist_ok=True)
        return d
    pm = meta.get(pid)
    if pm is None:
        try:
            pm = meta_of(api.get(f"/pages/{dashed(pid)}"), users)
        except ApiError:
            return None
    return resolve_dest_dir(pm, meta, page_index, row_index, api, users, depth)


def place_unplaced_pass(api, users, meta, page_index, row_index, state, report, args):
    """Retry placement for everything in workspace/_unplaced — parents appear
    over time (metadata convergence, rows mirrored, blocks resolvable)."""
    unp = os.path.join(WS, "_unplaced")
    if not os.path.isdir(unp):
        return
    start = api.n
    placed = 0
    stuck = 0
    for f in sorted(os.listdir(unp)):
        if not f.endswith(".md"):
            continue
        mm = ID32.search(f)
        if not mm:
            continue
        pid = mm.group(1)
        m = meta.get(pid)
        if m is None:
            try:
                m = meta_of(api.get(f"/pages/{dashed(pid)}"), users)
                meta[pid] = m
            except ApiError:
                stuck += 1
                continue
        dest = resolve_dest_dir(m, meta, page_index, row_index, api, users)
        if not dest or os.path.abspath(dest) == os.path.abspath(unp):
            stuck += 1
            continue
        src = os.path.join(unp, f)
        dst = os.path.join(dest, f)
        if args.dry_run:
            continue
        if os.path.exists(dst):
            os.remove(src)
        else:
            os.replace(src, dst)
        if os.path.isdir(src[:-3]) and not os.path.exists(dst[:-3]):
            os.replace(src[:-3], dst[:-3])
        page_index[pid] = dst
        placed += 1
        report["pages"]["placed"].append({"id": pid, "to": os.path.relpath(dst, WS)})
    if placed or stuck:
        log(f"placement pass: {placed} placed, {stuck} still unresolvable ({api.n - start} req)")
    if placed and not args.dry_run:
        try:
            if not any(fn.endswith(".md") for fn in os.listdir(unp)):
                os.rmdir(unp)
        except OSError:
            pass


def walk_content_page(api, users, page_meta, page_index, report, args, meta=None, row_index=None):
    """Re-render one content page .md (body + comments + attachments)."""
    pid = page_meta["id"]
    title = page_meta.get("title") or "untitled"
    # No request cap: what bounds a walk is what the mirror agrees to render
    # (6,000 blocks), and the pages that used to need a cap — the automation
    # subtrees — are never re-walked at all (phase_content's scope rule).
    w = Walker(api, users, max_blocks=6000)
    lines = [f"# {title}", "",
             f"<!-- notion page id: {pid} | parent: "
             f"{json.dumps({'type': page_meta.get('parent_type'), 'id': page_meta.get('parent_id')})} -->", ""]
    w.walk(dashed(pid), lines, 0)

    old_path = page_index.get(pid)
    if old_path:
        dest_dir = os.path.dirname(old_path)
    else:
        dest_dir = resolve_dest_dir(page_meta, meta or {}, page_index,
                                    row_index if row_index is not None else {}, api, users)
        if not dest_dir:
            dest_dir = os.path.join(WS, "_unplaced")
            if not args.dry_run:
                os.makedirs(dest_dir, exist_ok=True)
            report["notes"].append(f"new page {pid[:8]} '{title[:40]}' has no locatable parent -> workspace/_unplaced/ (retried each run)")
    new_name = f"{sanitize(title)} {pid}.md"
    new_path = os.path.join(dest_dir, new_name)

    if w.attachments:
        folder = new_path[:-3]
        lines = download_attachments(w, lines, folder,
                                     prefix_for=lambda bid, i: f"{bid[:8]}_", report=report)
    if w.truncated or w.partial:
        # per-file completeness stamp so a reader of THIS file knows it's partial
        lines.insert(3, "<!-- content_complete: false | some child blocks were "
                        "truncated or inaccessible; retried on later runs -->")
    txt = "\n".join(lines).rstrip() + "\n"
    if args.dry_run:
        return new_path, w
    if old_path and os.path.abspath(old_path) != os.path.abspath(new_path):
        os.replace(old_path, new_path)
        oldfold, newfold = old_path[:-3], new_path[:-3]
        if os.path.isdir(oldfold) and not os.path.exists(newfold):
            os.replace(oldfold, newfold)
        report["pages"]["renamed"].append({"from": os.path.relpath(old_path, WS), "to": os.path.relpath(new_path, WS)})
    with open(new_path, "w") as f:
        f.write(txt)
    page_index[pid] = new_path
    return new_path, w


# ------------------------------------------------------- _comments.md handling

APPENDIX_MARK = "## Truncation backfill"


def load_comments_md():
    """-> (head, sections, appendix). The 2026-07-10 truncation-backfill block is
    kept as an immutable appendix until a full sweep rebuilds the file."""
    path = os.path.join(WS, "_comments.md")
    if not os.path.exists(path):
        return "", [], ""
    txt = open(path).read()
    # strip our stats trailer first (always the very last block once present)
    tm = re.search(r"\n---\n_[^\n]*_\n?\Z", txt)
    if tm:
        txt = txt[:tm.start()]
    appendix = ""
    ai = txt.find(APPENDIX_MARK)
    if ai != -1:
        # include the preceding '---' separator block in the appendix
        sep = txt.rfind("\n---\n", 0, ai)
        cut = sep if sep != -1 and not txt[sep + 5:ai].strip() else ai
        appendix = txt[cut:]
        txt = txt[:cut]
    m = re.search(r"^## ", txt, re.M)
    head = txt[:m.start()] if m else txt
    body = txt[m.start():] if m else ""
    sections = []
    for sm in re.finditer(r"^## (.*?)  `([0-9a-f]{32})`\n(.*?)(?=^## |\Z)", body, re.M | re.S):
        sections.append({"title": sm.group(1), "id": sm.group(2), "body": sm.group(3)})
    return head, sections, appendix


def comment_bullets(page_comments, block_comments):
    out = []
    for c in page_comments:
        out.append(stamp_cid(f'- **on** "(page-level)" — {c.who} ({c.when[:10]}): {c.text}',
                             c.cid, c.did))
    for anchor, cs in block_comments:
        for c in cs:
            out.append(stamp_cid(f'- **on** "{anchor}" — {c.who} ({c.when[:10]}): {c.text}',
                                 c.cid, c.did))
    return dedup_by_cid(out)


def dedup_by_cid(bullets):
    """Drop repeats of the same comment id, keeping the first.

    Only id-bearing bullets: bullets without one may be genuinely repeated
    events whose count is the information (one `AS_Integration` row carries the
    same `🔗 CLICK TRACKED` line seven times), and collapsing those was the
    original bug."""
    out, seen = [], set()
    for b in bullets:
        cid = bullet_cid(b)
        if cid is not None:
            if cid in seen:
                continue
            seen.add(cid)
        out.append(b)
    return out


_PAGE_BULLET = re.compile(r'- \*\*on\*\* "(.*?)" — (.*?) \((\d{4}-\d\d-\d\d)\): ?(.*)', re.S)
_ROW_BULLET = re.compile(r"- _(.*?) \((\d{4}-\d\d-\d\d)\):_ ?(.*)", re.S)
_WILD = "\x00"
# a markdown link, a URL, an @-mention's first word and the `‣` an older
# renderer wrote for any mention all stand for "a mention was here"
_TOKEN = re.compile(r"\[[^\]]*\]\([^)]*\)|https?://\S+|@\S+|‣|[^\W_]+")


def _bullet_parts(b):
    """(anchor or None for a row bullet, author, day, text) of a bullet, marks off."""
    t = bullet_text_key(b)
    m = _PAGE_BULLET.match(t)
    if m:
        return m.group(1), m.group(2), m.group(3), m.group(4)
    m = _ROW_BULLET.match(t)
    return (None, m.group(1), m.group(2), m.group(3)) if m else None


def _words(text):
    """Lower-case words, with each mention-like token (and the run of them) as one
    wildcard: the renderer that wrote a legacy bullet and today's disagree on
    exactly those."""
    out = []
    for tok in _TOKEN.findall(text):
        if tok[0] in "[@‣" or tok.startswith("http"):
            if not (out and out[-1][0] == _WILD):
                out.append((_WILD, 0))
            out[-1] = (_WILD, out[-1][1] + 1)
        else:
            out.append((tok.lower(), 0))
    return out


def _covers(pattern, words):
    """Does `pattern` read as `words`, each wildcard standing for up to four words
    per mention it replaced (a user's name, a date, a page title)?"""
    rx = "".join(r"(?:\S+ ){0,%d}" % (4 * n) if w == _WILD else re.escape(w) + " "
                 for w, n in pattern)
    return re.fullmatch(rx, "".join(w + " " for w, _n in words)) is not None


def same_comment(legacy, ided):
    """Is the id-less bullet `legacy` the comment the id-bearing `ided` renders?

    Same author and day, and the same words once mentions and link targets are
    set aside, in either direction. The anchor is not compared: an older
    renderer cut it elsewhere or wrote a table row as "(table row)". A text of
    one or two words ("done", "sent ✅") is said more than once a day, so it
    pairs only on the same anchor, and in a row, which records none, never."""
    a, b = _bullet_parts(legacy), _bullet_parts(ided)
    if not a or not b or a[1:3] != b[1:3]:
        return False
    wa, wb = _words(a[3]), _words(b[3])
    literal = sum(1 for w, _n in wa if w != _WILD)
    if literal == 0:
        return False
    if literal <= 2 and (a[0] is None or b[0] is None or a[0].strip().lower() != b[0].strip().lower()):
        return False
    return _covers(wa, wb) or _covers(wb, wa)


def legacy_pairs(bullets):
    """{index of an id-less bullet: index of the id-bearing bullet it is}, one to
    one, first come first served."""
    ided = [j for j, b in enumerate(bullets) if bullet_cid(b)]
    claimed, out = set(), {}
    for i, b in enumerate(bullets):
        if bullet_cid(b):
            continue
        for j in ided:
            if j not in claimed and same_comment(b, bullets[j]):
                out[i] = j
                claimed.add(j)
                break
    return out


def reconcile_bullets(bullets):
    """One bullet per comment: repeats of an id dropped (the first kept), and an
    id-less bullet dropped when the id-bearing bullet of the same comment is
    there. Every writer of a comment list goes through here."""
    out = dedup_by_cid(bullets)
    drop = legacy_pairs(out)
    return [b for i, b in enumerate(out) if i not in drop]


def upgrade_legacy(bullets, b, keep_mark=False):
    """Put the id-bearing bullet `b` in place of the id-less bullet in `bullets`
    that is the same comment (or has its exact text). With `keep_mark`, a
    resolved annotation on the replaced bullet carries over. -> its index or None."""
    key = bullet_text_key(b)
    for i, old in enumerate(bullets):
        if bullet_cid(old) or not (bullet_text_key(old) == key or same_comment(old, b)):
            continue
        m = RESOLVED_MARK.search(old) if keep_mark else None
        bullets[i] = annotate_resolved(b, re.search(r"\d{4}-\d\d-\d\d", m.group(0)).group(0)) \
            if m and not RESOLVED_MARK.search(b) else b
        return i
    return None


def to_row_bullet(b):
    """A `_comments.md` bullet in a row file's form: the anchor goes (rows record
    none), continuation lines are indented, and the resolved mark and the id
    trailer go on the first line. An anchor can run over several lines, so the
    trailer is not always on the text's first line; it is read off the whole
    bullet."""
    m = re.match(r'- \*\*on\*\* "(.*?)" — (.*?) \((.*?)\): ?(.*)', bullet_text_key(b), re.S)
    if not m:
        return b
    lines = m.group(4).split("\n")
    out = f"- _{m.group(2)} ({m.group(3)}):_ {lines[0]}" + "".join(
        "\n" + (ln if ln.startswith("  ") or not ln.strip() else "  " + ln) for ln in lines[1:])
    cid = CID_MARK.search(b)
    out = stamp_cid(out, *(("", "") if not cid or cid.group(1) == CID_LEGACY
                           else (cid.group(1), cid.group(2) or "")))
    gone = RESOLVED_MARK.search(b)
    return annotate_resolved(out, re.search(r"\d{4}-\d\d-\d\d", gone.group(0)).group(0)) if gone else out


def live_index(new_bullets):
    """(ids, texts) of a fresh scan — what a stored bullet is checked against."""
    ids = {c for c in map(bullet_cid, new_bullets) if c}
    return ids, {bullet_text_key(b) for b in new_bullets}


def still_live(old_bullet, ids, texts):
    """Is this stored bullet in the fresh scan?

    By id when it has one. A legacy shim (or a bullet from before the migration)
    falls back to comparing text against *every* fresh bullet, not just the
    other id-less ones — otherwise the first probe after the migration would
    annotate every un-migrated bullet as resolved and add the fresh copy beside
    it, which is the duplicate-manufacturing this change exists to stop."""
    cid = bullet_cid(old_bullet)
    return cid in ids if cid else bullet_text_key(old_bullet) in texts


def merge_comment_bullets(old_body, new_bullets, today, prefix="- **on**"):
    """Append-only comment semantics: bullets that vanish from the API (resolved
    or deleted threads — the API only ever returns unresolved comments) are kept
    and annotated instead of dropped. Returns (bullets, n_retained_new).

    Identity is the comment id (`still_live`), so an *edited* comment updates in
    place: the fresh bullet carries the same id, the stored one is recognised as
    live and dropped in its favour, and no false resolved annotation appears.

    A capture `union_captured` re-adds as resolved keeps the stored bullet when
    that is already annotated: the date says when the comment was first found
    gone, and re-stamping it with today's on every scan both loses that and
    rewrites every such bullet each night."""
    gone = {bullet_cid(b): b for b in split_bullets(old_body, prefix)
            if bullet_cid(b) and RESOLVED_MARK.search(b)}
    new_bullets = [gone.get(bullet_cid(n), n) if RESOLVED_MARK.search(n) else n
                   for n in new_bullets]
    ids, texts = live_index(new_bullets)
    olds = split_bullets(old_body, prefix)
    n = len(new_bullets)
    # an id-less stored bullet that is one of the fresh comments: the fresh copy
    # replaces it, rather than it being annotated and kept beside that copy
    upgraded = {i - n for i, j in legacy_pairs(list(new_bullets) + olds).items() if i >= n > j}
    retained = []
    newly = 0
    for k, b in enumerate(olds):
        if k in upgraded or still_live(b, ids, texts):
            continue  # still live (or reappeared): the fresh copy wins, unannotated
        if RESOLVED_MARK.search(b):
            retained.append(b)  # annotated on an earlier run, keep as-is
        else:
            retained.append(annotate_resolved(b, today))
            newly += 1
    return reconcile_bullets(list(new_bullets) + retained), newly


# ------------------------------------------------- webhook capture integration

_WEBHOOK_CAPTURES = None


def webhook_captures():
    """page_id32 -> [(anchor, comment dict)] from the receiver's capture log
    (see webhook_receiver.py). Loaded once per run; deduped by comment id.

    The *latest* capture of a comment wins, at the position of its first: the
    receiver re-captures a whole thread on every comment.* event, so a later
    record of an edited comment carries its current text. Each comment dict is
    a copy carrying its record's `captured_at` under `_captured_at`, which is
    what `fold_bullets` orders a capture against a scan by."""
    global _WEBHOOK_CAPTURES
    if _WEBHOOK_CAPTURES is not None:
        return _WEBHOOK_CAPTURES
    out = {}
    path = os.path.join(STATE, paths.CAPTURE)
    if os.path.exists(path):
        for ln in open(path):
            try:
                e = json.loads(ln)
            except json.JSONDecodeError:
                continue
            for c in e.get("comments", []):
                d = out.setdefault(e.get("page_id", ""), {})
                key = c.get("id")
                if key is None and None in d:
                    continue  # an id-less capture has no identity to update by
                d[key] = (e.get("anchor", "(page-level)"),
                          dict(c, _captured_at=e.get("captured_at", "")))
    _WEBHOOK_CAPTURES = {pid: list(d.values()) for pid, d in out.items()}
    return _WEBHOOK_CAPTURES


def captured_text(c):
    """A captured comment's text. Captures written from 2026-08-06 carry the raw
    rich_text items, which render like any other comment; older ones only ever
    stored the flattened string, so that is all there is to show."""
    return rich_md(c["rich_text"]) if c.get("rich_text") else c.get("text", "")


def union_captured(bullets, pid, users, row_format=False):
    """Append webhook-captured comments missing from a fresh scan, annotated —
    they were open at capture time; absence from the scan means resolved/deleted
    since. Presence means the scan already has them (skip). Idempotent across
    runs (the retention merge and this both key on the comment id)."""
    caps = webhook_captures().get(pid, [])
    if not caps:
        return bullets
    today = dt.datetime.now(UTC).strftime("%Y-%m-%d")
    ids, texts = live_index(bullets)
    out = list(bullets)
    for anchor, c in caps:
        b = captured_bullet(anchor, c, users, row_format)
        if still_live(b, ids, texts):
            continue
        out.append(annotate_resolved(b, today))
        # two captures of one comment (the log is deduped per page, the
        # pre-repair merge was not) must not become two bullets
        if bullet_cid(b):
            ids.add(bullet_cid(b))
        texts.add(bullet_text_key(b))
    return out


def captured_bullet(anchor, c, users, row_format=False):
    """A captured comment rendered as the bullet a scan of it would produce."""
    who = users.name({"id": c.get("author_id", "")}) if c.get("author_id") else "?"
    when = (c.get("created_time") or "")[:10]
    text = captured_text(c)
    if row_format:
        t = text.replace("\r", "").replace("\n", "\n  ")
        b = f"- _{who} ({when}):_ {t}"
    else:
        b = f'- **on** "{anchor}" — {who} ({when}): {text}'
    return stamp_cid(b, undash(c.get("id") or ""), undash(c.get("discussion_id") or ""))


# --------------------------------------------- folding captures without a scan
#
# The webhook capture is the mirror's primary comment channel: the receiver
# fetches a thread within about a minute of any comment.* event, so a new or
# edited comment is already on disk before the nightly starts. Folding those
# records into `_comments.md` and the row files costs no requests. What a fold
# cannot see is resolution — Notion fires no event when a thread is resolved,
# and a resolved comment simply stops being listed — so that is left to the
# rolling per-block audit, the one place a comment scan still happens.


def _parse_ts(s):
    """An ISO timestamp (either the API's `…Z` or the receiver's `…+00:00`) as an
    aware datetime, or None — `captured_at: "backfill"` included."""
    try:
        t = dt.datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def fold_bullets(stored, caps, last_scan, users, row_format, deletions, today):
    """Stored bullets + webhook captures -> (bullets, stats), no request made.

    `last_scan` is when the page's comments were last read from the API, and it
    decides how much a capture may say. A capture taken *after* it is newer
    than anything stored: its comment is added open, or updates an open stored
    bullet with the same id in place (an edit). A capture taken before it is
    older than the scan, which listed every comment still open at the time — so
    it may only add a comment the scan did not have, and then as resolved by
    the scan's date. That keeps a fold from reverting a scan, and makes it
    idempotent: folding the same log twice changes nothing.

    With no scan on record a capture may still add, since a comment missing
    from the file never reached it, but it never rewrites a stored bullet: that
    bullet may come from a later read than the capture. Nor does a capture
    without raw `rich_text` (written before 2026-08-06): its flattened text is
    a lossier rendering of the same comment, not a newer one.

    `deletions` (comment id -> date, from comment.deleted events) annotates a
    stored bullet as gone; deletion is final, so no ordering is needed."""
    out = list(stored)
    pos = {c: i for i, c in enumerate(map(bullet_cid, out)) if c}
    # a stored bullet with no id (a `legacy` shim, or pre-migration) is matched by
    # text, as `still_live` does: the capture is the same comment, and adding it
    # beside the shim is the duplicate the id migration exists to prevent
    legacy = {bullet_text_key(b) for b in out if not bullet_cid(b)}
    scan_t = _parse_ts(last_scan)
    gone_by = (last_scan or "")[:10] or today
    added = updated = deleted = 0
    for anchor, c in caps:
        b = captured_bullet(anchor, c, users, row_format)
        cid = bullet_cid(b)
        cap_t = _parse_ts(c.get("_captured_at"))
        fresh = cap_t is not None and (scan_t is None or cap_t > scan_t)
        if cid in pos:
            i = pos[cid]
            if fresh and scan_t is not None and c.get("rich_text") \
                    and not RESOLVED_MARK.search(out[i]) \
                    and bullet_text_key(out[i]) != bullet_text_key(b):
                out[i] = b
                updated += 1
            continue
        if cid:
            # the same comment on disk under no id takes the id where it stands:
            # open when the capture is newer than the scan, else as the scan left it
            i = upgrade_legacy(out, b, keep_mark=not fresh)
            if i is not None:
                pos[cid] = i
                updated += 1
                continue
        elif bullet_text_key(b) in legacy or still_live(b, *live_index(out)):
            continue  # already on disk under no id, matched by text
        out.append(b if fresh else annotate_resolved(b, gone_by))
        added += 1
        if cid:
            pos[cid] = len(out) - 1
    for i, b in enumerate(out):
        day = deletions.get(bullet_cid(b))
        if day and not RESOLVED_MARK.search(b):
            out[i] = annotate_resolved(b, day)
            deleted += 1
    return out, {"added": added, "updated": updated, "deleted": deleted}


_CONTENT_EDITS = None


def content_edits():
    """page id32 -> timestamp of its latest `page.content_updated` event, over every
    retained segment of the receiver's event log. Loaded once per run."""
    global _CONTENT_EDITS
    if _CONTENT_EDITS is not None:
        return _CONTENT_EDITS
    base = os.path.join(STATE, "webhook-events.jsonl")
    out = {}
    for path in [f"{base}.{i}" for i in (3, 2, 1)] + [base]:
        if not os.path.exists(path):
            continue
        with open(path, errors="replace") as f:
            for ln in f:
                if '"page.content_updated"' not in ln:
                    continue
                try:
                    e = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                pid = undash(((e.get("entity") or {}).get("id")) or "")
                ts = e.get("timestamp") or ""
                if e.get("type") == "page.content_updated" and pid and ts > out.get(pid, ""):
                    out[pid] = ts
    _CONTENT_EDITS = out
    return out


def webhook_deletions():
    """comment id32 -> date of its comment.deleted event, over every retained
    segment of the receiver's event log. The payload names the comment as the
    event's entity, which is all a fold needs to mark it."""
    base = os.path.join(STATE, "webhook-events.jsonl")
    out = {}
    for path in [f"{base}.{i}" for i in (3, 2, 1)] + [base]:
        if not os.path.exists(path):
            continue
        with open(path, errors="replace") as f:
            for ln in f:
                if '"comment.deleted"' not in ln:
                    continue
                try:
                    e = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                cid = undash(((e.get("entity") or {}).get("id")) or "")
                if e.get("type") == "comment.deleted" and cid:
                    out[cid] = (e.get("timestamp") or e.get("received_at") or "")[:10]
    return out


def with_comments(enrichment, bullets):
    """A row's enrichment with its comments region replaced by `bullets` and
    everything before it — the body region, a probe annotation — kept verbatim.
    Byte-identical to what `probe_row` writes for the same body and bullets.

    A delimited file with no comments region is kept whole: the heading fallback
    in `strip_comments_section` would cut it at a `## Comments` heading that is
    part of the body."""
    t = enrichment or ""
    whole = COMMENTS_OPEN not in t and BODY_OPEN in t
    base = (t if whole else strip_comments_section(t)).rstrip("\n")
    if not bullets:
        return base + "\n" if base.strip() else ""
    tail = "\n".join(comments_section_lines(bullets))
    if not base.strip():
        return "\n\n" + tail.strip("\n") + "\n"
    return base + "\n" + tail + "\n"


def fold_captures(users, state, report, args):
    """Fold every captured comment into the mirror: `_comments.md` for content
    pages, the comments region for DB rows. No requests (bar a user-name lookup
    for an author not seen before). Pages the mirror has not captured yet are
    left for a later run, which folds them then.

    Each page's and row's last full comment scan comes from `comment-scan.json`,
    which records only complete per-block reads (a row's first entry is its
    first audit). A row with none yet takes every capture as current: whatever
    a capture holds that its file lacks never reached it — a capped probe
    carried the old record over without looking — and the audit decides within
    one cycle whether it is still open."""
    caps = webhook_captures()
    deletions = webhook_deletions()
    if not caps and not deletions:
        return
    today = dt.datetime.now(UTC).strftime("%Y-%m-%d")
    stats = {"captures": sum(len(v) for v in caps.values()), "pages": 0, "rows": 0,
             "added": 0, "updated": 0, "deleted": 0, "unplaced": 0, "held_elsewhere": 0}
    by_cid_page = collections.defaultdict(dict)  # page -> {cid: date} for deletions
    meta, _order = load_meta_jsonl()
    _head, sections, _app = load_comments_md()
    section_of = {s["id"]: s for s in sections}
    rows = row_md_global_index()
    pages = set(caps)
    # a deletion on a page with no capture left in the log still has to land
    cid_home = {}
    for pid, cs in caps.items():
        for _a, c in cs:
            cid_home[undash(c.get("id") or "")] = pid
    for s in sections:
        for b in split_bullets(s["body"]):
            if bullet_cid(b):
                cid_home.setdefault(bullet_cid(b), s["id"])
    for cid, day in deletions.items():
        pid = cid_home.get(cid)
        if pid:
            by_cid_page[pid][cid] = day
            pages.add(pid)

    # A comment has one home. A capture may name the wrong object: the resolved-comment
    # backfill filed a child page's comments under the parent page whose scan listed
    # them, and folding it would put back every copy the dedup pass removed.
    held = comment_homes()
    claim = {}  # a comment not yet on disk goes where its live capture says, if it has one
    for pid, cs in caps.items():
        for _a, c in cs:
            cid = undash(c.get("id") or "")
            if cid and cid not in held:
                live = c.get("_captured_at") not in ("", "backfill")
                if cid not in claim or (live and not claim[cid][1]):
                    claim[cid] = (pid, live)
    held.update({cid: v[0] for cid, v in claim.items()})
    page_updates = {}
    for pid in sorted(p for p in pages if p):
        pc, dels = caps.get(pid, []), by_cid_page.get(pid, {})
        keep = [(a, c) for a, c in pc if held.get(undash(c.get("id") or ""), pid) == pid]
        stats["held_elsewhere"] += len(pc) - len(keep)
        pc = keep
        if pid in rows:
            dirname, fname = rows[pid]
            path = os.path.join(DBS, dirname, fname)
            try:
                txt = open(path).read()
            except OSError:
                continue
            if MARKER not in txt:
                continue
            head, enr = txt.split(MARKER, 1)
            old = split_bullets(stored_comments_body(enr), prefix="- _")
            new, st = fold_bullets(old, pc, state["comment_scans"].get(pid, ""), users,
                                   True, dels, today)
            if new != old:
                stats["rows"] += 1
                for k in ("added", "updated", "deleted"):
                    stats[k] += st[k]
                if not args.dry_run:
                    with open(path, "w") as f:
                        f.write(head + MARKER + (with_comments(enr, new) or "\n"))
                    state["comment_rows"].setdefault(pid, "")
            continue
        m = meta.get(pid)
        sec = section_of.get(pid)
        if sec is None and not (m and m.get("parent_type") in ("page_id", "block_id", "workspace")):
            stats["unplaced"] += 1
            continue
        old = split_bullets(sec["body"]) if sec else []
        new, st = fold_bullets(old, pc, state["comment_scans"].get(pid, ""), users,
                               False, dels, today)
        if new != old:
            stats["pages"] += 1
            for k in ("added", "updated", "deleted"):
                stats[k] += st[k]
            page_updates[pid] = {"title": (m or {}).get("title") or (sec or {}).get("title")
                                 or "(untitled)", "bullets": new}
    if page_updates and not args.dry_run:
        update_comments_md(page_updates, report, merge=False)
    report["comments"]["folded"] = stats
    report["comments"]["added"] += stats["added"]
    report["comments"]["retained"] += stats["deleted"]
    log(f"webhook fold: {stats['added']} added, {stats['updated']} updated, "
        f"{stats['deleted']} deleted across {stats['pages']} page(s) and {stats['rows']} row(s) "
        f"({stats['captures']} captured comments, {stats['unplaced']} on pages not mirrored yet, "
        f"{stats['held_elsewhere']} already held by another page or row)")


# ------------------------------------------- comment-contamination assert (A4)
#
# On 2026-07-27 the resolved-comment backfill was found copying foreign threads
# onto pages that never carried them: `loadPageChunk` hydrates ancestor and
# database-level records alongside the requested page, and nothing filtered them
# out, so one discussion landed on up to 2,287 pages. The harvest is scoped now
# (`backfill_resolved_comments.owns_block`), but scoping only stops the bug we
# found. This is the detector: after a run's comment writes, no single comment
# text may be attributed to more pages than genuine repetition ever produces.
#
# CALIBRATION (measured over the live mirror, 2026-08-06 — 83,097 row files and
# 2,477 `_comments.md` sections, 39,368 bullets, 10,193 distinct texts):
#
#   spread     what it is
#   ------     ----------
#     ≤ 26     genuine repetition. The top case is judging-criteria boilerplate
#              on 26 rows — all of them in ONE database, which is the shape
#              honest repetition takes.
#   27 – 61    empty. Nothing in the corpus lands here.
#   62 – 2038  29 texts, every one contamination: carriers are unrelated pages
#              across unrelated hubs (two unrelated hub pages both carrying
#              "let's use this for our Notion clean-up"), and they are
#              backfill-sourced.
#
# The residual that table's third row describes was repaired on 2026-08-17: the
# 28 texts were un-merged from `_comments.md` down to the single page each one's
# comment actually hangs off (verified per comment against the live API), the
# baseline file was retired, and the corpus now tops out at 26. So the whole
# contamination population is gone and the empty band runs 27 upward.
#
# 30 is therefore back to being the bottom edge of that band, four pages above
# the benign maximum. It was briefly 45, the middle of the then-measured band,
# for a reason the repair does NOT retire: the benign top case IS judging
# boilerplate, events vary in size (one had 106 project rows), and the
# next one posting the same comment onto 31-45 project rows fires at 30 and not
# at 45. That false positive is expensive and asymmetric — a breach stops the
# commit and leaves the tree dirty, which then fails every later nightly on its
# own preflight until a human clears it — and a baseline cannot pre-accept it,
# because the offending text is a future, different one. The revert to 30 buys
# detection 15 pages earlier against a mechanism that has only ever produced
# 62-2,287, and pays for it with that false positive. If it ever fires on
# boilerplate, raise this constant back to 45 rather than baselining the text;
# the durable answer is keying on comment ids, which retires
# text as the key altogether.
MAX_COMMENT_PAGES = 30
CONTAMINATION_BASELINE = "comment-contamination-baseline.json"

# "- _Who (2026-01-02):_ text"  /  '- **on** "anchor" — Who (2026-01-02): text'
_COMMENT_PREFIX = re.compile(r"^- (?:_|\*\*on\*\*).*?\(\d{4}-\d{2}-\d{2}\):_? ?", re.S)
_NO_CONTENT = re.compile(r"[\W_]+", re.UNICODE)

Breach = collections.namedtuple("Breach", "text pages allowed")


def comment_text_key(bullet):
    """A comment bullet -> just its text, comparable across both mirror tiers.

    '' for a bullet whose text carries no content of its own: the mirror renders
    every mention as '‣', so distinct one-mention comments would otherwise
    collapse into one key and read as spread.
    """
    # the id trailer comes off too: it is per-comment, so leaving it in would
    # give every bullet a unique key and quietly retire the assert
    b = bullet_text_key(bullet)
    m = _COMMENT_PREFIX.match(b)
    # An unparseable bullet keys on its whole self rather than being dropped:
    # keeping the author prefix can only make a false positive less likely.
    key = " ".join((b[m.end():] if m else b).split())
    return "" if not _NO_CONTENT.sub("", key) else key


def comment_homes():
    """comment id -> the one object (page section or row file) that holds it."""
    out = {}
    for s in load_comments_md()[1]:
        for b in split_bullets(s["body"]):
            if bullet_cid(b):
                out.setdefault(bullet_cid(b), s["id"])
    for rid, (d, f) in row_md_global_index().items():
        try:
            txt = open(os.path.join(DBS, d, f)).read()
        except (OSError, UnicodeDecodeError):
            continue
        if MARKER not in txt or COMMENTS_OPEN not in txt and "\n## Comments" not in txt:
            continue
        for b in split_bullets(stored_comments_body(txt.split(MARKER, 1)[1]), prefix="- _"):
            if bullet_cid(b):
                out.setdefault(bullet_cid(b), rid)
    return out


def build_comment_index():
    """comment text -> {page ids carrying it}, over the whole mirror.

    Both tiers, because the incident spanned both: DB row `.md` files keyed by
    row id, and `_comments.md` sections keyed by page id. Repeats within one
    page count once — within-page duplication is a separate, pre-existing wart,
    not misattribution. The `_comments.md` truncation appendix is excluded, the
    same boundary `load_comments_md` draws for the writers.
    """
    idx = collections.defaultdict(set)
    for d in sorted(os.listdir(DBS)) if os.path.isdir(DBS) else []:
        dd = os.path.join(DBS, d)
        if not os.path.isdir(dd):
            continue
        for f in os.listdir(dd):
            if not f.endswith(".md") or f.startswith("_schema"):
                continue
            m = ID32.search(f)
            if not m:
                continue
            try:
                txt = open(os.path.join(dd, f)).read()
            except OSError:
                continue
            if MARKER not in txt:
                continue
            for b in split_bullets(extract_comments_body(txt.split(MARKER, 1)[1]), prefix="- _"):
                key = comment_text_key(b)
                if key:
                    idx[key].add(m.group(1))
    for s in load_comments_md()[1]:
        for b in split_bullets(s["body"]):
            key = comment_text_key(b)
            if key:
                idx[key].add(s["id"])
    return idx


def contamination_id(text):
    """Stable short key for the baseline file: comment texts run to hundreds of
    characters and carry newlines, so they are hashed rather than stored."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def load_contamination_baseline():
    """Breaches that already existed when someone last accepted the state, so a
    pre-existing mess does not fail every run until it is repaired. Absent file
    = strict: every breach fails. Written only by an explicit
    `--mode contamination-check --write-baseline`, never by a refresh run."""
    return jload(os.path.join(STATE, CONTAMINATION_BASELINE), {})


def save_contamination_baseline(index):
    jsave(os.path.join(STATE, CONTAMINATION_BASELINE),
          {contamination_id(t): {"pages": len(p), "text": t[:120]}
           for t, p in index.items() if len(p) > MAX_COMMENT_PAGES})


def contamination_breaches(index, baseline=None):
    """Texts spread wider than genuine repetition explains, worst first.

    A baselined text is a breach only if it spread *further* — the incident's
    signature is a page count that grows, so accepting a known mess still leaves
    it unable to ratchet upward one page at a time.
    """
    baseline = baseline or {}
    out = []
    for text, pages in index.items():
        n = len(pages)
        if n <= MAX_COMMENT_PAGES:
            continue
        allowed = (baseline.get(contamination_id(text)) or {}).get("pages")
        if allowed is None or n > allowed:
            out.append(Breach(text, n, allowed))
    return sorted(out, key=lambda b: (-b.pages, b.text))


def contamination_message(breaches, limit=10):
    L = [f"comment contamination: {len(breaches)} comment text(s) attributed to more than "
         f"{MAX_COMMENT_PAGES} pages — one text on many unrelated pages means a thread was "
         f"copied onto pages that never carried it (see the comment-contamination assert in "
         f"the engine's README.md)"]
    for b in breaches[:limit]:
        grew = f" (was {b.allowed} at baseline)" if b.allowed is not None else ""
        L.append(f"  {b.pages} pages{grew}: {b.text[:120]!r}")
    if len(breaches) > limit:
        L.append(f"  … +{len(breaches) - limit} more")
    return "\n".join(L)


def record_contamination_check(index, baseline, report):
    breaches = contamination_breaches(index, baseline)
    report["comments"]["contamination_breaches"] = len(breaches)
    if breaches:
        report["notes"].append(
            f"FAILED: comment contamination — {len(breaches)} text(s) over {MAX_COMMENT_PAGES} pages, "
            f"worst {breaches[0].pages}: {breaches[0].text[:80]!r}")
    return breaches


def contamination_cli(write_baseline=False):
    """`--mode contamination-check`: the same assert, standalone and offline.

    Needs no token and touches nothing, so it can be run against the mirror at
    any time — before enabling the inline check, or to confirm a repair."""
    t0 = time.time()
    index = build_comment_index()
    pages = len({p for ps in index.values() for p in ps})
    log(f"comment index: {len(index)} distinct texts over {pages} pages ({time.time() - t0:.1f}s)")
    if write_baseline:
        save_contamination_baseline(index)
        n = len(load_contamination_baseline())
        log(f"baseline written: {n} pre-existing breach(es) accepted at their current spread "
            f"({os.path.join(STATE, CONTAMINATION_BASELINE)})")
        return 0
    baseline = load_contamination_baseline()
    breaches = contamination_breaches(index, baseline)
    if not breaches:
        log(f"clean: no comment text is on more than {MAX_COMMENT_PAGES} pages"
            + (f", beyond the {len(baseline)} recorded in the baseline" if baseline else ""))
        return 0
    print(contamination_message(breaches, limit=30), file=sys.stderr)
    return 1


# ------------------------------------------------------------ coverage assert

# How many findings the one-line note names before it says "+N more". The full
# set always survives in report["coverage"]["findings"]; this caps the prose.
COVERAGE_SHOWN = 10


def coverage_findings(cen, exclusions, flags=None):
    """The census's absent set minus the individually reasoned exclusions.

    One entry per id the mirror links to and never captured. The backfill drained
    this to zero, so a non-empty result is a real new gap rather than a backlog
    — which is what filling the hole bought over baselining it.

    `flags` is the nightly's own `db-flags.json` state. An id it already
    refused (`capture_new_db` records `not_a_db` on a 400/403/404 and skips the
    id from then on, writing no exclusion) is still a finding — an untriaged
    decision is a gap — but it travels with that verdict attached, so the
    operator has a decision to make rather than an investigation to open.
    """
    flags = flags or {}
    out = []
    for e in cen["absent"]:
        if e["id"] in exclusions:
            continue
        out.append({
            "id": e["id"], "kind": e["kind"], "title": e["title"],
            "flagged": next((k for k in ("not_a_db", "db404")
                             if e["id"] in (flags.get(k) or {})), None),
            # Enough to locate it; the census file holds every occurrence.
            "occurrences": e["occurrences"][:3],
        })
    return out


def coverage_message(cov):
    """One report line. The zero case states the size of the set it just
    excused, because a bare "no gaps" is indistinguishable from "nothing was
    measured" — the failure mode this whole assert exists to rule out."""
    n = len(cov["findings"])
    if not cov["referenced"]:
        # Zero stand-ins anywhere in a ~92k-file corpus is not a clean mirror,
        # it is a scan that looked in the wrong place. Saying "no new gaps"
        # here would be the assert's own false-clean.
        return ("coverage assert INCONCLUSIVE: the scan found no stand-in references at all — "
                "the corpus root is missing or empty, not gap-free")
    if cov.get("floor"):
        # The sentinel above only catches a corpus that is wholly missing. Half a
        # corpus reads as clean, because references and their targets disappear
        # together: the gap shrinks rather than grows.
        return (f"coverage assert INCONCLUSIVE: {cov['referenced']} stand-in references, down from "
                f"{cov['floor']} last run — the corpus only grows in normal operation, so this is "
                f"a truncated checkout or a mid-sync tree, not a mirror that lost its gaps; if the "
                f"corpus really did shrink (a Notion cleanup), delete "
                f"{os.path.join(STATE, COVERAGE_FLOOR)} to re-baseline")
    if not n:
        return (f"coverage assert: no new gaps — {cov['absent']} referenced-but-absent id(s), "
                f"all named in _meta/coverage/exclusions.json ({cov['scan_s']}s scan)")
    lead = (f"coverage assert: {n} NEW referenced-but-absent id(s) — the mirror links to "
            f"them and never captured them")
    seen = sorted({f["flagged"] for f in cov["findings"] if f["flagged"]})
    if seen:
        k = sum(1 for f in cov["findings"] if f["flagged"])
        lead += (f"; {k} already carry the nightly's own {'/'.join(seen)} flag, so they want a "
                 f"triage decision (coverage_census.py --exclude ID --reason R --note …) "
                 f"rather than a fetch")
    if any(f["kind"] == "sub_page" for f in cov["findings"]):
        # No phase of the nightly closes one: probe_row harvests child_dbs into
        # `discovered` and never child_pages. Without saying so, a sub_page
        # finding is nightly noise with no next step.
        lead += ("; the sub_page ones need a `coverage_backfill.py` run — no phase of the "
                 "nightly captures them, so waiting will not clear them")
    shown = ", ".join(_finding_line(f) for f in cov["findings"][:COVERAGE_SHOWN])
    return lead + ": " + shown + (f" … +{n - COVERAGE_SHOWN} more" if n > COVERAGE_SHOWN else "")


def _finding_line(f):
    """One finding, in the terms acting on it takes: the full id `--exclude`
    wants, and the first place the mirror references it."""
    where = (f["occurrences"] or [{}])[0]
    return (f"{f['kind']} {f['id']} {f['title'][:30]!r}"
            + (f" at {where['file']}:{where['line']}" if where else ""))


# How far the referenced count may fall run-over-run before the assert refuses to
# call the result clean. 10% of slack absorbs ordinary attrition (rows do get
# deleted in Notion); a real cleanup that removes more than that says so, and
# clears by deleting the floor file, which the message names.
COVERAGE_FLOOR_TOLERANCE = 0.10
COVERAGE_FLOOR = "coverage-floor.json"


def record_coverage_check(report, root=None, exclusions_path=None, state=None,
                          floor_path=None, write_floor=True):
    """Re-measure the coverage gap and record it. Reports; never fails the run.

    Deliberately a fresh scan of the corpus rather than a read of the committed
    `_meta/coverage/census.json`: that file is the triage work list, frozen when
    it was generated, so an assert built on it would report how stale the file
    is instead of what the mirror is missing right now. The scan costs zero API
    requests and ~15s of local reading over the 92k-file corpus, and it writes
    no census — regenerating that tracked file nightly would churn it and
    overwrite the `disposition` triage the exclusion record is built on.

    `root` and `floor_path` travel together: a caller scanning a corpus other
    than WS must give the floor its own file, or one corpus's count becomes the
    other's floor. `write_floor` is off for a dry run, which reports without
    leaving anything on disk.
    """
    t0 = time.time()
    excl = coverage_census.load_exclusions(exclusions_path or coverage_census.DEFAULT_EXCLUSIONS)
    cen = coverage_census.census(root or WS)
    findings = coverage_findings(cen, excl, state)
    referenced = sum(t["referenced"] for t in cen["totals"].values())
    fpath = floor_path or os.path.join(STATE, COVERAGE_FLOOR)
    was = jload(fpath, {}).get("referenced") or 0
    shrank = was if referenced < was * (1 - COVERAGE_FLOOR_TOLERANCE) else 0
    report["coverage"] = {
        "absent": len(cen["absent"]),
        "excluded": len(cen["absent"]) - len(findings),
        "findings": findings,
        "referenced": referenced,
        # Last run's count, present only when this run came in under it. A run
        # that measured less than it should have must not lower the floor, or the
        # next equally-truncated run reads as clean.
        "floor": shrank,
        # The two limits travel with the number so the line cannot overclaim:
        # this measures referenced-but-absent over probed bodies, never
        # never-referenced. See README.md § About coverage.
        "blind_spots": cen["blind_spots"],
        "scan_s": round(time.time() - t0, 1),
    }
    if write_floor and referenced and not shrank:
        jsave(fpath, {"referenced": referenced, "ts": now_iso()})
    report["notes"].append(coverage_message(report["coverage"]))
    return findings


def run_coverage_assert(report, state=None, root=None, exclusions_path=None, write_floor=True):
    """The nightly's entry point: assert, or say why it could not."""
    try:
        return record_coverage_check(report, root=root, exclusions_path=exclusions_path,
                                     state=state, write_floor=write_floor)
    except Exception as e:  # noqa: BLE001 — deliberately total; see below
        # The assert reports, so it may not be the thing that fails the run. By
        # the time it runs the mirror is already fetched and the tree is already
        # written; letting a malformed exclusions.json or a filesystem error
        # propagate would abort refresh.sh before `git add` and throw away a
        # whole night's work over a file the nightly does not even write.
        report["notes"].append(f"coverage assert failed to run: {type(e).__name__}: {e}"[:300])
        return None


def update_comments_md(updates, report, merge=True):
    """updates: {pid: {"title":.., "bullets":[..]}} — replace/add sections.
    Append-only semantics: comments that vanish from the API are retained with a
    resolved/deleted annotation (the API cannot see resolved threads, so dropping
    them would erase history — they are kept by design).

    `merge=False` is for bullets that already are the whole section — a fold,
    which started from the stored section — so nothing is annotated here and
    the added/retained counts are the caller's to record.

    An id with a row file is a database row (a wiki page is a row of its wiki
    database too), and a row's comments live in its row file: its update goes
    there (`write_row_comments`), together with any section the file still held
    for it, which is dropped."""
    if not updates:
        return
    today = dt.datetime.now(UTC).strftime("%Y-%m-%d")
    head, sections, appendix = load_comments_md()
    by_id = {s["id"]: s for s in sections}
    rows = row_md_global_index()
    n_add = n_ret = 0
    for pid, u in updates.items():
        old = by_id.get(pid)
        if pid in rows:
            stored = split_bullets(old["body"]) if old else []
            n_ret += write_row_comments(rows[pid], [to_row_bullet(b) for b in u["bullets"]],
                                        [to_row_bullet(b) for b in stored], merge, today)
            if old:
                sections.remove(old)
                by_id.pop(pid)
            continue
        if merge:
            merged, newly_retained = merge_comment_bullets(old["body"] if old else "",
                                                           u["bullets"], today)
            n_ret += newly_retained
        else:
            merged = reconcile_bullets(u["bullets"])
        if not merged:
            if old:
                sections.remove(old)
                by_id.pop(pid)
            continue
        old_n = old["body"].count("- **on**") if old else 0
        n_add += max(0, len(merged) - old_n)
        body = "\n" + "\n".join(merged) + "\n\n"
        if old:
            old["title"], old["body"] = u["title"], body
        else:
            s = {"title": u["title"], "id": pid, "body": body}
            sections.append(s)
            by_id[pid] = s
    if merge:
        report["comments"]["added"] += n_add
        report["comments"]["retained"] += n_ret
    write_comments_md(head, sections, appendix)


def write_comments_md(head, sections, appendix):
    """`_comments.md` from its parts (`load_comments_md`), with the stats trailer."""
    if not head:
        head = "# Notion comments (content pages)\n\n_Inline reviewer comments captured per-block via `/v1/comments`. DB-row comments live in each row's `.md` under `workspace/_databases/`._\n\n"
    total = sum(s["body"].count("- **on**") for s in sections)
    note = " (plus the truncation-backfill appendix)" if appendix else ""
    tail = (f"\n---\n_{total} comments across {len(sections)} pages{note}. Resolved/deleted threads are"
            f" kept with a `[resolved/deleted ≤date]` annotation (the API only returns open comments)."
            f" Refreshed incrementally; see _meta/changelog/._\n")
    out = head + "".join(f"## {s['title']}  `{s['id']}`\n{s['body']}" for s in sections).rstrip("\n") \
        + "\n" + (appendix if appendix else "") + tail
    with open(os.path.join(WS, "_comments.md"), "w") as f:
        f.write(out)


def write_row_comments(where, bullets, extra_stored, merge, today, dry_run=False):
    """Page-tier comment bullets (already in row form) into a row file: merged
    like a probe's when `merge`, else taken as the whole list; `extra_stored` is
    what a `_comments.md` section still held for the row, stored comments too.
    A row file with no enrichment region gets one. -> newly annotated count."""
    dirname, fname = where
    path = os.path.join(DBS, dirname, fname)
    txt = open(path).read()
    if MARKER in txt:
        head, enr = txt.split(MARKER, 1)
    else:
        head, enr = txt.rstrip("\n") + "\n\n", ""
    stored = split_bullets(stored_comments_body(enr), prefix="- _") + list(extra_stored)
    newly = 0
    if merge:
        new, newly = merge_comment_bullets("\n".join(stored), bullets, today, prefix="- _")
    else:
        have = {bullet_cid(b) for b in bullets if bullet_cid(b)}
        new = list(bullets) + [b for b in stored if not bullet_cid(b) or bullet_cid(b) not in have]
    out = head + MARKER + (with_comments(enr, new) or "\n")
    if out != txt and not dry_run:
        with open(path, "w") as f:
            f.write(out)
    return newly


# ------------------------------------------------------------- structure.md

def _md_count(path):
    n = 0
    for _root, _dirs, files in os.walk(path):
        n += sum(1 for f in files if f.endswith(".md") and not f.startswith("_schema"))
    return n


def _display_name(name, seen):
    m = re.search(r" ([0-9a-f]{32})$", name)
    base = re.sub(r" [0-9a-f]{32}$", "", name) or "Untitled"
    if base in seen and m:
        base = f"{base} {m.group(1)[:4]}-{m.group(1)[-4:]}"
    seen.add(base)
    return base


def _subdirs(path, skip_special=False):
    try:
        return sorted((d for d in os.listdir(path)
                       if os.path.isdir(os.path.join(path, d))
                       and not (skip_special and d.startswith("_"))),
                      key=str.lower)
    except OSError:
        return []


def regenerate_structure_md(max_children=20):
    """Rebuild structure.md (tree overview with counts) from the on-disk mirror."""
    lines = [
        "# Workspace structure",
        "",
        "_The `workspace/` mirror reproduces the real Notion nesting (export teamspace / "
        "`Private & Shared` artifacts stripped). Folder ids trimmed here for readability. "
        "Counts = total `.md` (pages + DB-row bodies) beneath. Auto-regenerated by "
        f"the mirror engine's `refresh.py`; last: {dt.datetime.now(UTC).strftime('%Y-%m-%d')}._",
        "",
    ]
    top_seen = set()
    for top in _subdirs(WS, skip_special=True):
        tpath = os.path.join(WS, top)
        lines.append(f"- **{_display_name(top, top_seen)}/**  ({_md_count(tpath)} pages)")
        subs = _subdirs(tpath)
        seen2 = set()
        for i, sub in enumerate(subs):
            if i >= max_children:
                lines.append(f"    - … +{len(subs) - max_children} more")
                break
            spath = os.path.join(tpath, sub)
            lines.append(f"    - {_display_name(sub, seen2)}/  ({_md_count(spath)})")
            subs3 = _subdirs(spath)
            seen3 = set()
            for j, s3 in enumerate(subs3):
                if j >= max_children:
                    lines.append(f"        - … +{len(subs3) - max_children} more")
                    break
                lines.append(f"        - {_display_name(s3, seen3)}/  ({_md_count(os.path.join(spath, s3))})")
    if os.path.isdir(DBS):
        ndb = sum(1 for d in os.listdir(DBS) if os.path.isdir(os.path.join(DBS, d)))
        lines += ["", f"- **_databases/**  ({ndb} databases; schemas + full row CSVs + per-row `.md`)"]
    unp = os.path.join(WS, "_unplaced")
    if os.path.isdir(unp) and _md_count(unp):
        lines.append(f"- **_unplaced/**  ({_md_count(unp)} pages whose parents aren't locatable yet; "
                     "the refresh retries placement each run)")
    out = "\n".join(lines) + "\n"
    path = os.path.join(NOTION, "structure.md")
    old = open(path).read() if os.path.exists(path) else ""
    if out != old:
        with open(path, "w") as f:
            f.write(out)
        return True
    return False


# ---------------------------------------------------------------- report

def new_report(mode):
    return {"ts": now_iso(), "mode": mode, "requests": 0, "rate429": 0, "duration_s": 0,
            "dbs": {"checked": 0, "changed": [], "new": [], "deleted": [], "schema_changed": [],
                    "errors": [], "probe_annotated": []},
            "pages": {"changed": [], "new": [], "deleted": [], "renamed": [], "placed": [], "errors": []},
            "comments": {"pages_rescanned": 0, "added": 0, "retained": 0,
                         "contamination_breaches": 0},
            "attachments": {"downloaded": [], "failed": []},
            # Empty until the coverage assert runs, so "the assert did not run"
            # and "the assert found nothing" stay distinguishable in the report.
            "coverage": {},
            "phases": {},  # phase name -> API requests spent
            # endpoint class (notion_core.api.endpoint_class) -> requests, for the
            # whole run and per phase: where the requests went, not just how many
            "requests_by_endpoint": {},
            "phases_by_endpoint": {},
            "notes": []}


def endpoint_line(counts):
    """`comments 2882 · blocks/children 610 · …`, largest first."""
    return " · ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]) if v)


def report_md(r):
    L = [f"# Notion mirror refresh — {r['ts']} ({r['mode']})", "",
         f"API requests: {r['requests']} (429s: {r['rate429']}"
         + (", by reason: " + ", ".join(f"{k} {v}" for k, v in r["rate_limit_reasons"].items())
            if r.get("rate_limit_reasons") else "") + ")",
         f"Duration: {r['duration_s']}s"]
    if r.get("phases"):
        L += ["", "By phase: " + " · ".join(f"{k} {v}" for k, v in r["phases"].items() if v)]
    if r.get("requests_by_endpoint"):
        L += ["", "By endpoint: " + endpoint_line(r["requests_by_endpoint"])]
        for k, v in (r.get("phases_by_endpoint") or {}).items():
            if v:
                L.append(f"- {k}: {endpoint_line(v)}")
    L += [""]
    d = r["dbs"]
    L += [f"## Databases — {d['checked']} checked, {len(d['changed'])} with changes"]
    for c in d["changed"]:
        L.append(f"- **{c['title']}** (`{c['id'][:8]}`, {c['rows_total']} rows): "
                 f"+{c['added_n']} added, ~{c['changed_n']} changed, -{c['deleted_n']} deleted")
        for a in c["added"][:10]:
            L.append(f"    - new: {a['title'] or a['id']}")
        for a in c["changed"][:10]:
            L.append(f"    - changed: {a['title'] or a['id']}")
    for x in d["new"]:
        L.append(f"- NEW DATABASE captured: **{x['title']}** ({x.get('rows', '?')} rows)")
    for x in d["deleted"]:
        L.append(f"- DATABASE gone: **{x['title']}** — {x['note']}")
    if d["schema_changed"]:
        L.append(f"- schema changes: {', '.join(map(str, d['schema_changed']))}")
    for e in d["errors"]:
        L.append(f"- ERROR {e['db']} [{e['op']}]: {e['error']}")
    ann = d.get("probe_annotated") or []
    if ann:
        L.append(f"- {len(ann)} row(s) marked stale (probe incomplete, stored body/comments kept):")
        for a in ann[:10]:
            L.append(f"    - {a['db']} `{a['row'][:8]}` — {a['why']}")
        if len(ann) > 10:
            L.append(f"    - … +{len(ann) - 10} more")
    p = r["pages"]
    skipped = p.get("automation_skipped", 0)
    L += ["", f"## Content pages — {len(p['changed'])} re-walked, {len(p['new'])} new, {len(p['deleted'])} deleted"
          + (f" ({skipped} bot/automation pages changed — body re-render skipped)" if skipped else "")]
    for c in p["changed"] + p["new"]:
        L.append(f"- {c.get('what', 'changed')}: **{c['title']}** (`{c['id'][:8]}`) {c.get('path', '')}"
                 + (f" — {c['comments']} comments" if c.get("comments") else ""))
    for c in p["deleted"]:
        L.append(f"- deleted/trashed: **{c['title']}** (`{c['id'][:8]}`)")
    for c in p["renamed"]:
        L.append(f"- renamed: {c['from']} -> {c['to']}")
    for c in p["placed"][:30]:
        L.append(f"- placed from _unplaced: `{c['id'][:8]}` -> {c['to']}")
    if len(p["placed"]) > 30:
        L.append(f"- … +{len(p['placed']) - 30} more placed")
    for e in p["errors"]:
        L.append(f"- ERROR page {e['id'][:8]} '{e['title'][:40]}': {e['error']}")
    c = r["comments"]
    L += ["", f"## Comments — {c['pages_rescanned']} pages rescanned, +{c['added']} new, "
          f"{c['retained']} newly marked resolved/deleted (kept)"]
    if c.get("contamination_breaches"):
        L.append(f"- **FAILED: {c['contamination_breaches']} comment text(s) attributed to more than "
                 f"{MAX_COMMENT_PAGES} pages** — see Notes")
    rs = c.get("resolution")
    if rs:
        L.append(f"- resolution check: {rs['threads']} open threads on {rs['blocks']} blocks — "
                 f"{rs['resolved']} resolved, {rs['added']} added (missed by the webhook), "
                 f"{rs['reopened']} reopened, {rs['deleted']} deleted; "
                 f"{rs['looked_up']} thread blocks looked up, {rs['errors']} unreadable")
    fo = c.get("folded")
    if fo:
        L.append(f"- webhook fold: +{fo['added']} added, {fo['updated']} edited, "
                 f"{fo['deleted']} deleted across {fo['pages']} pages and {fo['rows']} rows, "
                 f"no requests ({fo['unplaced']} captured comments on pages not mirrored yet)")
    au = c.get("audit")
    if au:
        bits = []
        if "pages_scanned" in au:
            bits.append(f"{au['pages_scanned']}/{au['pages_total']} human pages "
                        f"({au['page_cycle_days']:g}-day cycle; oldest scan now "
                        f"{(au.get('oldest_page_scan') or 'never')[:10]})")
        if "rows_scanned" in au:
            bits.append(f"{au['rows_scanned']}/{au['rows_pool']} comment-bearing rows "
                        f"({au['row_cycle_days']:g}-day cycle)")
        L.append("- per-block comment audit: " + "; ".join(bits))
    rp = c.get("row_probes")
    if rp:
        L.append(f"- row probes: {rp['carry']} carried their comments, {rp['scan']} read them")
    a = r["attachments"]
    if a["downloaded"] or a["failed"]:
        L += ["", f"## Attachments — {len(a['downloaded'])} downloaded, {len(a['failed'])} failed"]
        for x in a["downloaded"][:20]:
            L.append(f"- {x['file']} ({x['bytes'] // 1024} KB)")
        for x in a["failed"][:20]:
            L.append(f"- FAILED block {x['block'][:8]}: {x['error']}")
    rw = r.get("rows")
    if rw:
        L += ["", f"## Rows — {len(rw['refreshed'])}/{rw['requested']} refreshed"]
        for x in rw["skipped"]:
            L.append(f"- skipped `{x['row'][:8]}`: {x['why']}")
        for e in rw["errors"]:
            L.append(f"- ERROR row `{e['row'][:8]}`: {e['error']}")
    pp = r.get("props_probe")
    if pp:
        L += ["", f"## Property probes — {pp['drained']} drained, "
              f"{pp['dropped']} dropped"]
        for e in pp["errors"][:10]:
            L.append(f"- ERROR row `{e['row'][:8]}`: {e['error']}")
    cov = r.get("coverage")
    if cov:
        # The findings themselves are enumerated once, in the coverage note under
        # `## Notes`. Listing them again here put them at two depths in the one
        # document the changelog analysis is told to read first.
        L += ["", f"## Coverage — {len(cov['findings'])} new gap(s) "
              f"({cov['absent']} referenced-but-absent, {cov['excluded']} excluded; "
              f"{cov['scan_s']}s scan)"]
    if r["notes"]:
        L += ["", "## Notes"] + [f"- {n}" for n in r["notes"]]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- main phases

def consume_db_events(state, report, discovered):
    """database.*/data_source.* webhook events -> same-day lifecycle handling:
    *.deleted on a known DB seeds a 404-strike (removal confirms this run
    instead of two runs); *.created/undeleted on unknown ids feed discovery
    (data_source events map through their parent database id)."""
    path = os.path.join(STATE, "webhook-db-events.json")
    events = jload(path, {})
    if not events:
        return
    known = db_dirs()
    for eid, ev in events.items():
        etype = ev.get("type", "")
        target = eid
        if etype.startswith("data_source.") and ev.get("parent_type") == "database":
            target = ev.get("parent") or eid
        if etype == "database.deleted" and target in known:
            state["db404"][target] = max(state["db404"].get(target, 0), 1)
            report["notes"].append(f"webhook: database.deleted for '{known[target][:40]}' — removal confirms this run")
        elif etype.endswith((".created", ".undeleted")) and target not in known:
            discovered.add("db:" + target)
    if DRY_RUN:
        return
    jsave(path, {})  # consumed (sweeps are the backstop for any race)
    log(f"consumed {len(events)} webhook db event(s)")


def check_webhook_liveness(report):
    """A webhook feed cannot detect its own gaps: if the receiver dies, events just
    stop and nothing local says so — the mirror quietly loses its freshness path
    while every run still looks healthy: comments would then reach it only
    through the rolling audit, days late, and a resolved-before-audit comment
    never. Warn.
    Threshold is set off the measured distribution, not a guess: over the first
    3.2 days of the subscription (13,122 events) the largest inter-event gap was
    1.69h and p99 was 0.15h, and the daily job-feed refresh alone guarantees a
    burst per day."""
    ev = os.path.join(STATE, "webhook-events.jsonl")
    if not os.path.exists(ev):
        return
    quiet_h = (time.time() - os.path.getmtime(ev)) / 3600
    if quiet_h >= float(os.environ.get("NOTION_REFRESH_WEBHOOK_QUIET_H", "24")):
        report["notes"].append(
            f"⚠ webhook feed silent for {quiet_h:.0f}h (largest gap ever observed: 1.7h) — check "
            f"`systemctl status notion-webhook` and the integration's webhook subscription. "
            f"Until it is back, new comments reach the mirror only through the rolling audit, "
            f"which covers pages and rows already carrying comments.")


# ------------------------------------------------- row-scoped refresh (--mode rows)

def csv_cols(csv_path):
    """The DB CSV's column names, read from its header line alone."""
    if not csv_path or not os.path.exists(csv_path):
        return []
    with open(csv_path, newline="") as f:
        for row in csv.reader(f):
            return row[1:]
    return []


def db_csv_path(dirpath):
    for f in os.listdir(dirpath):
        if f.endswith(".csv") and ID32.search(f):
            return os.path.join(dirpath, f)
    return None


def update_csv_row(csv_path, cols, rid, page, users):
    """Rewrite one row's line in the DB CSV, preserving row order. True if changed.

    The CSV line and the row .md's property table are the same photograph taken
    two ways. A probe that moved one and not the other would leave the mirror
    disagreeing with itself in a way no later run detects: the nightly compares
    the CSV against Notion, never against the .md."""
    if not csv_path or not os.path.exists(csv_path):
        return False
    header, rows = read_csv(csv_path)
    if not header:
        return False
    vals = [rid] + [cell((page.get("properties") or {}).get(c), users) for c in cols]
    if rows.get(rid) == vals:
        return False
    order = list(rows.keys()) + ([] if rid in rows else [rid])
    rows[rid] = vals
    buf = io.StringIO()
    wtr = csv.writer(buf)
    wtr.writerow(header)
    for k in order:
        wtr.writerow(rows[k])
    blob = buf.getvalue().encode()
    with open(csv_path, "rb") as f:
        if blob == f.read():
            return False
    with open(csv_path, "wb") as f:
        f.write(blob)
    return True


# The walker's rendering of a child_database block, which is where a row body
# names a database nobody has mirrored. Reading it back out of the enrichment is
# how a rows run notices one without probe_row having to hand its Walker over.
CHILD_DB_RE = re.compile(r"— database `([0-9a-f]{32})` \(rows in workspace/_databases/\)")


def persist_discovery(discovered):
    """Carry databases noticed during a rows run over to the next nightly.

    A rows run has no discovery phase of its own — that is a full `/search`, a
    nightly cost — and an inline child database is exactly what `/search` does
    not return. Dropping the id at function exit would leave the DB unmirrored
    with nothing on disk recording that it was ever seen."""
    tags = {t for t in discovered if t.startswith("db:") and t[3:]}
    if not tags:
        return
    path = os.path.join(STATE, "pending-discovery.json")
    cur = set(jload(path, []))
    if tags - cur:
        jsave(path, sorted(cur | tags))


def props_queue_path():
    return os.path.join(STATE, "props-probe-queue.json")


def props_processing_path():
    return os.path.join(STATE, "props-probe-queue.processing.json")


def drain_props_probe(api, users, state, report, args, discovered=None):
    """Drain `props-probe-queue.json`: one GET per row, property table + CSV line.

    Its own file, written by the receiver alone, so no end-of-run rewrite of
    some other state file from an in-memory copy can erase what arrived during
    a multi-hour nightly.

    Snapshot-and-swap: rename the queue to a processing file and drain from
    there, so appends arriving mid-drain land in a fresh queue and are picked up
    on the next take. An orphaned processing file (a drain killed mid-flight) is
    drained first; without that its entries are never looked at again, which is
    the silent loss the whole file separation exists to avoid.

    An event arriving between the rename and the receiver's next write is a
    benign race, deliberately not "fixed": it lands in the new queue file and
    costs at most one duplicate GET and an idempotent re-render. The alternative
    is a lock spanning both processes, and the receiver is a long-lived daemon
    that cannot hold the mirror lock for hours.

    Residual loss is bounded: a lost entry costs staleness until the nightly,
    whose per-DB query re-renders every row's property table wholesale. This
    probe makes property freshness hourly; it is not the only path to it. The
    queue is deduped by row in the receiver, so a drain costs at most one
    request per edited row whatever the event volume."""
    if args.dry_run:
        return
    stats = report.setdefault("props_probe", {"drained": 0, "dropped": 0, "errors": []})
    qp, pp = props_queue_path(), props_processing_path()
    dirs = db_dirs()
    mds = {}
    for _pass in range(2):  # the orphan, then the live queue
        if not os.path.exists(pp):
            if not os.path.exists(qp):
                return
            try:
                os.replace(qp, pp)
            except OSError:
                return
        batch = [e for e in jload(pp, []) if isinstance(e, dict) and e.get("row")]
        for item in batch:
            rid = undash(item["row"])
            try:
                page = api.get(f"/pages/{dashed(rid)}")
            except ApiError as e:
                stats["dropped"] += 1
                stats["errors"].append({"row": rid, "error": str(e)[:160]})
                continue
            db_id = undash(((page.get("parent") or {}).get("database_id")) or "")
            dirname = dirs.get(db_id)
            if not dirname:
                stats["dropped"] += 1
                if db_id and discovered is not None:
                    discovered.add("db:" + db_id)
                continue
            dirpath, title, idx = row_render_target(dirname, mds)
            try:
                expand_truncated_props(api, page, title, report)
                csvp = db_csv_path(dirpath)
                cols = csv_cols(csvp)
                if not cols:
                    # render_row_md builds the property table from `cols`, so
                    # rendering with none would blank the table of every row it
                    # touched. A DB dir with no readable CSV is a broken mirror,
                    # not a row to re-render; leave it to the nightly's sweep.
                    stats["dropped"] += 1
                    stats["errors"].append({"row": rid, "error": f"no CSV header in {dirname}"})
                    continue
                # probe=False is the whole point of this kind: upsert_row_md
                # carries the stored enrichment over verbatim, so the body is
                # never read, never re-walked and never at risk of being lost.
                upsert_row_md(api, users, page, db_id, title, cols, dirpath, False,
                              state, report, args, md_idx=idx)
                update_csv_row(csvp, cols, rid, page, users)
            except ApiError as e:
                stats["dropped"] += 1
                stats["errors"].append({"row": rid, "error": str(e)[:160]})
                continue
            stats["drained"] += 1
        try:
            os.remove(pp)
        except OSError:
            pass


def phase_rows(api, users, state, report, args, ids, discovered):
    """Refresh exactly the rows named on the command line, and nothing else.

    It skips consume_db_events, which clears webhook-db-events.json: a phase
    with no discovery of its own would eat the same-day capture signal for
    every newly created database and give nothing back.

    Every named row is probed unconditionally. db_probe_policy's enriched-only
    rule exists so a feed DB's churn cannot cost thousands of body walks in a
    blind sweep; here the rows have already been named. Consulting it would let
    a low-enrichment DB turn the whole run into a body-level no-op, and its
    7-day cache would make that failure stick for a week."""
    dirs = db_dirs()
    stats = report.setdefault("rows", {"requested": len(ids), "refreshed": [],
                                       "skipped": [], "errors": []})
    mds = {}
    for rid in ids:
        rid = undash(rid)
        try:
            page = api.get(f"/pages/{dashed(rid)}")
        except ApiError as e:
            stats["errors"].append({"row": rid, "error": str(e)[:160]})
            continue
        db_id = undash(((page.get("parent") or {}).get("database_id")) or "")
        dirname = dirs.get(db_id)
        if not dirname:
            stats["skipped"].append({"row": rid,
                                     "why": f"database {db_id[:8] or '?'} is not mirrored"})
            if db_id:
                discovered.add("db:" + db_id)
            continue
        dirpath, title, idx = row_render_target(dirname, mds)
        try:
            expand_truncated_props(api, page, title, report)
            csvp = db_csv_path(dirpath)
            cols = csv_cols(csvp)
            if not cols:
                # see drain_props_probe: rendering with no columns blanks the
                # property table rather than refreshing it
                stats["skipped"].append({"row": rid, "why": f"no CSV header in {dirname}"})
                continue
            upsert_row_md(api, users, page, db_id, title, cols, dirpath, True,
                          state, report, args, md_idx=idx)
            update_csv_row(csvp, cols, rid, page, users)
        except ApiError as e:
            stats["errors"].append({"row": rid, "error": str(e)[:160]})
            continue
        stats["refreshed"].append(rid)
        enr = existing_enrichment(os.path.join(dirpath, idx.get(rid, ""))) or ""
        for did in CHILD_DB_RE.findall(enr):
            discovered.add("db:" + did)
    drain_props_probe(api, users, state, report, args, discovered)
    if not args.dry_run:
        persist_discovery(discovered)  # after the drain, which also discovers


def phase_dbs(api, users, state, report, args, discovered):
    dirs = db_dirs()
    todo = sorted(dirs.items(), key=lambda kv: kv[1].lower())
    if args.dbs:
        pat = args.dbs.lower()
        todo = [(i, d) for i, d in todo if pat in d.lower() or pat in i]
    for db_id, dirname in todo:
        report["dbs"]["checked"] += 1
        try:
            refresh_db(api, users, db_id, dirname, state, report, args, discovered)
        except Exception as e:  # noqa: BLE001 - one bad DB must not kill the sweep
            report["dbs"]["errors"].append({"db": dirname, "op": "refresh", "error": f"{type(e).__name__}: {e}"[:300]})
        if report["dbs"]["checked"] % 50 == 0:
            log(f"DB sweep: {report['dbs']['checked']}/{len(todo)} (req={api.n})")


def search_sweep(api, body, report):
    """Every result of a POST /search sweep. Notion now and then rejects a cursor it
    issued itself mid-sweep (400 validation_error, "The start_cursor provided is
    invalid"); the sweep restarts from the top once, so results yielded before the
    rejection reach the caller a second time and the caller has to be idempotent
    (phase_content is: consider() and seen_ids are). A second rejection, or any
    other error, is raised as before."""
    for attempt in range(2):
        try:
            yield from api.paginate("POST", "/search", body=body)
            return
        except ApiError as e:
            if attempt or e.code != 400 or "start_cursor" not in e.body:
                raise
            log("content search: Notion rejected its own start_cursor; restarting the sweep")
            report["notes"].append("content search: Notion rejected its own cursor mid-sweep; "
                                   "the sweep restarted from the top")


def phase_content(api, users, state, report, args, mode, discovered):
    """Full page-search sweep every run: change detection, metadata refresh, and
    deletion detection (DB rows cross-checked against the row sweep for free;
    content pages get a verifying GET before any local delete)."""
    meta, order = load_meta_jsonl()
    page_index = index_workspace_pages()
    row_index = row_md_global_index()
    since = state.get("content_since") or ""
    changed_objs = {}
    max_seen = since
    complete = True

    def consider(r):
        nonlocal max_seen
        m = meta_of(r, users)
        i = m["id"]
        old = meta.get(i)
        if m["last_edited_time"] and m["last_edited_time"] > (max_seen or ""):
            max_seen = m["last_edited_time"]
        if old is None or old.get("last_edited_time") != m["last_edited_time"] \
                or old.get("title") != m["title"] or old.get("parent_id") != m["parent_id"] \
                or old.get("archived") != m["archived"] or old.get("in_trash") != m["in_trash"]:
            changed_objs[i] = (m, old)

    seen_ids = set()
    for r in search_sweep(api, {
            "filter": {"value": "page", "property": "object"},
            "sort": {"timestamp": "last_edited_time", "direction": "ascending"}}, report):
        consider(r)
        seen_ids.add(undash(r["id"]))
    for i, old in list(meta.items()):
        if i in seen_ids:
            continue
        pt = old.get("parent_type")
        if pt == "database_id":
            dbmap = state["rows"].get(old.get("parent_id", ""))
            if dbmap is not None and i not in dbmap:
                meta.pop(i, None)
                if i in order:
                    order.remove(i)
            continue
        try:
            pg = api.get(f"/pages/{dashed(i)}")
            if pg.get("in_trash") or pg.get("archived"):
                raise ApiError(404, "in_trash")
            consider(pg)
        except ApiError as e:
            if e.code != 404:
                # Not a verdict about the page (a 5xx that outlasted the
                # retries, a dropped connection): deleting on it would empty the
                # mirror during an outage. Keep it; the next run asks again.
                report["pages"]["errors"].append({"id": i, "title": old.get("title", ""),
                                                  "error": f"deletion check: {e}"[:160]})
                complete = False
                continue
            if pt in ("page_id", "block_id", "workspace"):
                path = page_index.get(i)
                report["pages"]["deleted"].append({"id": i, "title": old.get("title", "")})
                if path and not args.dry_run:
                    os.remove(path)
            meta.pop(i, None)
            if i in order:
                order.remove(i)

    # pages whose last walk failed or was partial: force a re-walk this run
    for pid, attempts in list(state["retry_pages"].items()):
        if attempts > 5:
            report["notes"].append(f"giving up re-walking page {pid[:8]} after {attempts} attempts")
            state["retry_pages"].pop(pid)
            continue
        if pid in meta and pid not in changed_objs:
            changed_objs[pid] = (meta[pid], {"last_edited_time": "(retry)"})

    # minute-granularity guard: last_edited_time rounds to the minute, so an edit
    # made moments AFTER we read a page can share its le with the version we
    # rendered — invisible to le-diffing forever. If a page's le collides with
    # (or trails just behind) the moment we last walked it, re-walk once; the
    # fresh walk stamp clears the condition. The walk stamp is `page_walks`; a
    # page not walked since that record began falls back to its comment scan,
    # which every walk used to set.
    def _ts(s):
        try:
            return dt.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return None
    for pid, mm in meta.items():
        if pid in changed_objs or mm.get("parent_type") not in ("page_id", "block_id", "workspace"):
            continue
        le = _ts(mm.get("last_edited_time") or "")
        scan = _ts(state["page_walks"].get(pid) or state["comment_scans"].get(pid) or "")
        if le and scan and scan - dt.timedelta(seconds=150) <= le <= scan:
            changed_objs[pid] = (mm, {"last_edited_time": "(minute-edge recheck)"})

    # process changes: content pages get re-walked; DB rows (handled by the DB
    # sweep) still get their metadata upserted here.
    comment_updates = {}
    for i, (m, old) in sorted(changed_objs.items(), key=lambda kv: kv[1][0].get("last_edited_time") or ""):
        meta[i] = m
        if i not in order:
            order.append(i)
        if m["parent_type"] not in ("page_id", "block_id", "workspace"):
            continue
        if m.get("in_trash") or m.get("archived"):
            path = page_index.get(i)
            report["pages"]["deleted"].append({"id": i, "title": m.get("title", "")})
            if path and not args.dry_run:
                os.remove(path)
            continue
        body_changed = old is None or old.get("last_edited_time") != m.get("last_edited_time")
        # Scope rule, not a request cap: a page under an automation subtree (bot
        # delta logs — up to 70k blocks of "No changes detected" — and recordings)
        # is captured once, at first sight, and never re-walked; its metadata still
        # refreshes. They are append-only, change daily and carry no human signal:
        # on 2026-07-20 thirteen of them re-walked nightly cost ~14k requests, and a
        # per-page request cap only turned that into a truncated render repeated
        # every night. Nothing under them is comment-scanned either.
        if body_changed and old is not None and i in page_index \
                and _is_automation(i, page_index):
            report["pages"]["automation_skipped"] = report["pages"].get("automation_skipped", 0) + 1
            continue
        try:
            if body_changed:
                path, w = walk_content_page(api, users, m, page_index, report, args,
                                            meta=meta, row_index=row_index)
                if w.partial:
                    state["retry_pages"][i] = state["retry_pages"].get(i, 0) + 1
                    report["notes"].append(f"page {i[:8]} '{m.get('title', '')[:40]}' captured partially (inaccessible child blocks) — will retry")
                else:
                    state["retry_pages"].pop(i, None)
                state["page_walks"][i] = now_iso()
                for did, _t in w.child_dbs:
                    discovered.add("db:" + did)
                # Comments are read here only for a page never scanned before,
                # while its blocks are in hand: an edit says nothing about the
                # comments, which arrive through the webhook fold and whose
                # resolution the rolling audit reads. And never under an
                # automation subtree (the scope rule's other half).
                n_comments = 0
                if i not in state["comment_scans"] and not _is_automation(i, page_index):
                    bc, _capped = w.harvest_comments()
                    pl = w.comments_for(dashed(i))
                    comment_updates[i] = {"title": m.get("title") or "(untitled)",
                                          "bullets": union_captured(comment_bullets(pl, bc), i, users)}
                    state["comment_scans"][i] = now_iso()
                    n_comments = len(comment_updates[i]["bullets"])
                    report["comments"]["pages_rescanned"] += 1
                entry = {"id": i, "title": m.get("title", ""), "path": os.path.relpath(path, WS),
                         "what": "new" if old is None else "changed",
                         "comments": n_comments}
                (report["pages"]["new"] if old is None else report["pages"]["changed"]).append(entry)
            elif old and (old.get("title") != m.get("title") or old.get("parent_id") != m.get("parent_id")):
                path = page_index.get(i)
                if path and old.get("title") != m.get("title") and not args.dry_run:
                    new_path = os.path.join(os.path.dirname(path), f"{sanitize(m.get('title'))} {i}.md")
                    if os.path.abspath(new_path) != os.path.abspath(path):
                        os.replace(path, new_path)
                        if os.path.isdir(path[:-3]):
                            os.replace(path[:-3], new_path[:-3])
                        page_index[i] = new_path
                        report["pages"]["renamed"].append({"from": os.path.relpath(path, WS),
                                                           "to": os.path.relpath(new_path, WS)})
        except ApiError as e:
            report["pages"]["errors"].append({"id": i, "title": m.get("title", ""), "error": str(e)[:200]})
            state["retry_pages"][i] = state["retry_pages"].get(i, 0) + 1

    place_unplaced_pass(api, users, meta, page_index, row_index, state, report, args)

    if not args.dry_run:
        update_comments_md(comment_updates, report)
        save_meta_jsonl(meta, order)
        if complete:
            state["content_since"] = max_seen
    return meta


# Machine-generated subtrees (workspace/-relative prefixes, colon-separated in
# `NOTION_MIRROR_AUTOMATION_SUBTREES`): typically most of the block volume and no
# human comment traffic. Out of scope for everything but a first capture: never
# re-walked (phase_content) and never comment-scanned. Empty when unset.
AUTOMATION_SUBTREES = tuple(
    s for s in (mirror_root.config("NOTION_MIRROR_AUTOMATION_SUBTREES") or "").split(":") if s)


def _is_automation(pid, page_index):
    p = page_index.get(pid)
    if not p:
        return False
    rel = os.path.relpath(p, WS)
    return any(rel.startswith(s + os.sep) for s in AUTOMATION_SUBTREES)


def scan_page_comments(api, users, m, pool=None):
    """Per-block comment rescan of one page -> bullets (walk + harvest +
    page-level + webhook-captured union). `pool` lists the blocks concurrently."""
    w = Walker(api, users, max_blocks=6000)
    sink = []
    w.walk(dashed(m["id"]), sink, 0)
    pl = w.comments_for(dashed(m["id"]))
    bc, _capped = w.harvest_comments(pool=pool)
    return union_captured(comment_bullets(pl, bc), m["id"], users)


def _audit_days(name, default):
    """A cycle length in days from the environment; a bad value refuses the run
    at the phase rather than quietly auditing some other share of the corpus."""
    raw = os.environ.get(name, "").strip()
    days = float(raw) if raw else float(default)
    if days <= 0:
        raise ValueError(f"{name}={raw!r}: an audit cycle must be a positive number of days")
    return days


def audit_share(total, days):
    """How many of `total` to audit tonight so the whole set cycles in `days`."""
    return min(total, math.ceil(total / days)) if total else 0


def _has_open_legacy(bullets):
    """An open bullet with no comment id: the resolution check cannot reach it."""
    return any(not bullet_cid(b) and not RESOLVED_MARK.search(b) for b in bullets)


def _audit_order(ids, last, legacy, legacy_n, share):
    """Tonight's audit: up to `legacy_n` of the ids holding open id-less comments
    (their one rescan gives those comments ids, after which the resolution check
    covers them), then the regular share, longest-unscanned first."""
    order = sorted(ids, key=lambda i: (last(i), i))
    first = [i for i in order if i in legacy][:legacy_n]
    taken = set(first)
    return first, first + [i for i in order if i not in taken][:share]


def _same_in(b, bullets):
    """Is the comment `b` renders among `bullets` (by id, else by author, day and
    words, anchors aside)?"""
    cid = bullet_cid(b)
    for x in bullets:
        if cid and bullet_cid(x) == cid:
            return True
        if not cid or not bullet_cid(x):
            if same_comment(b, x) if not cid else same_comment(x, b):
                return True
            pb, px = _bullet_parts(b), _bullet_parts(x)
            if pb and px and pb[1:] == px[1:]:
                return True
    return False


def phase_comment_dedup(api, state, report, args, meta_titles=None):
    """--mode comment-dedup, once: bring the stored comments to one bullet per
    comment in one place, as every writer now keeps them.

    1. A `_comments.md` section for an id with a row file moves into the row file.
    2. Every section and row loses its repeated ids and the id-less bullets whose
       id-bearing copy sits beside them (`reconcile_bullets`).
    3. A comment held by more than one object is kept only where its thread is,
       when its thread is one of those objects: a page's scan used to list its
       child pages' page-level comments too. The thread comes from
       `comment-parents.json`, else from `GET /comments/{id}` (a few dozen). A
       thread on a block elsewhere (a synced block shown in both pages) stays in
       both.
    """
    today = dt.datetime.now(UTC).strftime("%Y-%m-%d")
    head, sections, appendix = load_comments_md()
    rows = row_md_global_index()
    parents = state["comment_parents"]
    st = {"sections_moved": 0, "moved_bullets": 0, "legacy_dropped": 0, "repeats_dropped": 0,
          "child_page_copies_dropped": 0, "thread_lookups": 0, "rows_changed": 0,
          "sections_changed": 0}
    places, originals, row_txt = {}, {}, {}   # id -> bullets; id -> as found; id -> file text
    for rid, where in rows.items():
        try:
            txt = open(os.path.join(DBS, *where)).read()
        except (OSError, UnicodeDecodeError):
            continue
        if MARKER in txt and has_comments(txt.split(MARKER, 1)[1]):
            row_txt[rid] = txt
            places[rid] = originals[rid] = split_bullets(stored_comments_body(txt.split(MARKER, 1)[1]),
                                                          prefix="- _")
    kept = []
    for sec in sections:
        bs = split_bullets(sec["body"])
        if sec["id"] in rows:
            st["sections_moved"] += 1
            st["moved_bullets"] += len(bs)
            places[sec["id"]] = places.get(sec["id"], []) + [to_row_bullet(b) for b in bs]
            originals.setdefault(sec["id"], [])
            continue
        kept.append(sec)
        places[sec["id"]] = originals[sec["id"]] = bs
    for i, bs in places.items():
        once = dedup_by_cid(bs)
        st["repeats_dropped"] += len(bs) - len(once)
        places[i] = reconcile_bullets(once)
        st["legacy_dropped"] += len(once) - len(places[i])
    holders = collections.defaultdict(set)
    for i, bs in places.items():
        for b in bs:
            if bullet_cid(b):
                holders[bullet_cid(b)].add(i)
    for cid, ids in holders.items():
        if len(ids) < 2:
            continue
        b = next(x for x in places[next(iter(ids))] if bullet_cid(x) == cid)
        did = (CID_MARK.search(b).group(2) or "") if CID_MARK.search(b) else ""
        home = parents.get(did) if did else None
        if home is None:
            st["thread_lookups"] += 1
            try:
                par = api.get(f"/comments/{dashed(cid)}").get("parent") or {}
            except ApiError:
                continue
            home = par.get("page_id") or par.get("block_id")
            if not home:
                continue
            if did:
                parents[did] = undash(home)
        home = undash(home)
        if home not in ids:
            continue
        for i in ids - {home}:
            places[i] = [x for x in places[i] if bullet_cid(x) != cid]
            st["child_page_copies_dropped"] += 1
    # What no thread can place (a resolved or deleted comment, a bullet with no
    # id): the parent's copy is anchored on the child page's title, and the child
    # holds the same comment.
    titles = collections.defaultdict(set)
    for sec in sections:
        titles[sec["title"].strip()[:90]].add(sec["id"])
    for rid in places:
        t = (meta_titles or {}).get(rid)
        if t:
            titles[t.strip()[:90]].add(rid)
    for i in list(places):
        keep = []
        for b in places[i]:
            parts = _bullet_parts(b)
            others = titles.get((parts[0] or "").strip()[:90], set()) - {i} if parts and parts[0] else set()
            if any(_same_in(b, places.get(y, [])) for y in others):
                st["child_page_copies_dropped"] += 1
                continue
            keep.append(b)
        places[i] = keep
    if not args.dry_run:
        for rid, where in rows.items():
            if rid not in places or places[rid] == originals.get(rid):
                continue
            st["rows_changed"] += 1
            txt = row_txt.get(rid) or open(os.path.join(DBS, *where)).read()
            if MARKER in txt:
                h, enr = txt.split(MARKER, 1)
            else:
                h, enr = txt.rstrip("\n") + "\n\n", ""
            with open(os.path.join(DBS, *where), "w") as f:
                f.write(h + MARKER + (with_comments(enr, places[rid]) or "\n"))
    else:
        st["rows_changed"] = sum(1 for rid in rows if rid in places and places[rid] != originals.get(rid))
    out = []
    for sec in kept:
        bs = places[sec["id"]]
        if bs != originals[sec["id"]]:
            st["sections_changed"] += 1
            if not bs:
                continue
            sec = dict(sec, body="\n" + "\n".join(bs) + "\n\n")
        out.append(sec)
    if not args.dry_run and (st["sections_moved"] or st["sections_changed"]):
        write_comments_md(head, out, appendix)
    report["comments"]["dedup"] = st
    log(f"comment dedup: {st['sections_moved']} row section(s) moved into their rows "
        f"({st['moved_bullets']} bullets); dropped {st['repeats_dropped']} repeated ids, "
        f"{st['legacy_dropped']} id-less bullets beside their id-bearing copy and "
        f"{st['child_page_copies_dropped']} copies of a child page's comments "
        f"({st['thread_lookups']} thread lookups); "
        f"{st['sections_changed']} sections and {st['rows_changed']} rows rewritten")


_EXPORT_PROP = re.compile(r"^([^\s:|<>\-*`!\[][^:\n]{0,80}): (.*)$")


def export_props_missing(page_txt, row_txt):
    """The properties an export-era page file lists (its `Key: value` block under the
    title) with a value the row file does not carry: a relation or rollup into a
    database not shared with the integration, which the API leaves out, or a
    property removed since the export. The page file is the only copy of those."""
    lines = page_txt.split("\n")[1:]
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    have = {m.group(1).strip() for m in re.finditer(r"^\| (.+?) \| .* \|$", row_txt, re.M)}
    words = set(re.findall(r"[a-z0-9]+", row_txt.lower()))
    out = set()
    while i < len(lines) and _EXPORT_PROP.match(lines[i]):
        key, val = _EXPORT_PROP.match(lines[i]).groups()
        key = key.strip().lstrip("# ")
        said = re.findall(r"[a-z0-9]+", f"{key} {val}".lower())
        if val.strip() and key not in have and not all(w in words for w in said):
            out.add(key)
        i += 1
    return out


def phase_row_page_dedup(report, args):
    """--mode row-page-dedup, once: a database row is mirrored as its row file only.

    A workspace export, and a one-off re-capture after it, also wrote a page file in
    the page tree for many database rows: the row's properties as `Key: value` lines
    and a body snapshot, never updated since, because the content phase walks content
    pages only (a row's metadata is all it refreshes). Such a page file goes once its
    row file carries the probe marker, so the row's body has been read into it; a
    row never probed keeps its page file and is listed, and so does a page file that
    holds a property value the row file lacks (`export_props_missing`). Markdown links elsewhere in
    the mirror that led to a removed file lead to its row file instead, and folders
    the removal leaves empty go too. No requests."""
    meta, _order = load_meta_jsonl()
    rows = row_md_global_index()
    st = {"page_files": 0, "removed": 0, "kept_unprobed": [], "kept_unique": {},
          "links_rewritten": 0, "files_relinked": 0, "folders_removed": 0}
    doomed = {}   # abs page path -> abs row path
    for root, dirs, files in os.walk(WS):
        if os.path.abspath(root) == os.path.abspath(DBS):
            dirs[:] = []
            continue
        for f in files:
            m = ID32.search(f) if f.endswith(".md") else None
            if not m or (meta.get(m.group(1)) or {}).get("parent_type") != "database_id" \
                    or m.group(1) not in rows:
                continue
            st["page_files"] += 1
            rpath = os.path.join(DBS, *rows[m.group(1)])
            try:
                rtxt = open(rpath).read()
            except OSError:
                rtxt = ""
            if MARKER not in rtxt:
                st["kept_unprobed"].append(m.group(1))
                continue
            only_here = export_props_missing(open(os.path.join(root, f), errors="replace").read(), rtxt)
            if only_here:
                st["kept_unique"][m.group(1)] = sorted(only_here)
                continue
            doomed[os.path.abspath(os.path.join(root, f))] = os.path.abspath(rpath)
    by_id = {ID32.search(os.path.basename(pth)).group(1): r for pth, r in doomed.items()}
    link = re.compile(r"\]\(([^)\s]*?([0-9a-f]{32})\.md)\)")
    for root, _dirs, files in os.walk(WS):
        for f in files:
            path = os.path.abspath(os.path.join(root, f))
            if not f.endswith(".md") or path in doomed:
                continue
            try:
                txt = open(path).read()
            except (OSError, UnicodeDecodeError):
                continue
            n = 0

            def relink(mm, here=os.path.dirname(path)):
                nonlocal n
                target = by_id.get(mm.group(2))
                if not target:
                    return mm.group(0)
                n += 1
                return "](" + urllib.parse.quote(os.path.relpath(target, here)) + ")"
            new = link.sub(relink, txt)
            if n:
                st["links_rewritten"] += n
                st["files_relinked"] += 1
                if not args.dry_run:
                    with open(path, "w") as fh:
                        fh.write(new)
    st["removed"] = len(doomed)
    if not args.dry_run:
        emptied = set()
        for pth in doomed:
            os.remove(pth)
            emptied.add(os.path.dirname(pth))
        ws = os.path.abspath(WS)
        for d in sorted(emptied, key=len, reverse=True):
            while d != ws and d.startswith(ws + os.sep) and os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
                st["folders_removed"] += 1
                d = os.path.dirname(d)
        if doomed and regenerate_structure_md():
            report["notes"].append("structure.md regenerated (tree changed)")
    report["row_page_dedup"] = st
    log(f"row page dedup: {st['removed']} of {st['page_files']} page files of database rows removed "
        f"({len(st['kept_unprobed'])} kept: row never probed; {len(st['kept_unique'])} kept: they hold "
        f"property values the row file lacks); {st['links_rewritten']} links in "
        f"{st['files_relinked']} files now lead to the row file; {st['folders_removed']} empty folders removed")


def phase_comment_audit_pages(api, users, meta, state, report, args, only_legacy=False):
    """The rolling per-block comment audit of the human content pages.

    A backstop now: the webhook fold brings new comments in and the resolution
    check notices resolved ones every night, so a full block-by-block rescan is
    only for what both miss — a comment.created the receiver never saw on a
    page with no open thread. Sized by count: every human page is rescanned
    once per NOTION_REFRESH_PAGE_AUDIT_DAYS (default 30), longest-unscanned
    first, so a page never scanned sorts to the front. Pages still holding open
    comments with no id go first, NOTION_REFRESH_LEGACY_PAGES (default 60) a
    night, until none are left. Automation subtrees are never scanned."""
    page_index = index_workspace_pages()
    scans = state["comment_scans"]
    human = {m["id"]: m for m in meta.values()
             if m.get("parent_type") in ("page_id", "block_id", "workspace")
             and not m.get("in_trash") and not m.get("archived")
             and not _is_automation(m["id"], page_index)}
    days = _audit_days("NOTION_REFRESH_PAGE_AUDIT_DAYS", 30)
    sections = load_comments_md()[1]
    if only_legacy:
        # --mode legacy-comments: every page still holding one, including a page
        # the metadata no longer lists (its scan says whether it still exists)
        for s in sections:
            if s["id"] not in human and s["id"] not in meta and \
                    not _is_automation(s["id"], page_index):
                human[s["id"]] = {"id": s["id"], "title": s["title"]}
    legacy = {s["id"] for s in sections
              if s["id"] in human and _has_open_legacy(split_bullets(s["body"]))}
    first, due = _audit_order(human, lambda i: scans.get(i, ""), legacy,
                              len(legacy) if only_legacy else
                              int(os.environ.get("NOTION_REFRESH_LEGACY_PAGES", "60")),
                              0 if only_legacy else audit_share(len(human), days))
    updates = {}
    with comment_listing(api) as pool:
        for n, pid in enumerate(due, 1):
            m = human[pid]
            started = now_iso()  # a comment made mid-scan is newer than the scan
            try:
                updates[pid] = {"title": m.get("title") or "(untitled)",
                                "bullets": scan_page_comments(api, users, m, pool=pool)}
            except ApiError as e:
                report["pages"]["errors"].append({"id": pid, "title": m.get("title", ""),
                                                  "error": f"comment audit: {e}"[:160]})
            scans[pid] = started  # an erroring page still moves to the back
            if n % 100 == 0:
                log(f"page comment audit {n}/{len(due)} (req={api.n})")
    if not args.dry_run:
        update_comments_md(updates, report)
    report["comments"]["pages_rescanned"] += len(updates)
    oldest = min((scans.get(i, "") for i in human), default="")
    report["comments"].setdefault("audit", {}).update({
        "pages_scanned": len(due), "pages_total": len(human), "page_cycle_days": days,
        "legacy_pages_scanned": len(first), "legacy_pages_left": len(legacy) - len(first),
        "oldest_page_scan": oldest})
    log(f"page comment audit: {len(due)}/{len(human)} human pages ({days:g}-day cycle; "
        f"{len(first)} of {len(legacy)} with id-less comments)")


def seed_comment_rows(state):
    """The row audit's pool: every row file that carries a comments region,
    re-read from disk each run (about 15 s). Joining only on the engine's own
    writes left out every row whose comments arrived another way — 239 rows
    holding id-less comments were outside a 971-row pool on 2026-09-26."""
    crows = state["comment_rows"]
    for _db_id, dirname in db_dirs().items():
        dirpath = os.path.join(DBS, dirname)
        for f in os.listdir(dirpath):
            mm = ID32.search(f)
            if not (mm and f.endswith(".md") and not f.startswith("_schema")):
                continue
            try:
                if has_comments(open(os.path.join(dirpath, f)).read()):
                    crows.setdefault(mm.group(1), "")
            except OSError:
                continue
    log(f"comment-row audit seeded: {len(crows)} rows")


def phase_comment_audit_rows(api, users, state, report, args, discovered=None, only_legacy=False):
    """The rolling per-block comment audit of the comment-bearing DB rows, the
    same backstop as the page audit: every row in the pool re-probed with a full
    comment read once per NOTION_REFRESH_ROW_AUDIT_DAYS (default 14),
    longest-unaudited first, rows holding open id-less comments first
    (NOTION_REFRESH_LEGACY_ROWS, default 100, a night)."""
    crows = state["comment_rows"]
    seed_comment_rows(state)
    row_to_db = {rid: db for db, rows in state["rows"].items() for rid in rows}
    for rid in [r for r in crows if r not in row_to_db]:
        crows.pop(rid)  # row deleted since it joined
    days = _audit_days("NOTION_REFRESH_ROW_AUDIT_DAYS", 14)
    index = row_md_global_index()
    legacy = set()
    for rid in crows:
        if rid in index:
            d, f = index[rid]
            try:
                txt = open(os.path.join(DBS, d, f)).read()
            except OSError:
                continue
            if MARKER in txt and _has_open_legacy(split_bullets(
                    stored_comments_body(txt.split(MARKER, 1)[1]), prefix="- _")):
                legacy.add(rid)
    first, due = _audit_order(crows, lambda r: crows[r], legacy,
                              len(legacy) if only_legacy else
                              int(os.environ.get("NOTION_REFRESH_LEGACY_ROWS", "100")),
                              0 if only_legacy else audit_share(len(crows), days))
    dirs = db_dirs()
    mds = {}
    done = 0
    with comment_listing(api) as pool:
        for rid in due:
            dirname = dirs.get(row_to_db[rid])
            if not dirname:
                continue
            dirpath, title, idx = row_render_target(dirname, mds)
            try:
                page = api.get(f"/pages/{dashed(rid)}")
                expand_truncated_props(api, page, title, report)
                csvp = db_csv_path(dirpath)
                cols = csv_cols(csvp)
                if cols:
                    upsert_row_md(api, users, page, row_to_db[rid], title, cols, dirpath, "scan",
                                  state, report, args, md_idx=idx, discovered=discovered,
                                  pool=pool)
                    if not args.dry_run:
                        update_csv_row(csvp, cols, rid, page, users)
                done += 1
            except ApiError as e:
                report["dbs"]["errors"].append({"db": title, "op": f"comment audit {rid[:8]}",
                                                "error": str(e)[:160]})
            crows[rid] = now_iso()
    report["comments"].setdefault("audit", {}).update({
        "rows_scanned": done, "rows_pool": len(crows), "row_cycle_days": days,
        "legacy_rows_scanned": len(first), "legacy_rows_left": len(legacy) - len(first)})
    log(f"row comment audit: {done}/{len(due)} rows probed (pool {len(crows)}, {days:g}-day "
        f"cycle; {len(first)} of {len(legacy)} with id-less comments)")


@contextlib.contextmanager
def comment_listing(api):
    """An executor for the comment-listing phases, with the Api paced at
    NOTION_REFRESH_COMMENT_RPS (8) across NOTION_REFRESH_COMMENT_WORKERS (6) in
    flight. The engine is otherwise sequential and latency-bound (~2.3 req/s);
    these phases are thousands of independent GETs. Notion's per-connection
    limit is well above that (600 or 180 req/min by plan) but the workspace's
    is shared with the webhook receiver, tasksync and every other integration,
    so the rate stays below it and any 429/529 pauses every worker."""
    rps = float(os.environ.get("NOTION_REFRESH_COMMENT_RPS", "8"))
    workers = int(os.environ.get("NOTION_REFRESH_COMMENT_WORKERS", "6"))
    with api.rate(rps), concurrent.futures.ThreadPoolExecutor(max(1, workers)) as pool:
        yield pool


def _open_bullet_homes(rows_index=None):
    """{(kind, home id): [bullets]} for every page section and row file that
    holds at least one open (unannotated) bullet; kind is "page" or "row"."""
    out = {}
    for s in load_comments_md()[1]:
        bs = split_bullets(s["body"])
        if any(not RESOLVED_MARK.search(b) for b in bs):
            out[("page", s["id"])] = bs
    for rid, (d, f) in (rows_index if rows_index is not None else row_md_global_index()).items():
        try:
            txt = open(os.path.join(DBS, d, f)).read()
        except OSError:
            continue
        if MARKER not in txt or not has_comments(txt):
            continue
        bs = split_bullets(stored_comments_body(txt.split(MARKER, 1)[1]), prefix="- _")
        if any(not RESOLVED_MARK.search(b) for b in bs):
            out[("row", rid)] = bs
    return out


def _thread_of(bullet):
    m = CID_MARK.search(bullet)
    if not m or m.group(1) == CID_LEGACY:
        return None, None
    return m.group(1), (m.group(2) or m.group(1))


def capture_parents():
    """discussion id32 -> the block or page id32 its thread sits on, from the
    receiver's capture log (each record is one thread's parent and comments).

    Only the receiver's own records count. The resolved-comment backfill wrote
    its records into the same log with `captured_at: "backfill"` and the page as
    the entity whatever block the thread sat on; listing that page would find
    none of its block-anchored comments and mark every one resolved."""
    out = {}
    path = os.path.join(STATE, paths.CAPTURE)
    if not os.path.exists(path):
        return out
    for ln in open(path):
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if _parse_ts(e.get("captured_at")) is None:
            continue
        ent = undash(e.get("entity_id") or "")
        if len(ent) != 32:
            continue
        for c in e.get("comments", []):
            did = undash(c.get("discussion_id") or c.get("id") or "")
            if did:
                out[did] = ent
    return out


def _listed_comment(c, users):
    return Comment(rich_md(c.get("rich_text")), users.name(c.get("created_by")),
                   c.get("created_time", ""), undash(c.get("id") or ""),
                   undash(c.get("discussion_id") or ""))


def _render(c, kind, anchor):
    if kind == "row":
        t = c.text.replace("\r", "").replace("\n", "\n  ")
        return stamp_cid(f"- _{c.who} ({c.when[:10]}):_ {t}", c.cid, c.did)
    return stamp_cid(f'- **on** "{anchor}" — {c.who} ({c.when[:10]}): {c.text}', c.cid, c.did)


_ANCHOR = re.compile(r'^- \*\*on\*\* "(.*?)" — ')


def phase_resolution_check(api, users, state, report, args):
    """Every open comment thread in the mirror, re-listed where it lives, every night.

    Resolving a thread fires no webhook and a resolved comment simply stops
    being listed, so the only way to see one is to list the comments of the
    block it sat on. It can only happen to a thread the mirror holds open, so
    this lists exactly those blocks (a page-level thread's is the page) and
    marks any open comment the listing no longer returns resolved. A comment
    on a listed block that the mirror lacks (a missed comment.created) is added,
    and a resolved one listed again (a reopened thread) loses its annotation.

    A thread's block comes from the capture log, else from `GET /comments/{id}`
    (which answers for resolved comments too, so it is asked once per thread
    and remembered in comment-parents.json). A comment that answers 404 is
    deleted. id-less legacy bullets are out of reach here; the audit upgrades
    them."""
    today = dt.datetime.now(UTC).strftime("%Y-%m-%d")
    parents = state["comment_parents"]
    homes = _open_bullet_homes()
    threads = {}  # did -> [(home key, cid)]
    for key, bs in homes.items():
        for b in bs:
            if RESOLVED_MARK.search(b):
                continue
            cid, did = _thread_of(b)
            if cid:
                threads.setdefault(did, []).append((key, cid))
    stats = {"threads": len(threads), "homes": len(homes), "from_state": 0, "from_captures": 0,
             "looked_up": 0, "deleted": 0, "blocks": 0, "blocks_gone": 0, "errors": 0,
             "resolved": 0, "added": 0, "reopened": 0}
    caps = capture_parents()
    gone = set()  # comment ids that answered 404
    todo = []
    for did, members in threads.items():
        if did in parents:
            stats["from_state"] += 1
        elif did in caps:
            parents[did] = caps[did]
            stats["from_captures"] += 1
        else:
            todo.append((did, members[0][1]))

    def lookup(item):
        did, cid = item
        try:
            c = api.get(f"/comments/{dashed(cid)}")
        except ApiError as e:
            return did, cid, ("gone" if e.code == 404 else None)
        par = c.get("parent") or {}
        return did, cid, undash(par.get(par.get("type", "")) or "") or None

    with comment_listing(api) as pool:
        for did, cid, where in pool.map(lookup, todo):
            if where == "gone":
                gone.add(cid)
                stats["deleted"] += 1
            elif where:
                parents[did] = where
                stats["looked_up"] += 1
            else:
                stats["errors"] += 1
        blocks = sorted({parents[did] for did in threads if did in parents})
        stats["blocks"] = len(blocks)

        def listing(block):
            try:
                return block, [_listed_comment(c, users) for c in api.paginate(
                    "GET", "/comments", params={"block_id": dashed(block)})]
            except ApiError as e:
                return block, ("gone" if e.code == 404 else None)

        listed = dict(pool.map(listing, blocks))

    stats["blocks_gone"] = sum(1 for v in listed.values() if v == "gone")
    stats["errors"] += sum(1 for v in listed.values() if v is None)
    by_home = collections.defaultdict(set)  # home key -> blocks its threads sit on
    for did, members in threads.items():
        for key, _cid in members:
            if did in parents:
                by_home[key].add(parents[did])
    page_updates = {}
    meta = None
    rows_index = None
    for key, bs in homes.items():
        kind, home = key
        blocks_here = [b for b in by_home.get(key, ()) if listed.get(b) is not None]
        live = {}
        block_of = {}
        for b in blocks_here:
            if listed[b] != "gone":
                for c in listed[b]:
                    live[c.cid] = c
                    block_of[c.cid] = b
        checked = set(blocks_here)
        new = []
        seen = set()
        for b in bs:
            cid, did = _thread_of(b)
            if cid:
                seen.add(cid)
            if not cid:
                new.append(b)
            elif cid in gone and not RESOLVED_MARK.search(b):
                new.append(annotate_resolved(b, today))
                stats["resolved"] += 1
            elif parents.get(did) in checked:
                if cid in live and RESOLVED_MARK.search(b):
                    new.append(RESOLVED_MARK.sub("", b))
                    stats["reopened"] += 1
                elif cid not in live and not RESOLVED_MARK.search(b):
                    new.append(annotate_resolved(b, today))
                    stats["resolved"] += 1
                else:
                    new.append(b)
            else:
                new.append(b)
        anchor_of = {}
        for b in bs:
            cid, did = _thread_of(b)
            m = _ANCHOR.match(b)
            if cid and m and did in parents:
                anchor_of.setdefault(parents[did], m.group(1))
        legacy = {bullet_text_key(b) for b in bs if not bullet_cid(b)}
        for c in sorted(live.values(), key=lambda c: c.when):
            if c.cid in seen:
                continue
            block = block_of[c.cid]
            parents.setdefault(c.did or c.cid, block)  # known now: no lookup next night
            b = _render(c, kind, anchor_of.get(block, "(page-level)"))
            if upgrade_legacy(new, b) is not None:
                continue  # here as an id-less bullet, which takes the id and is open
            new.append(b)
            stats["added"] += 1
        if new == bs:
            continue
        if kind == "page":
            if meta is None:
                meta = load_meta_jsonl()[0]
            page_updates[home] = {"title": (meta.get(home) or {}).get("title") or
                                  next((s["title"] for s in load_comments_md()[1] if s["id"] == home),
                                       "(untitled)"),
                                  "bullets": new}
        elif not args.dry_run:
            if rows_index is None:
                rows_index = row_md_global_index()
            d, f = rows_index[home]
            path = os.path.join(DBS, d, f)
            txt = open(path).read()
            head, enr = txt.split(MARKER, 1)
            with open(path, "w") as fh:
                fh.write(head + MARKER + (with_comments(enr, new) or "\n"))
    if page_updates and not args.dry_run:
        update_comments_md(page_updates, report, merge=False)
    report["comments"]["added"] += stats["added"]
    report["comments"]["retained"] += stats["resolved"]
    report["comments"]["resolution"] = stats
    log(f"resolution check: {stats['threads']} open threads on {stats['blocks']} blocks — "
        f"{stats['resolved']} resolved, {stats['added']} added, {stats['reopened']} reopened, "
        f"{stats['deleted']} deleted, {stats['errors']} unreadable")


def phase_full_comment_sweep(api, users, meta, state, report, args):
    """Manual (--mode full-comments): per-block rescan of every HUMAN content
    page in one run (~55k requests, ~12h — automation subtrees excluded; their
    1.2M blocks are out of scope for comments altogether). A clean,
    complete sweep rebuilds _comments.md wholesale (dropping the historical
    appendix) provided no automation-page sections would be lost."""
    page_index = index_workspace_pages()
    updates = {}
    clean = True
    content = [m for m in meta.values() if m.get("parent_type") in ("page_id", "block_id", "workspace")
               and not m.get("in_trash") and not m.get("archived")
               and not _is_automation(m["id"], page_index)]
    log(f"full comment sweep over {len(content)} human content pages")
    for n, m in enumerate(sorted(content, key=lambda x: x["id"]), 1):
        try:
            updates[m["id"]] = {"title": m.get("title") or "(untitled)",
                                "bullets": scan_page_comments(api, users, m)}
            state["comment_scans"][m["id"]] = now_iso()
            report["comments"]["pages_rescanned"] += 1
        except ApiError as e:
            clean = False
            report["pages"]["errors"].append({"id": m["id"], "title": m.get("title", ""), "error": f"comment sweep: {e}"[:200]})
        if n % 20 == 0:
            log(f"comment sweep {n}/{len(content)} (req={api.n})")
    if not args.dry_run:
        update_comments_md(updates, report)
        if clean:
            report["notes"].append("full human-corpus comment sweep completed cleanly")


def skip_discovery(did, known, state):
    """Ids the nightly already has an answer for, so it spends no request.

    `unshared` is the reader half of a contract with `coverage_backfill.py`,
    whose HTTP-400 "no data sources accessible" branch records databases whose
    data sources are not shared with the integration; both sides round-trip the
    bucket through their state saves. The 1,322 ids the drained backfill had
    already excluded before the producer existed were seeded into db-flags.json
    by hand on 2026-08-07 (from exclusions.json's `no_access` entries) — the
    backfill will not re-run to write them itself. The arm matters: 1,284 of
    the references to those ids sit in row bodies, which row-body discovery
    re-harvests every night, so without it they are re-asked on every run —
    and each answer writes a `not_a_db` flag that is simply wrong, since they
    are unshared rather than linked views.
    """
    return not did or did in known or did in state["not_a_db"] or did in state.get("unshared", {})


def phase_discovery(api, users, state, report, args, discovered):
    known = set(db_dirs())
    for tag in sorted(discovered):
        if not tag.startswith("db:"):
            continue
        did = tag[3:]
        if skip_discovery(did, known, state):
            continue
        capture_new_db(api, users, did, state, report, args)
        known.add(did)
    # search-visible new databases: the run's catalog when there is one
    cat = getattr(api, "catalog", None)
    try:
        if isinstance(cat, dict):
            found = list(cat)
        else:
            # a result is a data source; the mirror is keyed by its database
            found = (undash(((r.get("parent") or {}).get("database_id")) or r["id"])
                     for r in api.paginate("POST", "/search", body={
                         "filter": {"value": "data_source", "property": "object"},
                         "sort": {"timestamp": "last_edited_time", "direction": "descending"},
                         "page_size": 100}))
        for did in found:
            if not skip_discovery(did, known, state):
                capture_new_db(api, users, did, state, report, args)
                known.add(did)
    except ApiError as e:
        report["notes"].append(f"database discovery search failed: {e}"[:200])


def phase_schema_sweep(api, users, state, report, args):
    """Every run: refresh every _schema.json/_schema.md (+ row/prop counts).
    Detects DB renames and cascades them (dir, csv, row-md headers).

    A database the run's catalog lists with one source costs nothing here
    (`get_database`); any other costs a GET of the database and one per source."""
    for db_id, dirname in sorted(db_dirs().items(), key=lambda kv: kv[1].lower()):
        dirpath = os.path.join(DBS, dirname)
        schema = jload(os.path.join(dirpath, "_schema.json"), {})
        title = schema.get("title") or dirname.rsplit(" ", 1)[0]
        csvf = next((os.path.join(dirpath, f) for f in os.listdir(dirpath)
                     if f.endswith(".csv") and ID32.search(f)), None)
        nrows = 0
        if csvf and os.path.exists(csvf):
            with open(csvf, newline="") as f:
                nrows = max(0, sum(1 for _ in csv.reader(f)) - 1)
        _props, live_title = refresh_schema_files(api, dirpath, db_id, title, nrows, report)
        if args.dry_run or live_title == title:
            continue
        # rename cascade
        new_dirname = f"{sanitize(live_title)} {db_id}"
        new_dirpath = os.path.join(DBS, new_dirname)
        if new_dirname != dirname and not os.path.exists(new_dirpath):
            os.rename(dirpath, new_dirpath)
            dirpath = new_dirpath
        if csvf:
            newcsv = os.path.join(dirpath, f"{sanitize(live_title)} {db_id}.csv")
            oldcsv = os.path.join(dirpath, os.path.basename(csvf))
            if os.path.exists(oldcsv) and os.path.abspath(oldcsv) != os.path.abspath(newcsv):
                os.rename(oldcsv, newcsv)
        old_hdr = f"| db: {db_id} ({title}) -->"
        new_hdr = f"| db: {db_id} ({live_title}) -->"
        for f in os.listdir(dirpath):
            if f.endswith(".md") and not f.startswith("_schema"):
                p = os.path.join(dirpath, f)
                try:
                    txt = open(p).read()
                except OSError:
                    continue
                if old_hdr in txt:
                    with open(p, "w") as fh:
                        fh.write(txt.replace(old_hdr, new_hdr, 1))
        report["notes"].append(f"database renamed: '{title}' -> '{live_title}'")


# ---------------------------------------------------------------- validate mode

def validate(api, users, args):
    """Re-render sample DBs; rows unedited since the build must match byte-for-byte."""
    cutoff = args.validate_cutoff
    dirs = db_dirs()
    sample = [(i, d) for i, d in sorted(dirs.items(), key=lambda kv: kv[1].lower())
              if not args.dbs or args.dbs.lower() in d.lower()]
    if not args.dbs:
        sample = sample[:6]
    mismatch = total_old = 0
    for db_id, dirname in sample:
        dirpath = os.path.join(DBS, dirname)
        csvf = next((os.path.join(dirpath, f) for f in os.listdir(dirpath)
                     if f.endswith(".csv") and ID32.search(f)), None)
        if not csvf:
            print(f"[skip] {dirname}: no csv")
            continue
        header, old_rows = read_csv(csvf)
        cols = header[1:]
        try:
            rows, _ = query_db_rows(api, db_id)
        except ApiError as e:
            print(f"[skip] {dirname}: {e}")
            continue
        by_id = {undash(r["id"]): r for r in rows}
        md_idx = index_row_mds(dirpath)
        db_mis = 0
        vreport = {"dbs": {"errors": []}}
        for rid, old in old_rows.items():
            page = by_id.get(rid)
            if page is None or (page.get("last_edited_time") or "9") >= cutoff:
                continue
            total_old += 1
            expand_truncated_props(api, page, dirname, vreport)
            new = [rid] + [cell((page.get("properties") or {}).get(c), users) for c in cols]
            if new != old:
                mismatch += 1
                db_mis += 1
                if db_mis <= args.validate_verbose:
                    for c, o, n in zip(["_row_id"] + cols, old, new):
                        if o != n:
                            print(f"  [{dirname[:40]}] {rid[:8]} col '{c}':\n    old={o[:160]!r}\n    new={n[:160]!r}")
        schema = jload(os.path.join(dirpath, "_schema.json"), {})
        title = schema.get("title") or ""
        md_mis = 0
        for rid, fname in list(md_idx.items())[:200]:
            page = by_id.get(rid)
            if page is None or (page.get("last_edited_time") or "9") >= cutoff:
                continue
            old_txt = open(os.path.join(dirpath, fname)).read()
            enrich = existing_enrichment(os.path.join(dirpath, fname)) or ""
            new_txt = render_row_md(page, db_id, title, cols, users, enrich)
            if new_txt != old_txt and md_mis < 3:
                md_mis += 1
                import difflib
                dl = list(difflib.unified_diff(old_txt.split("\n"), new_txt.split("\n"), lineterm=""))[:25]
                print(f"  [md] {dirname[:40]}/{fname[:50]}:")
                print("    " + "\n    ".join(dl))
        for err in vreport["dbs"]["errors"]:
            print(f"  [expand-error] {err['op']}: {err['error']}")
        print(f"[{dirname[:60]}] rows={len(old_rows)} old-row-mismatches={db_mis} md-mismatches={md_mis} (req={api.n})")
    print(f"\nTOTAL: {mismatch}/{total_old} unchanged-row mismatches; requests={api.n}, 429s={api.r429}")


def mask_stamps(text):
    """Retention annotations carry the date a comment was first missed, and a
    re-probe re-stamps them with today's — mask before comparing.

    The probe-failure annotation goes too: a re-probe that succeeds renders it
    away, so every row whose stored enrichment carries one would otherwise read
    as a mismatch whose whole diff is that line."""
    return RESOLVED_MARK.sub("", strip_probe_annotation(text) or "")


def refetch_candidates(args, want):
    """Enriched rows to re-probe: comment-bearing ones first (they exercise the
    merge, which is where the enrichment renderer actually earns its keep), then
    body-only ones to fill the sample. Stops scanning once `want` of the former
    are in hand."""
    commented, bodies = [], []
    for db_id, dirname in sorted(db_dirs().items(), key=lambda kv: kv[1].lower()):
        if args.dbs and args.dbs.lower() not in dirname.lower() and args.dbs.lower() not in db_id:
            continue
        dirpath = os.path.join(DBS, dirname)
        for rid, fname in sorted(index_row_mds(dirpath).items()):
            enrich = existing_enrichment(os.path.join(dirpath, fname))
            if not enrich or not enrich.strip():
                continue
            (commented if has_comments(enrich) else bodies).append(
                (dirpath, fname, rid, enrich))
        if len(commented) >= want:
            break
    return commented, bodies


def validate_refetch(api, users, args):
    """Re-probe a sample of enriched rows and diff probe_row's returned string
    against the enrichment on disk.

    The property-table pass above never touches Walker.render or the comment
    merge; this one does, end to end against live Notion. Read-only on the
    mirror except that an attachment which is not already on disk downloads, as
    it would on any probe."""
    import difflib
    report = new_report("validate")
    commented, bodies = refetch_candidates(args, args.refetch_sample)
    sample = (commented + bodies)[:args.refetch_sample]
    n_commented = min(len(commented), len(sample))
    if not sample:
        print("no enriched rows matched --dbs; nothing to re-probe")
        return
    print(f"re-probing {len(sample)} rows ({n_commented} comment-bearing)")
    mismatch = checked = 0
    for dirpath, fname, rid, disk in sample:
        n0 = api.n
        try:
            fresh = probe_row(api, users, rid, dirpath, report,
                              old_comments_body=extract_comments_body(disk))
        except ApiError as e:
            print(f"[skip] {fname[:60]}: {e}")
            continue
        checked += 1
        ok = mask_stamps(fresh) == mask_stamps(disk)
        mismatch += not ok
        print(f"[{'ok      ' if ok else 'MISMATCH'}] {os.path.basename(dirpath)[:34]}/"
              f"{fname[:44]} req={api.n - n0}")
        if not ok:
            dl = list(difflib.unified_diff(mask_stamps(disk).split("\n"),
                                           mask_stamps(fresh).split("\n"),
                                           "on-disk", "re-probed", lineterm=""))
            print("    " + "\n    ".join(dl[:args.validate_verbose + 3]))
    print(f"\nTOTAL: {mismatch}/{checked} enrichment mismatches "
          f"({n_commented} comment-bearing in sample); requests={api.n}, 429s={api.r429}")


# --------------------------------------------------------------- mirror lock

# The mirror has one writer at a time. That used to be enforced only in
# `refresh.sh`, so invoking this engine directly silently forfeited it: on
# 2026-08-13 a manual re-pull of one table and the hourly rows tick rendered the same
# CSV concurrently, and the tick committed 57 of 8,682 rows over the good state.
# The wrapper still takes the lock — it also commits, and the commit has to be
# inside the same critical section — so this has to be re-entrant *under the
# wrapper* without being re-entrant in general.
#
# The handshake is the wrapper's own lock fd, inherited: `refresh.sh` exports
# NOTION_MIRROR_LOCK_FD=9 beside its `flock -n 9`, and the claim is believed only
# when that fd really is open on the lock file (same st_dev/st_ino). Everything
# else — cron, a person, another script — takes the lock here or does not write.
LOCK_ENV_FD = "NOTION_MIRROR_LOCK_FD"
# Modes that write the mirror. `validate` and `contamination-check` read it (a
# refetch validate can download an attachment it finds missing, which is a fill,
# not a rewrite), and a `--dry-run` of any mode guards every write, so neither
# takes the lock — taking it would block a nightly for the length of a read.
WRITE_MODES = ("daily", "full-comments", "place", "rows", "legacy-comments", "comment-dedup",
               "row-page-dedup")


class MirrorLocked(Exception):
    """Another writer holds the lock; this process must not write the mirror."""


def lock_path():
    """The file `refresh.sh` self-locks. Resolved per call rather than at import
    so a test or a sandbox rehearsal can redirect it (HOME, or NOTION_MIRROR_LOCK
    where redirecting HOME is not on offer) instead of contending for the real
    one and refusing a live refresh."""
    return (os.environ.get("NOTION_MIRROR_LOCK")
            or os.path.expanduser("~/.locks/notion-mirror-internal"))


def _lock_inherited(path):
    """True when an ancestor holds the lock and handed down its fd.

    The env var on its own would be an unchecked claim, and a stale one in some
    unrelated environment would buy write access to the mirror; `fstat` on the
    named fd makes the claim checkable against the lock file itself."""
    fd = os.environ.get(LOCK_ENV_FD, "")
    if not fd.isdigit():
        return False
    try:
        mine, theirs = os.fstat(int(fd)), os.stat(path)
    except OSError:
        return False
    return (mine.st_dev, mine.st_ino) == (theirs.st_dev, theirs.st_ino)


def take_lock(path=None):
    """Hold the mirror lock for the life of the process, or refuse to write.

    Returns the fd it holds the lock on, or None when the lock is already held by
    an ancestor that handed us its fd. Raises `MirrorLocked` when anyone else
    holds it.

    An os-level fd rather than a file object (which is what `coverage_backfill`
    uses): a flock is released the moment its file object is garbage-collected,
    so a file object would have to stay referenced for the whole run and the
    protection would rest on nobody ever tidying that reference away. A bare fd
    survives until the process exits whether Python still remembers it or not."""
    import fcntl  # noqa: PLC0415 — deliberately lazy: Linux-only, and importing
    # it at module scope would make this module unimportable on Windows, where
    # a downstream fork runs the flattener half of it.
    path = path or lock_path()
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        # Held. If it is our own wrapper's lock, we are inside its critical
        # section and may proceed; if not, someone else is mid-write.
        if _lock_inherited(path):
            return None
        raise MirrorLocked(
            f"another mirror writer holds {path} — refusing to write the mirror "
            f"concurrently (two writers on one CSV is what committed a "
            f"99%-truncated table on 2026-08-13). Wait for it to finish, "
            f"or run through refresh.sh, which takes the same lock.")
    return fd


# ---------------------------------------------------------------- entrypoint

def report_stem(mode, dry_run):
    """Which last-run-report pair this run owns.

    A rows run writes its own: refresh.sh builds the nightly's commit summary
    from last-run-report.json and cats last-run-report.md into the stub note,
    and `reanalyze` reads the same pair — so an hourly job clobbering them loses
    the nightly's machine report. A dry run likewise must not overwrite the
    record of the last real one."""
    return ("last-run-report"
            + (".rows" if mode == "rows" else "")
            + (".dry-run" if dry_run else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=["daily", "weekly", "monthly", "full-comments", "place",
                                       "validate", "rows", "contamination-check",
                                       "legacy-comments", "comment-dedup", "row-page-dedup"],
                    default="daily",
                    help="daily = everything incremental incl. the webhook fold and the "
                         "rolling per-block comment audit; "
                         "full-comments = manual whole-human-corpus comment sweep (~55k req); "
                         "place = only retry _unplaced placement + regenerate structure.md; "
                         "rows = refresh only the rows named by --rows (plus a props-probe "
                         "drain), touching nothing else; "
                         "legacy-comments = rescan, once, every page and row still holding "
                         "an open comment with no id, so the resolution check can reach it; "
                         "comment-dedup = once, no requests: one bullet per comment, row "
                         "comments in row files; "
                         "row-page-dedup = once, no requests: remove the page files of "
                         "database rows whose row file is probed; "
                         "weekly/monthly are legacy aliases (daily / full-comments)")
    ap.add_argument("--rows", default="", help="--mode rows: page ids, comma- or space-separated")
    ap.add_argument("--rps", type=float, default=3.0)  # Notion's ~3 req/s per-token cap
    ap.add_argument("--dbs", default="", help="only DBs whose dir name/id contains this")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-dbs", action="store_true", help="skip the DB row sweep")
    ap.add_argument("--skip-content", action="store_true", help="skip the content-page pass")
    ap.add_argument("--write-baseline", action="store_true",
                    help="contamination-check: accept the current breaches as pre-existing")
    ap.add_argument("--validate-cutoff", default="2026-07-08T00:00:00.000Z")
    ap.add_argument("--validate-verbose", type=int, default=8, help="mismatched cells to print per DB")
    ap.add_argument("--refetch", action="store_true",
                    help="validate mode: re-probe enriched rows and diff the body/comment "
                         "renderer against disk, instead of the property-table pass")
    ap.add_argument("--refetch-sample", type=int, default=20, help="rows to re-probe with --refetch")
    args = ap.parse_args()
    if args.mode == "weekly":
        args.mode = "daily"
    elif args.mode == "monthly":
        args.mode = "full-comments"

    if args.mode == "contamination-check":
        return contamination_cli(args.write_baseline)  # offline: reads the mirror, no token

    row_ids = []
    if args.mode == "rows":
        try:
            # An omitted --rows is a props-probe drain with no rows named, which
            # is what most hourly ticks are: task rows change a few times a day
            # while `properties_updated` events arrive continuously. It still
            # runs (rather than being skipped by the caller) because the run is
            # also what tests the mirror lock — a wedged nightly has to keep
            # showing up as a refused tick. Cost with an empty queue: 0 requests.
            row_ids = parse_row_ids(args.rows) if args.rows.strip() else []
        except ValueError as e:
            print(f"--mode rows: {e}", file=sys.stderr)
            return 2

    token = os.environ.get("NOTION_TOKEN")
    if not token:
        print("NOTION_TOKEN not set", file=sys.stderr)
        return 2
    # Before the first request, and before anything is written: mutual exclusion
    # is this engine's own business now, not only the wrapper's. The lock is held
    # on an fd for the life of the process (see take_lock), so there is nothing to
    # keep referenced here. Exit 3 distinguishes contention from a usage error (2)
    # for anything reading the status.
    global DRY_RUN
    DRY_RUN = args.dry_run
    if args.mode in WRITE_MODES and not args.dry_run:
        try:
            take_lock()
        except MirrorLocked as e:
            print(f"refresh.py: {e}", file=sys.stderr)
            return 3
    api = Api(token, args.rps, version=VER_LATEST)
    users = Users(api)
    t0 = time.time()

    if args.mode == "validate":
        try:
            (validate_refetch if args.refetch else validate)(api, users, args)
        finally:
            users.save()
        return 0

    os.makedirs(STATE, exist_ok=True)
    dbflags = jload(os.path.join(STATE, "db-flags.json"), {})
    state = {
        "rows": {},  # loaded lazily below
        "db404": dbflags.get("db404", {}),
        "not_a_db": dbflags.get("not_a_db", {}),
        # written by coverage_backfill.py: databases whose data sources are not
        # shared with the integration. Same {id32: iso-date} shape as not_a_db.
        "unshared": dbflags.get("unshared", {}),
        "probe_policy": dbflags.get("probe_policy", {}),
        "content_since": jload(os.path.join(STATE, paths.LAST_RUN), {}).get("content_since"),
        "comment_scans": jload(os.path.join(STATE, "comment-scan.json"), {}),
        "retry_pages": jload(os.path.join(STATE, "retry-pages.json"), {}),
        "page_walks": jload(os.path.join(STATE, "page-walks.json"), {}),
        "comment_parents": jload(os.path.join(STATE, "comment-parents.json"), {}),
        "comment_rows": jload(os.path.join(STATE, "comment-rows.json"), {}),
    }
    rows_state_path = os.path.join(STATE, "rows-last-edited.json")
    state["rows"] = jload(rows_state_path, {})
    report = new_report(args.mode)
    discovered = set()
    breaches = []
    unchecked = None   # the contamination scan itself failed; see the finally
    pending_disc = os.path.join(STATE, "pending-discovery.json")
    if args.mode != "rows":
        # databases a rows run noticed but had no discovery phase to capture
        discovered.update(t for t in jload(pending_disc, []) if isinstance(t, str))

    def run_phase(name, fn, *a, **kw):
        """Run a phase, recording its API-request cost. Without this the only way
        to see where a 15,000-request run went is to reconstruct it from log
        timestamps."""
        n0, e0 = api.n, collections.Counter(api.by_endpoint)
        try:
            return fn(*a, **kw)
        finally:
            report["phases"][name] = report["phases"].get(name, 0) + (api.n - n0)
            per = report["phases_by_endpoint"].setdefault(name, {})
            for k, v in (api.by_endpoint - e0).items():
                per[k] = per.get(k, 0) + v

    try:
        if args.mode == "place":
            meta, _order = load_meta_jsonl()
            place_unplaced_pass(api, users, meta, index_workspace_pages(),
                                row_md_global_index(), state, report, args)
        elif args.mode == "rows":
            run_phase("rows", phase_rows, api, users, state, report, args, row_ids, discovered)
        elif args.mode == "legacy-comments":
            run_phase("audit-pages", phase_comment_audit_pages, api, users, load_meta_jsonl()[0],
                      state, report, args, only_legacy=True)
            run_phase("audit-rows", phase_comment_audit_rows, api, users, state, report, args,
                      discovered, only_legacy=True)
        elif args.mode == "row-page-dedup":
            run_phase("row-page-dedup", phase_row_page_dedup, report, args)
        elif args.mode == "comment-dedup":
            run_phase("comment-dedup", phase_comment_dedup, api, state, report, args,
                      {i: m.get("title") or "" for i, m in load_meta_jsonl()[0].items()})
        else:
            check_webhook_liveness(report)
            consume_db_events(state, report, discovered)
            run_phase("fold", fold_captures, users, state, report, args)
            run_phase("catalog", load_source_catalog, api, report)
            if not args.skip_dbs:
                run_phase("dbs", phase_dbs, api, users, state, report, args, discovered)
            # After the sweep, not before: entries queued before it are already
            # covered (the per-DB query re-renders every property table), while
            # entries that arrived during a multi-hour sweep are not. The nightly
            # drains this queue at all so that a broken hourly job leaves a
            # bounded file rather than an unbounded one.
            run_phase("props", drain_props_probe, api, users, state, report, args, discovered)
            meta = None
            if not args.skip_content:
                meta = run_phase("content", phase_content, api, users, state, report, args,
                                 args.mode, discovered)
            run_phase("schema", phase_schema_sweep, api, users, state, report, args)
            run_phase("discovery", phase_discovery, api, users, state, report, args, discovered)
            if meta is not None:
                if args.mode == "full-comments":
                    run_phase("full-comments", phase_full_comment_sweep, api, users, meta, state, report, args)
                else:
                    run_phase("resolve", phase_resolution_check, api, users, state, report, args)
                    run_phase("audit-pages", phase_comment_audit_pages, api, users, meta, state,
                              report, args)
            if args.mode != "full-comments" and not args.skip_dbs:
                run_phase("audit-rows", phase_comment_audit_rows, api, users, state, report,
                          args, discovered)
            # The coverage assert, last: it scans the corpus the run actually
            # leaves behind, inside refresh.py so the changelog analysis reads
            # its output from the staged diff rather than after the commit. It
            # costs no API requests (~15s of local file reading) and it reports
            # rather than failing — an inline database appearing overnight is
            # information, not a reason to abort a mirror refresh.
            run_coverage_assert(report, state, write_floor=not args.dry_run)
        if not args.dry_run and (report["pages"]["new"] or report["pages"]["deleted"]
                                 or report["pages"]["renamed"] or report["pages"]["placed"]):
            if regenerate_structure_md():
                report["notes"].append("structure.md regenerated (tree changed)")
    finally:
        report["requests"] = api.n
        report["requests_by_endpoint"] = dict(api.by_endpoint.most_common())
        report["rate429"] = api.r429
        report["rate_limit_reasons"] = dict(getattr(api, "limit_reasons", {}))
        report["duration_s"] = int(time.time() - t0)
        if not args.dry_run:
            users.save()
        if args.mode == "rows":
            # A rows run persists only what it actually changed: the rows it
            # probed may have joined the audit pool or had their comments read.
            if not args.dry_run:
                jsave(os.path.join(STATE, "comment-rows.json"), state["comment_rows"])
                jsave(os.path.join(STATE, "comment-scan.json"), state["comment_scans"])
        elif not args.dry_run:
            known = set(db_dirs())
            left = sorted(t for t in discovered
                          if t.startswith("db:") and not skip_discovery(t[3:], known, state))
            if left or os.path.exists(pending_disc):
                jsave(pending_disc, left)
            jsave(rows_state_path, state["rows"])
            jsave(os.path.join(STATE, "comment-scan.json"), state["comment_scans"])
            jsave(os.path.join(STATE, "retry-pages.json"), state["retry_pages"])
            jsave(os.path.join(STATE, "page-walks.json"), state["page_walks"])
            jsave(os.path.join(STATE, "comment-parents.json"), state["comment_parents"])
            jsave(os.path.join(STATE, "comment-rows.json"), state["comment_rows"])
            jsave(os.path.join(STATE, "db-flags.json"),
                  {"db404": state["db404"], "not_a_db": state["not_a_db"],
                   "unshared": state["unshared"], "probe_policy": state["probe_policy"]})
            jsave(os.path.join(STATE, paths.LAST_RUN),
                  {"content_since": state["content_since"], "ts": report["ts"], "mode": args.mode})
        # In the finally on purpose: a run that failed partway still wrote
        # comments, so it still has to be checked. LAST in the finally, and
        # wrapped, because it is the only thing here that reads the whole corpus:
        # `build_comment_index` guards its reads with `except OSError` alone, so
        # one non-UTF-8 byte (UnicodeDecodeError) or a directory that vanished
        # mid-run (FileNotFoundError) raises. Raised from the top of this block,
        # that discarded every state write above it and the run report with them.
        # `rows` is in the list because it writes comments: phase_rows probes each
        # named row, rendering its comment bullets into the row file, and the
        # hourly job commits them 24 times a day. Cost of including it, measured
        # against the live corpus: 6.55s, no API requests.
        if args.mode in ("daily", "full-comments", "rows", "legacy-comments", "comment-dedup"):
            try:
                breaches = record_contamination_check(build_comment_index(),
                                                      load_contamination_baseline(), report)
            except Exception as e:  # noqa: BLE001 — an assert that could not run is not a pass
                unchecked = f"{type(e).__name__}: {e}"[:200]
                report["comments"]["contamination_breaches"] = None
                report["notes"].append(
                    f"FAILED: comment contamination assert could not run: {unchecked}")
        stem = report_stem(args.mode, args.dry_run)
        jsave(os.path.join(STATE, f"{stem}.json"), report)
        with open(os.path.join(STATE, f"{stem}.md"), "w") as f:
            f.write(report_md(report))
        log(f"done: {api.n} requests, {api.r429} 429s, {report['duration_s']}s")
    if breaches:
        # Non-zero stops refresh.sh before `git add`, so the suspect write stays
        # in the working tree, uncommitted, and ntfys instead of landing quietly.
        print(contamination_message(breaches), file=sys.stderr)
        return 1
    if unchecked:
        print(f"comment contamination assert could not run: {unchecked}", file=sys.stderr)
        return 1
    has_changes = bool(report["dbs"]["changed"] or report["dbs"]["new"] or report["dbs"]["deleted"]
                       or report["pages"]["changed"] or report["pages"]["new"] or report["pages"]["deleted"]
                       or report["pages"]["renamed"] or report["pages"]["placed"]
                       or report["comments"]["added"] or report["comments"]["retained"]
                       or (report["comments"].get("dedup") or {}).get("rows_changed")
                       or (report["comments"].get("dedup") or {}).get("sections_changed")
                       or (report["comments"].get("dedup") or {}).get("sections_moved")
                       or (report.get("row_page_dedup") or {}).get("removed")
                       or report.get("rows", {}).get("refreshed")
                       or report.get("props_probe", {}).get("drained"))
    print(json.dumps({"changes": has_changes, "requests": api.n}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
