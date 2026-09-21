#!/usr/bin/env python3
"""Incremental full-fidelity refresh of the notion/ mirror.

Keeps workspace/ (pages, _databases/, _comments.md), _meta/ (pages-metadata.jsonl,
content-pages.tsv) current against the live Notion workspace, touching only
what changed so git diffs stay meaningful and API usage stays polite.

Design (2026-07-16):
  * DB rows: full per-DB /query sweep every run (~800 req for 74k rows). /v1/search
    provably misses whole DBs (64 of 407 at build time), so per-DB queries are the
    only reliable change/deletion source. CSVs re-render preserving existing column
    and row order (new rows appended by created_time) -> minimal diffs. A per-row
    last_edited_time state file triggers body+comment re-probes for changed rows.
  * Content pages: /v1/search desc early-stop daily (a few requests); weekly full
    sweep rebuilds pages-metadata.jsonl + content-pages.tsv and detects deletions
    (verified via GET /pages before deleting anything locally). Changed pages are
    re-walked notion_walk-style with comment harvest + attachment download in the
    same pass.
  * Comments: re-scanned same-day for anything re-walked, PLUS a budgeted rolling
    per-block scan every run (comments do NOT bump last_edited_time — verified
    empirically — and the API has no global comments listing, so freshness costs
    one request per block). Human content pages cycle ~weekly at the default
    budget; machine-generated transcript subtrees (95% of block volume) cycle
    slowly on 10% of the budget. _comments.md is a faithful snapshot of
    unresolved comments; git history preserves resolved ones.
  * Throttle ~2 req/s (shared 3 req/s integration cap), Retry-After honored,
    per-run request budget with a persisted overflow queue for row probes.

Formats replicate the Jul-08..10 build exactly (see README.md):
  _databases/<Title> <dbid32>/{_schema.json,_schema.md,<Title> <dbid32>.csv,
  <RowTitle> <rowid32>.md}; row .md = header comment + props table (non-empty,
  schema order) + '<!-- body+comments fetched -->' + optional ## Body / ## Comments.

Env: NOTION_TOKEN required. Stdlib only, plus the sibling `coverage_census`
module for the coverage assert.
"""
import argparse
import collections
import csv
import datetime as dt
import hashlib
import io
import json
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
from notion_core.api import API, VER, VER_DS, Api, ApiError, Budget, Truncated  # noqa: F401
from notion_core.flatten import (PAGINATED_PROP_TYPES, cell, expand_truncated_props,  # noqa: F401
                                 fmt_date, fmt_num, md_cell)
from notion_core.richtext import absorbs_a_paragraph, md_link, plain, rich_md  # noqa: F401
from notion_core.rowmd import (BODY_CLOSE, BODY_OPEN, CID_LEGACY, CID_MARK,  # noqa: F401
                               COMMENTS_CLOSE, COMMENTS_OPEN, MARKER, RESOLVED_MARK,
                               annotate_resolved, bullet_cid, bullet_text_key, cid_trailer,
                               extract_comments_body, split_bullets, stamp_cid,
                               strip_comments_section)
from notion_core.runcfg import MODE_BUDGETS, parse_row_ids  # noqa: F401
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
            except Budget:
                return uid
            except ApiError:
                # cache the fallback: deleted users 404 forever — never re-fetch
                self.map[uid] = uid
        return self.map[uid]

    def save(self):
        jsave(self.path, self.map)


# ---------------------------------------------------------------- attachments

def download_attachments(walker, lines, dest_dir, prefix_for, report):
    """Resolve ATTACH: placeholders -> relative filenames, downloading new files."""
    if not walker.attachments:
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

    Pure — no API calls, so it cannot raise Budget or ApiError. Every request
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
    """The `## Comments` region as `parts` entries (see probe_row)."""
    return ["", "## Comments", "", COMMENTS_OPEN, *bullets, COMMENTS_CLOSE]


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
              max_blocks=800, block_comment_cap=25, discovered=None):
    """Fetch body + comments for a DB row -> enrichment string ('' if none).
    Per-block comment scans are capped: big bodies (meeting transcripts) would
    cost one request per block; page-level comments are always checked. Old
    comments never vanish: capped scans keep them verbatim, full scans keep
    resolved/deleted ones annotated."""
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
    page_comments = w.comments_for(page_id)
    block_comments, capped = w.harvest_comments(cap=block_comment_cap)
    if capped:
        report["comments"]["row_block_scans_capped"] += 1
    parts = []
    body = "\n".join(body_lines).strip("\n")
    if body.strip():
        parts += body_section_lines(body)
    flat = []
    for _anchor, cs in ([("(page)", page_comments)] if page_comments else []) + block_comments:
        for c in cs:
            t = c.text.replace("\r", "").replace("\n", "\n  ")
            flat.append(stamp_cid(f"- _{c.who} ({c.when[:10]}):_ {t}", c.cid, c.did))
    # a comment reachable both page-level and through its anchor block is one
    # comment: with ids that is now decidable, and the invariant this whole
    # change buys is that no two surviving bullets on a row share an id
    flat = dedup_by_cid(flat)
    if capped:
        # block scan skipped: page-level comments are fresh, block-anchored ones
        # can't be compared — carry the old record over verbatim
        ids, texts = live_index(flat)
        flat += [b for b in split_bullets(old_comments_body, prefix="- _")
                 if not still_live(b, ids, texts)]
    else:
        flat = union_captured(flat, undash(page_id), users, row_format=True)
        today = dt.datetime.now(UTC).strftime("%Y-%m-%d")
        flat, kept = merge_comment_bullets(old_comments_body, flat, today, prefix="- _")
        report["comments"]["retained"] += kept
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
        return "", capped
    return "\n\n" + "\n".join(parts).strip("\n") + "\n", capped


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


def query_db_rows(api, db_id):
    """All rows of a DB. 2022 endpoint first; multi-source DBs via 2025-09-03.

    Complete past Notion's 10,000-results-per-query cap: `Api.query_rows` windows
    by created_time, so a big DB's overflow rows can no longer be mistaken for
    deletions (which is how ~630 live rows of a signups feed got tombstoned in
    Aug 2026). An unwindowable truncation raises Truncated instead of returning
    a short set."""
    try:
        return api.query_rows(f"/databases/{dashed(db_id)}/query"), None
    except ApiError as e:
        if e.code in (400,) and "data source" in e.body.lower() or e.code == 400 and "multiple" in e.body.lower():
            d = api.get(f"/databases/{dashed(db_id)}", ver=VER_DS)
            rows = []
            srcs = d.get("data_sources") or []
            for s in srcs:
                rows.extend(api.query_rows(f"/data_sources/{s['id']}/query", ver=VER_DS))
            return rows, {"data_sources": srcs, "database": d}
        raise


def schema_md(title, db_id, nrows, props):
    lines = [f"# Schema — {title}", "",
             f"`db {db_id}` · {nrows} rows · {len(props)} properties", "",
             "| Property | Type | Detail |", "|---|---|---|"]
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


def refresh_schema_files(api, dirpath, db_id, title, nrows, report, force=False):
    """GET schema; rewrite _schema.json/_schema.md if content changed.
    Returns (props, live_title)."""
    spath = os.path.join(dirpath, "_schema.json")
    old = jload(spath, {})
    try:
        d = api.get(f"/databases/{dashed(db_id)}")
    except ApiError as e:
        report["dbs"]["errors"].append({"db": title, "op": "schema", "error": str(e)[:200]})
        return (old.get("database") or {}).get("properties") or {}, title
    d.pop("request_id", None)
    live_title = plain(d.get("title")) or title
    new = {"id": db_id, "title": live_title, "database": d,
           "data_sources": old.get("data_sources") or d.get("data_sources") or []}
    oldn = dict(old.get("database") or {})
    oldn.pop("request_id", None)
    if force or oldn != d or old.get("title") != live_title:
        jsave(spath, new)
        props = d.get("properties") or {}
        with open(os.path.join(dirpath, "_schema.md"), "w") as f:
            f.write(schema_md(live_title, db_id, nrows, props))
        if oldn.get("properties") != d.get("properties"):
            report["dbs"]["schema_changed"].append(live_title)
        update_all_schemas(live_title, db_id, nrows, props)
    return d.get("properties") or {}, live_title


def update_all_schemas(title, db_id, nrows, props):
    path = os.path.join(DBS, "_ALL-SCHEMAS.md")
    txt = open(path).read() if os.path.exists(path) else "# All database schemas\n\n"
    body = schema_md(title, db_id, nrows, props).split("\n", 2)[2]  # drop '# Schema —' header
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
    except Budget:
        raise
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
        # property removed from schema? verify before dropping
        props, title = refresh_schema_files(api, dirpath, db_id, title, len(rows), report)
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
        if args.dry_run:
            continue
        if old is None or line_changed or le_changed:
            need_probe = (old is None) or le_changed
            if need_probe and probe_mode == "enriched":
                old_md = md_idx.get(rid)
                enr = existing_enrichment(os.path.join(dirpath, old_md)) if old_md else None
                if not (enr and enr.strip()):
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
                  md_idx=None, discovered=None):
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
        old_cb = extract_comments_body(stored)
        # read before probing: a capped scan returns FRESH enrichment, so the
        # previous annotation survives only on disk
        first_missed = probe_annotation(stored)
        try:
            enrichment, capped = probe_row(api, users, page["id"], dirpath, report,
                                           old_comments_body=old_cb, discovered=discovered)
            # a capped scan is a partial read: the body is fresh but block-anchored
            # comments were carried over unverified, so the row is not current
            stale = "block-comment scan capped" if capped else None
            fully_read = not capped
        except Budget:
            state["queue"].append({"kind": "row_probe", "db": db_id, "row": rid})
            enrichment = None
            # deliberately unannotated: the queue entry already records the row as
            # owed a probe, and the budget wall stops every row behind it at once —
            # marking them would rewrite thousands of files and unwrite them next run
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
        # today's churned the tree: `capped` is a function of body size, so a
        # big-bodied row caps on every probe forever and the byte-difference write
        # gate rewrote it nightly with the date as its entire diff. Kept
        # regardless of WHY the row is stale, since the annotation records no
        # reason to compare against: a row capped in August that starts failing
        # outright in September keeps the August date. That reads staler than it
        # is, the safe direction, and the run's own reason is in the report.
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
        d = api.get(f"/databases/{dashed(db_id)}")
    except ApiError as e:
        # A 4xx is a verdict about the id — not a database, deleted, or not
        # shared — so record it and stop asking. Anything else is a verdict
        # about the moment: this flag is permanent and `phase_discovery` skips
        # on it, so flagging a 5xx retires a live database from the mirror for
        # good, on one bad night. Unflagged, the id stays in pending-discovery
        # and the next run retries it. It must not propagate either:
        # phase_discovery catches only Budget, so an escaping ApiError would
        # unwind past main's try and lose the night's uncommitted work.
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
    if args.dry_run:
        report["dbs"]["new"].append({"title": title, "id": db_id, "rows": "?", "dry_run": True})
        return
    os.makedirs(dirpath, exist_ok=True)
    jsave(os.path.join(dirpath, "_schema.json"),
          {"id": db_id, "title": title, "database": d, "data_sources": d.get("data_sources") or []})
    try:
        rows, _ = query_db_rows(api, db_id)
    except (ApiError, Budget) as e:
        rows = []
        report["dbs"]["errors"].append({"db": title, "op": "new-capture", "error": str(e)[:200]})
    props = d.get("properties") or {}
    with open(os.path.join(dirpath, "_schema.md"), "w") as f:
        f.write(schema_md(title, db_id, len(rows), props))
    update_all_schemas(title, db_id, len(rows), props)
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
    ic = r.get("icon")
    icon = (ic.get("emoji") if ic.get("type") == "emoji" else ic.get("type", "")) if ic else ""
    return {"id": undash(r["id"]), "title": page_title_of(r), "parent_type": pt,
            "parent_id": undash(par.get(pt)) if isinstance(par.get(pt), str) else "",
            "created_time": r.get("created_time"), "last_edited_time": r.get("last_edited_time"),
            "created_by": users.name(r.get("created_by")), "last_edited_by": users.name(r.get("last_edited_by")),
            "archived": r.get("archived", False), "in_trash": r.get("in_trash", False),
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
        except (ApiError, Budget):
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
        os.makedirs(d, exist_ok=True)
        return d
    hit = row_index.get(pid)
    if hit:
        dirname, fname = hit
        d = os.path.join(DBS, dirname, fname[:-3])
        os.makedirs(d, exist_ok=True)
        return d
    pm = meta.get(pid)
    if pm is None:
        try:
            pm = meta_of(api.get(f"/pages/{dashed(pid)}"), users)
        except (ApiError, Budget):
            return None
    return resolve_dest_dir(pm, meta, page_index, row_index, api, users, depth)


def place_unplaced_pass(api, users, meta, page_index, row_index, state, report, args, max_api=400):
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
        if api.n - start >= max_api:
            report["notes"].append("placement pass paused (per-run API cap); remainder next run")
            break
        pid = mm.group(1)
        m = meta.get(pid)
        if m is None:
            try:
                m = meta_of(api.get(f"/pages/{dashed(pid)}"), users)
                meta[pid] = m
            except Budget:
                break
            except ApiError:
                stuck += 1
                continue
        try:
            dest = resolve_dest_dir(m, meta, page_index, row_index, api, users)
        except Budget:
            break
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
    # per-page request cap: auto-generated logs/transcripts (the automation
    # subtrees: delta logs, recordings) are large, deeply nested, change daily, and carry no
    # human signal — one such page could otherwise burn thousands of requests and
    # starve the rest of the run. Cap them hard; give human pages generous room.
    cap = int(os.environ.get("NOTION_REFRESH_AUTOMATION_WALK_CAP", "40")) \
        if _is_automation(pid, page_index) else int(os.environ.get("NOTION_REFRESH_PAGE_WALK_CAP", "400"))
    w = Walker(api, users, max_blocks=6000, max_requests=cap)
    lines = [f"# {title}", "",
             f"<!-- notion page id: {pid} | parent: "
             f"{json.dumps({'type': page_meta.get('parent_type'), 'id': page_meta.get('parent_id')})} -->", ""]
    w.walk(dashed(pid), lines, 0)

    old_path = page_index.get(pid)
    if old_path:
        dest_dir = os.path.dirname(old_path)
    else:
        dest_dir = None
        try:
            dest_dir = resolve_dest_dir(page_meta, meta or {}, page_index,
                                        row_index if row_index is not None else {}, api, users)
        except Budget:
            raise
        if not dest_dir:
            dest_dir = os.path.join(WS, "_unplaced")
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
    live and dropped in its favour, and no false resolved annotation appears."""
    ids, texts = live_index(new_bullets)
    retained = []
    newly = 0
    for b in split_bullets(old_body, prefix):
        if still_live(b, ids, texts):
            continue  # still live (or reappeared): the fresh copy wins, unannotated
        if RESOLVED_MARK.search(b):
            retained.append(b)  # annotated on an earlier run, keep as-is
        else:
            retained.append(annotate_resolved(b, today))
            newly += 1
    return list(new_bullets) + retained, newly


# ------------------------------------------------- webhook capture integration

_WEBHOOK_CAPTURES = None


def webhook_captures():
    """page_id32 -> [(anchor, comment dict)] from the receiver's capture log
    (see webhook_receiver.py). Loaded once per run; deduped by comment id."""
    global _WEBHOOK_CAPTURES
    if _WEBHOOK_CAPTURES is not None:
        return _WEBHOOK_CAPTURES
    out = {}
    seen = set()
    path = os.path.join(STATE, paths.CAPTURE)
    if os.path.exists(path):
        for ln in open(path):
            try:
                e = json.loads(ln)
            except json.JSONDecodeError:
                continue
            for c in e.get("comments", []):
                key = (e.get("page_id"), c.get("id"))
                if key in seen:
                    continue
                seen.add(key)
                out.setdefault(e.get("page_id", ""), []).append((e.get("anchor", "(page-level)"), c))
    _WEBHOOK_CAPTURES = out
    return out


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
        who = users.name({"id": c.get("author_id", "")}) if c.get("author_id") else "?"
        when = (c.get("created_time") or "")[:10]
        text = captured_text(c)
        if row_format:
            t = text.replace("\r", "").replace("\n", "\n  ")
            b = f"- _{who} ({when}):_ {t}"
        else:
            b = f'- **on** "{anchor}" — {who} ({when}): {text}'
        b = stamp_cid(b, undash(c.get("id") or ""), undash(c.get("discussion_id") or ""))
        if still_live(b, ids, texts):
            continue
        out.append(annotate_resolved(b, today))
        # two captures of one comment (the log is deduped per page, the
        # pre-repair merge was not) must not become two bullets
        if bullet_cid(b):
            ids.add(bullet_cid(b))
        texts.add(bullet_text_key(b))
    return out


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
    """The nightly's entry point: assert, or say why not.

    Skipped after a budget wall on purpose. With phases cut short, a
    referenced-but-absent id means "the run stopped before capturing it", not
    "a new gap"; 35% of recent runs stop that way, and a finding list nobody
    can trust is worse than no list. The skip is stated rather than silent —
    silence would read as a clean assert, which is the failure mode the assert
    exists to rule out."""
    if report["budget_exhausted"]:
        report["notes"].append(
            "coverage assert skipped: request budget exhausted, so a referenced-but-absent "
            "id would say where the run stopped rather than what the mirror is missing")
        return None
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


def update_comments_md(updates, report):
    """updates: {pid: {"title":.., "bullets":[..]}} — replace/add sections.
    Append-only semantics: comments that vanish from the API are retained with a
    resolved/deleted annotation (the API cannot see resolved threads, so dropping
    them would erase history — they are kept by design)."""
    if not updates:
        return
    today = dt.datetime.now(UTC).strftime("%Y-%m-%d")
    head, sections, appendix = load_comments_md()
    if not head:
        head = "# Notion comments (content pages)\n\n_Inline reviewer comments captured per-block via `/v1/comments`. DB-row comments live in each row's `.md` under `workspace/_databases/`._\n\n"
    by_id = {s["id"]: s for s in sections}
    n_add = n_ret = 0
    for pid, u in updates.items():
        old = by_id.get(pid)
        merged, newly_retained = merge_comment_bullets(old["body"] if old else "", u["bullets"], today)
        n_ret += newly_retained
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
    report["comments"]["added"] += n_add
    report["comments"]["retained"] += n_ret
    total = sum(s["body"].count("- **on**") for s in sections)
    note = " (plus the truncation-backfill appendix)" if appendix else ""
    tail = (f"\n---\n_{total} comments across {len(sections)} pages{note}. Resolved/deleted threads are"
            f" kept with a `[resolved/deleted ≤date]` annotation (the API only returns open comments)."
            f" Refreshed incrementally; see _meta/changelog/._\n")
    out = head + "".join(f"## {s['title']}  `{s['id']}`\n{s['body']}" for s in sections).rstrip("\n") \
        + "\n" + (appendix if appendix else "") + tail
    with open(os.path.join(WS, "_comments.md"), "w") as f:
        f.write(out)


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
    return {"ts": now_iso(), "mode": mode, "requests": 0, "rate429": 0,
            "budget_exhausted": False, "duration_s": 0,
            "dbs": {"checked": 0, "changed": [], "new": [], "deleted": [], "schema_changed": [],
                    "errors": [], "probe_annotated": []},
            "pages": {"changed": [], "new": [], "deleted": [], "renamed": [], "placed": [], "errors": []},
            "comments": {"pages_rescanned": 0, "added": 0, "retained": 0,
                         "row_block_scans_capped": 0, "page_scans_capped": 0,
                         "contamination_breaches": 0},
            "attachments": {"downloaded": [], "failed": []},
            "deferred": {"row_probes_queued": 0},
            # Empty until the coverage assert runs, so "the assert did not run"
            # and "the assert found nothing" stay distinguishable in the report.
            "coverage": {},
            "phases": {},  # phase name -> API requests spent
            "notes": []}


def report_md(r):
    L = [f"# Notion mirror refresh — {r['ts']} ({r['mode']})", "",
         f"API requests: {r['requests']} (429s: {r['rate429']})"
         + (" — **request budget exhausted, run partial**" if r["budget_exhausted"] else ""),
         f"Duration: {r['duration_s']}s"]
    if r.get("phases"):
        L += ["", "By phase: " + " · ".join(f"{k} {v}" for k, v in r["phases"].items() if v)]
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
    capnotes = []
    if c.get("row_block_scans_capped"):
        capnotes.append(f"{c['row_block_scans_capped']} big row bodies")
    if c.get("page_scans_capped"):
        capnotes.append(f"{c['page_scans_capped']} automation pages")
    L += ["", f"## Comments — {c['pages_rescanned']} pages rescanned, +{c['added']} new, "
          f"{c['retained']} newly marked resolved/deleted (kept)"
          + (f" (per-block scan skipped: {', '.join(capnotes)})" if capnotes else "")]
    if c.get("contamination_breaches"):
        L.append(f"- **FAILED: {c['contamination_breaches']} comment text(s) attributed to more than "
                 f"{MAX_COMMENT_PAGES} pages** — see Notes")
    sh = c.get("shard")
    if sh:
        L.append(f"- rolling comment scan: {sh['human_scanned']}/{sh['human_total']} human pages "
                 f"+ {sh['auto_scanned']}/{sh['auto_total']} automation pages this run "
                 f"({sh['requests']} req; full human cycle ≈ {sh['human_cycle_days_est']} days)")
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
              f"{pp['deferred']} deferred, {pp['dropped']} dropped"]
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
    if r["deferred"]["row_probes_queued"]:
        L += ["", f"Deferred to next run: {r['deferred']['row_probes_queued']} row body/comment probes (budget)."]
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
    if not events:
        return
    jsave(path, {})  # consumed (sweeps are the backstop for any race)
    log(f"consumed {len(events)} webhook db event(s)")


def check_webhook_liveness(report):
    """A webhook feed cannot detect its own gaps: if the receiver dies, events just
    stop and nothing local says so — the mirror quietly loses its freshness path
    while every run still looks healthy. phase_comment_shard already restores the
    full blind-scan budget once the feed is 48h stale, but silently; warn first.
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
            f"The blind comment scan returns to full budget once the feed is 48h stale.")


def phase_queue(api, users, state, report, args, max_req=None, discovered=None):
    # webhook events naming DB rows -> probe those rows this run
    prio_path = os.path.join(STATE, "webhook-priority-pages.json")
    prio = set(jload(prio_path, []))
    if prio and state["rows"]:
        row_to_db = {rid: db for db, rows in state["rows"].items() for rid in rows}
        moved = 0
        for pid in list(prio):
            db = row_to_db.get(pid)
            if db:
                state["queue"].append({"kind": "row_probe", "db": db, "row": pid})
                prio.discard(pid)
                moved += 1
        if moved and not args.dry_run:
            jsave(prio_path, sorted(prio))
            log(f"webhook events -> {moved} row probe(s) queued")
    q = state["queue"]
    if not q:
        return
    log(f"draining probe queue: {len(q)} entries")
    dirs = db_dirs()
    start = api.n
    remaining, skipped = [], 0
    mds = {}  # dirpath -> {row id: filename}; per-row listdir is O(n^2) on big DBs
    for n, item in enumerate(q):
        if item.get("kind") != "row_probe":
            continue
        # the queue is opportunistic freshness work, the DB sweep is the correctness
        # backbone — cap the drain so a burst of events can't starve every later phase
        # (2,962 queued entries ate 70% of the 07-31 budget and the comment shard got
        # nothing).
        if max_req is not None and api.n - start >= max_req:
            remaining += [i for i in q[n:] if i.get("kind") == "row_probe"]
            report["notes"].append(f"probe queue capped at {max_req} req — "
                                   f"{len(remaining)} entries deferred to next run")
            break
        dbid, rid = item["db"], item["row"]
        dirname = dirs.get(dbid)
        if not dirname:
            continue
        dirpath = os.path.join(DBS, dirname)
        schema = jload(os.path.join(dirpath, "_schema.json"), {})
        idx = mds.get(dirpath)
        if idx is None:
            idx = mds[dirpath] = index_row_mds(dirpath)
        # same enriched-only rule the sweep applies (db_probe_policy): in big feed DBs
        # where <5% of rows carry a body/comments, probe only rows that already have
        # enrichment. Without this a webhook-named row costs a body+comment walk that
        # the sweep would have skipped as pointless — the two paths disagreed row for
        # row (one job feed: 1,573 queued vs 1,906 skipped on the same run).
        # ... except for a row the receiver has captured comments for. That
        # capture reaches the mirror through union_captured, which runs only
        # inside probe_row: the comment-audit pool seeds from rows where
        # has_comments already holds, and the full sweep is content-pages-only.
        # So on an unenriched row the probe is the capture's only door, and the
        # policy verdict is cached 7 days while the DB stays under the threshold
        # precisely because nothing is enriched — silent, and self-latching.
        probe = True
        if not webhook_captures().get(rid) and \
                db_probe_policy(dirpath, len(state["rows"].get(dbid, {})), state, dbid) == "enriched":
            old_md = idx.get(rid)
            enr = existing_enrichment(os.path.join(dirpath, old_md)) if old_md else None
            if not (enr and enr.strip()):
                probe = False
                skipped += 1
        try:
            page = api.get(f"/pages/{dashed(rid)}")
            expand_truncated_props(api, page, schema.get("title", ""), report)
            cols = [c for c in (read_csv(next((os.path.join(dirpath, f) for f in os.listdir(dirpath)
                                               if f.endswith('.csv') and ID32.search(f)), ""))[0] or [])[1:]]
            upsert_row_md(api, users, page, dbid, schema.get("title", ""), cols, dirpath, probe,
                          state, report, args, md_idx=idx, discovered=discovered)
        except Budget:
            # out of requests: keep this entry and every one behind it, and stop.
            # Looping on would re-raise per item and inflate the skip counter with
            # decisions about rows we never touched.
            if not probe:
                skipped -= 1
            remaining += [i for i in q[n:] if i.get("kind") == "row_probe"]
            break
        except ApiError:
            pass
    state["queue"] = remaining + [i for i in q if i.get("kind") != "row_probe"]
    if skipped:
        report["notes"].append(f"probe queue: {skipped} body/comment probe(s) skipped "
                               f"(enriched-only policy)")
    log(f"probe queue: {api.n - start} req, {skipped} probes skipped, {len(remaining)} deferred")


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


def drain_props_probe(api, users, state, report, args, max_req, discovered=None):
    """Drain `props-probe-queue.json`: one GET per row, property table + CSV line.

    Its own file, for a structural reason. refresh.py rewrites probe-queue.json
    wholesale from an in-memory copy at end of run, so anything the receiver
    appends there during a multi-hour nightly is erased. Entries in a separate
    file *cannot* be caught by that write — there is no lost-update window to
    reason about rather than a narrow one to guard.

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
    probe makes property freshness hourly; it is not the only path to it."""
    if args.dry_run:
        return
    stats = report.setdefault("props_probe",
                              {"drained": 0, "deferred": 0, "dropped": 0, "errors": []})
    qp, pp = props_queue_path(), props_processing_path()
    start = api.n
    dirs = db_dirs()
    mds = {}
    for _pass in range(2):  # the orphan, then the live queue
        if api.n - start >= max_req:
            return  # nothing to spend: don't even move the queue file
        if not os.path.exists(pp):
            if not os.path.exists(qp):
                return
            try:
                os.replace(qp, pp)
            except OSError:
                return
        batch = [e for e in jload(pp, []) if isinstance(e, dict) and e.get("row")]
        rest = []
        for n, item in enumerate(batch):
            if api.n - start >= max_req:
                rest = batch[n:]
                break
            rid = undash(item["row"])
            try:
                page = api.get(f"/pages/{dashed(rid)}")
            except Budget:
                report["budget_exhausted"] = True
                rest = batch[n:]
                break
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
            except Budget:
                report["budget_exhausted"] = True
                rest = batch[n:]
                break
            except ApiError as e:
                stats["dropped"] += 1
                stats["errors"].append({"row": rid, "error": str(e)[:160]})
                continue
            stats["drained"] += 1
        if rest:
            jsave(pp, rest)
            stats["deferred"] += len(rest)
            return
        try:
            os.remove(pp)
        except OSError:
            pass


def phase_rows(api, users, state, report, args, ids, discovered):
    """Refresh exactly the rows named on the command line, and nothing else.

    Deliberately not phase_queue. That folds webhook-priority-pages.json into
    the persisted queue, jsaves the shrunken file *before* draining, and then
    drains the whole persisted queue capped only by max_req — measured at design
    time as 120 queued entries plus 533 priority pages, i.e. up to 653 rows at
    3-8 requests each, on a tick budgeted at 10-30. It also skips
    consume_db_events, which clears webhook-db-events.json: a phase with no
    discovery of its own would eat the same-day capture signal for every newly
    created database and give nothing back.

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
        except Budget:
            report["budget_exhausted"] = True
            break
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
        qlen = len(state["queue"])
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
        except Budget:
            report["budget_exhausted"] = True
            break
        except ApiError as e:
            stats["errors"].append({"row": rid, "error": str(e)[:160]})
            continue
        if len(state["queue"]) > qlen:
            # upsert_row_md absorbs a mid-probe Budget by queueing the row, and a
            # rows run does not persist that queue — so the row is neither probed
            # nor owed to anyone. Counting it refreshed would leave a tick that
            # reads clean over a row whose body was never read, which is the
            # failure this mode exists to make visible.
            del state["queue"][qlen:]
            report["budget_exhausted"] = True
            stats["errors"].append({"row": rid, "error": "request budget hit mid-probe"})
            break
        stats["refreshed"].append(rid)
        enr = existing_enrichment(os.path.join(dirpath, idx.get(rid, ""))) or ""
        for did in CHILD_DB_RE.findall(enr):
            discovered.add("db:" + did)
    # Shares this tick's budget rather than holding an allowance of its own, so a
    # burst of property edits cannot turn an hourly run into an unbounded one.
    drain_props_probe(api, users, state, report, args,
                      max(0, api.budget - api.n), discovered)
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
        except Budget:
            report["budget_exhausted"] = True
            report["notes"].append(f"budget hit during DB sweep at '{dirname}' — remaining DBs untouched this run")
            return
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
    content pages get a verifying GET, capped, before any local delete)."""
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
    verify_budget = 500
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
        if verify_budget <= 0:
            report["notes"].append("deletion-verification cap reached; remaining candidates deferred")
            complete = False
            break
        verify_budget -= 1
        try:
            pg = api.get(f"/pages/{dashed(i)}")
            if pg.get("in_trash") or pg.get("archived"):
                raise ApiError(404, "in_trash")
            consider(pg)
        except ApiError:
            if pt in ("page_id", "block_id", "workspace"):
                path = page_index.get(i)
                report["pages"]["deleted"].append({"id": i, "title": old.get("title", "")})
                if path and not args.dry_run:
                    os.remove(path)
            meta.pop(i, None)
            if i in order:
                order.remove(i)
        except Budget:
            report["budget_exhausted"] = True
            complete = False
            break

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
    # fresh walk stamp clears the condition.
    def _ts(s):
        try:
            return dt.datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return None
    for pid, mm in meta.items():
        if pid in changed_objs or mm.get("parent_type") not in ("page_id", "block_id", "workspace"):
            continue
        le, scan = _ts(mm.get("last_edited_time") or ""), _ts(state["comment_scans"].get(pid) or "")
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
        # Bot-generated automation pages (the automation subtrees: delta logs — up to
        # 70k blocks of "No changes detected" — and recordings) are append-only,
        # change daily, and carry zero human/comment signal. Re-rendering them
        # daily is the dominant request+time cost. Capture each ONCE (first sight),
        # then skip the body re-walk on later changes (metadata still refreshes).
        # A rare full pass (--mode full-comments, or manual) still refreshes them.
        if body_changed and old is not None and i in page_index \
                and _is_automation(i, page_index) \
                and os.environ.get("NOTION_REFRESH_WALK_AUTOMATION", "0") != "1":
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
                for did, _t in w.child_dbs:
                    discovered.add("db:" + did)
                # automation transcript pages are huge and comment-free: cap their
                # per-block comment scan so daily recording churn stays cheap. A
                # capped page keeps its existing _comments.md section untouched.
                cap = 50 if _is_automation(i, page_index) else None
                bc, capped = w.harvest_comments(cap=cap)
                n_comments = 0
                if capped:
                    report["comments"]["page_scans_capped"] += 1
                else:
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
        except Budget:
            report["budget_exhausted"] = True
            report["notes"].append(f"budget hit during content pass at page {i[:8]} — since-marker held back, will re-detect")
            complete = False
            state["retry_pages"][i] = state["retry_pages"].get(i, 0)  # ensure re-walk next run
            break
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
# human comment traffic. The rolling comment scan cycles them slowly (10% of
# budget) instead of letting them starve the human corpus. Empty when unset.
AUTOMATION_SUBTREES = tuple(
    s for s in (mirror_root.config("NOTION_MIRROR_AUTOMATION_SUBTREES") or "").split(":") if s)


def _is_automation(pid, page_index):
    p = page_index.get(pid)
    if not p:
        return False
    rel = os.path.relpath(p, WS)
    return any(rel.startswith(s + os.sep) for s in AUTOMATION_SUBTREES)


def scan_page_comments(api, users, m, walk_cap=400):
    """Per-block comment rescan of one page -> bullets (walk + harvest +
    page-level + webhook-captured union). walk_cap bounds a single huge page."""
    w = Walker(api, users, max_blocks=6000, max_requests=walk_cap)
    sink = []
    w.walk(dashed(m["id"]), sink, 0)
    pl = w.comments_for(dashed(m["id"]))
    bc, _capped = w.harvest_comments()
    return union_captured(comment_bullets(pl, bc), m["id"], users)


def phase_comment_shard(api, users, meta, state, report, args, budget_req):
    """Rolling per-block comment scan: comments don't bump last_edited_time and
    the API has no global comments feed, so freshness costs one request per
    block. Each run spends up to budget_req requests scanning the longest-
    unscanned pages (90% human corpus, 10% automation subtrees), giving every
    human page a rescan roughly weekly at the default budget."""
    page_index = index_workspace_pages()
    scans = state["comment_scans"]
    content = [m for m in meta.values() if m.get("parent_type") in ("page_id", "block_id", "workspace")
               and not m.get("in_trash") and not m.get("archived")]
    # webhook feed active -> events point at what changed, so the blind scan
    # shrinks to a slow audit; event-named pages always scan first.
    prio_path = os.path.join(STATE, "webhook-priority-pages.json")
    prio = set(jload(prio_path, []))
    ev = os.path.join(STATE, "webhook-events.jsonl")
    if os.path.exists(ev) and time.time() - os.path.getmtime(ev) < 48 * 3600:
        reduced = int(os.environ.get("NOTION_REFRESH_COMMENT_BUDGET_WEBHOOK", "1500"))
        if reduced < budget_req:
            log(f"webhook feed active — shard budget {budget_req} -> {reduced}")
            budget_req = reduced
    human = [m for m in content if not _is_automation(m["id"], page_index)]
    auto = [m for m in content if _is_automation(m["id"], page_index)]
    prio_pages = [m for m in content if m["id"] in prio]
    updates = {}
    start = api.n
    counts = {"prio": 0, "human": 0, "auto": 0}
    done = set()

    def scan(pages, label, cap):
        wc = 40 if label == "auto" else 400
        for m in sorted(pages, key=lambda x: scans.get(x["id"], "")):
            if m["id"] in done:
                continue
            if api.n - start >= cap:
                return
            try:
                updates[m["id"]] = {"title": m.get("title") or "(untitled)",
                                    "bullets": scan_page_comments(api, users, m, walk_cap=wc)}
                scans[m["id"]] = now_iso()
                counts[label] += 1
                done.add(m["id"])
                prio.discard(m["id"])
            except Budget:
                report["budget_exhausted"] = True
                return
            except ApiError as e:
                scans[m["id"]] = now_iso()  # don't wedge the queue on a 404/403 page
                done.add(m["id"])
                prio.discard(m["id"])
                report["pages"]["errors"].append({"id": m["id"], "title": m.get("title", ""),
                                                  "error": f"comment shard: {e}"[:160]})

    scan(prio_pages, "prio", budget_req)
    scan(human, "human", budget_req)
    # automation-subtree pages (DeltaLogs/recordings) are bot logs with no human
    # comments — don't spend the blind-scan budget on them (webhooks/full-comments
    # mode still cover the rare case). Give the whole budget to human pages.
    # drop priority ids that are neither content pages nor rows (DB-level events,
    # not-yet-swept new pages): the daily sweeps cover them, and they'd otherwise
    # sit in the queue forever
    known = {m["id"] for m in content}
    row_ids = set().union(*state["rows"].values()) if state["rows"] else set()
    prio &= (known | row_ids)

    # rows carry comments too (1.5k+ known across the task, meeting and notes tables) and the
    # page shard never touches them — cycle the longest-unaudited comment-bearing
    # rows through the probe queue (drained next run; ~6-day full cycle), which
    # is also the only way row resolved-thread annotations ever fire.
    crows = state["comment_rows"]
    if not crows:
        seeded = 0
        for db_id, dirname in db_dirs().items():
            dirpath = os.path.join(DBS, dirname)
            for f in os.listdir(dirpath):
                if f.endswith(".md") and not f.startswith("_schema"):
                    mm2 = ID32.search(f)
                    if not mm2:
                        continue
                    try:
                        if has_comments(open(os.path.join(dirpath, f)).read()):
                            crows[mm2.group(1)] = ""
                            seeded += 1
                    except OSError:
                        continue
        log(f"comment-row audit seeded: {seeded} rows")
    row_to_db = {rid: db for db, rows in state["rows"].items() for rid in rows}
    queued = 0
    per_run = int(os.environ.get("NOTION_REFRESH_ROW_COMMENT_AUDIT", "120"))
    for rid in sorted(crows, key=lambda r: crows[r]):
        if queued >= per_run:
            break
        db = row_to_db.get(rid)
        if db is None:
            crows.pop(rid, None)  # row deleted since seeding
            continue
        state["queue"].append({"kind": "row_probe", "db": db, "row": rid})
        crows[rid] = now_iso()
        queued += 1
    if queued:
        report["comments"]["shard_rows_queued"] = queued
        log(f"comment-row audit: {queued} probes queued for next run "
            f"(pool {len(crows)}, cycle ≈ {round(len(crows) / max(1, queued), 1)} runs)")
    if not args.dry_run:
        jsave(prio_path, sorted(prio))
    if not args.dry_run:
        update_comments_md(updates, report)
    spent = api.n - start
    report["comments"]["pages_rescanned"] += counts["prio"] + counts["human"] + counts["auto"]
    report["comments"]["shard"] = {
        "prio_scanned": counts["prio"],
        "human_scanned": counts["human"], "auto_scanned": counts["auto"], "requests": spent,
        "human_total": len(human), "auto_total": len(auto),
        "human_cycle_days_est": round(len(human) / counts["human"], 1) if counts["human"] else None,
    }
    log(f"comment shard: {counts['prio']}p+{counts['human']}h+{counts['auto']}a pages, {spent} req "
        f"(human corpus {len(human)}, est cycle {report['comments']['shard']['human_cycle_days_est']}d)")


def phase_full_comment_sweep(api, users, meta, state, report, args):
    """Manual (--mode full-comments): per-block rescan of every HUMAN content
    page in one run (~55k requests, ~12h — automation subtrees excluded; their
    1.2M blocks are only ever cycled slowly by the daily shard). A clean,
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
                                "bullets": scan_page_comments(api, users, m, walk_cap=400)}
            state["comment_scans"][m["id"]] = now_iso()
            report["comments"]["pages_rescanned"] += 1
        except Budget:
            report["budget_exhausted"] = True
            report["notes"].append(f"budget hit during comment sweep at {m['id'][:8]} ({n}/{len(content)})")
            clean = False
            break
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
    re-harvests every night, so without it they are re-asked on every run
    against a budget that already goes PARTIAL on a third of them — and each
    answer writes a `not_a_db` flag that is simply wrong, since they are
    unshared rather than linked views.
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
        try:
            capture_new_db(api, users, did, state, report, args)
            known.add(did)
        except Budget:
            report["budget_exhausted"] = True
            return
    # search-visible new databases
    try:
        for r in api.paginate("POST", "/search", body={
                "filter": {"value": "database", "property": "object"},
                "sort": {"timestamp": "last_edited_time", "direction": "descending"},
                "page_size": 100}):
            did = undash(r["id"])
            if not skip_discovery(did, known, state):
                capture_new_db(api, users, did, state, report, args)
                known.add(did)
    except Budget:
        report["budget_exhausted"] = True
    except ApiError as e:
        report["notes"].append(f"database discovery search failed: {e}"[:200])


def phase_schema_sweep(api, users, state, report, args):
    """Weekly: refresh every _schema.json/_schema.md (+ row/prop counts).
    Detects DB renames and cascades them (dir, csv, row-md headers)."""
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
        try:
            _props, live_title = refresh_schema_files(api, dirpath, db_id, title, nrows, report)
        except Budget:
            report["budget_exhausted"] = True
            return
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
        except Budget:
            print(f"[stop] request budget hit before {dirname}")
            break
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
            fresh, capped = probe_row(api, users, rid, dirpath, report,
                                      old_comments_body=extract_comments_body(disk))
        except Budget:
            print("[stop] request budget hit")
            break
        except ApiError as e:
            print(f"[skip] {fname[:60]}: {e}")
            continue
        checked += 1
        ok = mask_stamps(fresh) == mask_stamps(disk)
        mismatch += not ok
        print(f"[{'ok      ' if ok else 'MISMATCH'}] {os.path.basename(dirpath)[:34]}/"
              f"{fname[:44]} req={api.n - n0}{' capped' if capped else ''}")
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
WRITE_MODES = ("daily", "full-comments", "place", "rows")


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
                                       "validate", "rows", "contamination-check"],
                    default="daily",
                    help="daily = everything incremental incl. rolling comment shard; "
                         "full-comments = manual whole-human-corpus comment sweep (~55k req); "
                         "place = only retry _unplaced placement + regenerate structure.md; "
                         "rows = refresh only the rows named by --rows (plus a props-probe "
                         "drain), touching nothing else; "
                         "weekly/monthly are legacy aliases (daily / full-comments)")
    ap.add_argument("--rows", default="", help="--mode rows: page ids, comma- or space-separated")
    ap.add_argument("--rps", type=float, default=3.0)  # Notion's ~3 req/s per-token cap
    ap.add_argument("--budget", type=int, default=0, help="max API requests (0 = mode default)")
    ap.add_argument("--comment-budget", type=int,
                    default=int(os.environ.get("NOTION_REFRESH_COMMENT_BUDGET", "4000")),
                    help="requests per run for the rolling comment shard")
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
    if args.mode in WRITE_MODES and not args.dry_run:
        try:
            take_lock()
        except MirrorLocked as e:
            print(f"refresh.py: {e}", file=sys.stderr)
            return 3
    budget = args.budget or MODE_BUDGETS[args.mode]
    api = Api(token, args.rps, budget)
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
        "queue": jload(os.path.join(STATE, "probe-queue.json"), []),
        "db404": dbflags.get("db404", {}),
        "not_a_db": dbflags.get("not_a_db", {}),
        # written by coverage_backfill.py: databases whose data sources are not
        # shared with the integration. Same {id32: iso-date} shape as not_a_db.
        "unshared": dbflags.get("unshared", {}),
        "probe_policy": dbflags.get("probe_policy", {}),
        "content_since": jload(os.path.join(STATE, paths.LAST_RUN), {}).get("content_since"),
        "comment_scans": jload(os.path.join(STATE, "comment-scan.json"), {}),
        "retry_pages": jload(os.path.join(STATE, "retry-pages.json"), {}),
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
        n0 = api.n
        try:
            return fn(*a, **kw)
        finally:
            report["phases"][name] = report["phases"].get(name, 0) + (api.n - n0)

    try:
        if args.mode == "place":
            meta, _order = load_meta_jsonl()
            place_unplaced_pass(api, users, meta, index_workspace_pages(),
                                row_md_global_index(), state, report, args, max_api=1500)
        elif args.mode == "rows":
            run_phase("rows", phase_rows, api, users, state, report, args, row_ids, discovered)
        else:
            check_webhook_liveness(report)
            consume_db_events(state, report, discovered)
            queue_budget = int(os.environ.get("NOTION_REFRESH_QUEUE_BUDGET", "0")) or max(1000, budget // 3)
            run_phase("queue", phase_queue, api, users, state, report, args, max_req=queue_budget,
                      discovered=discovered)
            if not args.skip_dbs:
                run_phase("dbs", phase_dbs, api, users, state, report, args, discovered)
            # After the sweep, not before: entries queued before it are already
            # covered (the per-DB query re-renders every property table), while
            # entries that arrived during a multi-hour sweep are not. The cap
            # bounds the redundant remainder either way. The nightly drains this
            # queue at all so that a broken hourly job leaves a bounded file
            # rather than an unbounded one.
            run_phase("props", drain_props_probe, api, users, state, report, args,
                      int(os.environ.get("NOTION_REFRESH_PROPS_BUDGET", "1000")), discovered)
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
                    shard_budget = max(0, min(args.comment_budget, budget - api.n))
                    if shard_budget > 100:
                        run_phase("comments", phase_comment_shard, api, users, meta, state, report,
                                  args, shard_budget)
                    else:
                        report["notes"].append("comment shard skipped: request budget exhausted by earlier phases")
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
    except Budget:
        report["budget_exhausted"] = True
    finally:
        report["requests"] = api.n
        report["rate429"] = api.r429
        report["duration_s"] = int(time.time() - t0)
        users.save()
        # above the mode branch: it writes to the report, not to disk, and a dry
        # run reporting 0 deferred probes hides one of the things it exists to show
        report["deferred"]["row_probes_queued"] = len(state["queue"])
        if args.mode == "rows":
            # A rows run persists only what it actually changed. In particular it
            # must not rewrite probe-queue.json or webhook-priority-pages.json:
            # the receiver appends to both between our load and our save, and a
            # wholesale rewrite from a stale in-memory copy is exactly the
            # lost-update that props_probe's separate file exists to avoid.
            if not args.dry_run:
                jsave(os.path.join(STATE, "comment-rows.json"), state["comment_rows"])
        elif not args.dry_run:
            known = set(db_dirs())
            left = sorted(t for t in discovered
                          if t.startswith("db:") and not skip_discovery(t[3:], known, state))
            if left or os.path.exists(pending_disc):
                jsave(pending_disc, left)
            jsave(rows_state_path, state["rows"])
            jsave(os.path.join(STATE, "probe-queue.json"), state["queue"])
            jsave(os.path.join(STATE, "comment-scan.json"), state["comment_scans"])
            jsave(os.path.join(STATE, "retry-pages.json"), state["retry_pages"])
            jsave(os.path.join(STATE, "comment-rows.json"), state["comment_rows"])
            jsave(os.path.join(STATE, "db-flags.json"),
                  {"db404": state["db404"], "not_a_db": state["not_a_db"],
                   "unshared": state["unshared"], "probe_policy": state["probe_policy"]})
            jsave(os.path.join(STATE, paths.LAST_RUN),
                  {"content_since": state["content_since"], "ts": report["ts"], "mode": args.mode})
        # In the finally on purpose: a run that exhausted its budget still wrote
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
        if args.mode in ("daily", "full-comments", "rows"):
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
                       or report.get("rows", {}).get("refreshed")
                       or report.get("props_probe", {}).get("drained"))
    print(json.dumps({"changes": has_changes, "requests": api.n,
                      "budget_exhausted": report["budget_exhausted"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
