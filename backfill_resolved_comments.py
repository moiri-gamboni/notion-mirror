#!/usr/bin/env python3
"""One-time backfill of resolved (and any API-invisible) comment threads via
Notion's internal `loadPageChunk` record API, authenticated as the workspace
member (token_v2). The public integration API only ever returns OPEN comments;
loadPageChunk returns resolved ones too, and runs on www.notion.so (not the
IP-blocked file.notion.com), so no export/download is involved.

Output: appends to _meta/state/webhook-comments-capture.jsonl in the exact shape
the daily refresh already consumes (webhook_captures / union_captured) — resolved
threads land in the mirror annotated `[resolved/deleted ≤date]` via the existing
merge, deduped by comment id. Idempotent + resumable (a progress file records
done page ids).

Auth: ~/.config/notion/token_v2 (+ backfill-ctx.json for the active-user id).
Throttled; honors Retry-After. Env: BACKFILL_RPS (2.0), BACKFILL_LIMIT (0=all).
"""
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.request

import paths

NOTION = paths.NOTION
STATE = paths.STATE
META = os.path.join(paths.META, "pages-metadata.jsonl")
DBS = paths.DBS
OUT = os.path.join(STATE, paths.CAPTURE)
PROGRESS = os.path.join(STATE, "backfill-progress.json")
RPS = float(os.environ.get("BACKFILL_RPS", "2.0"))
LIMIT = int(os.environ.get("BACKFILL_LIMIT", "0"))

# Credentials are read lazily, never at import. `repair_backfill_attribution.py`
# `exec_module`s this file only to reuse `v3`/`undash`/`dashed`/`val`, and its dry-run
# path makes no request — so a module-level `open()` of ~/.config/notion/{token_v2,
# backfill-ctx.json} turned "report what would change" into a `FileNotFoundError` at import
# on any box without those files. These accessors open on first use (the first `v3` call)
# and memoize, so a live backfill is unchanged.
_ctx = None
_opener = None


def ctx():
    global _ctx
    if _ctx is None:
        _ctx = json.load(open(os.path.expanduser("~/.config/notion/backfill-ctx.json")))
    return _ctx


def opener():
    """A cookie-authed URL opener carrying token_v2; built once, on first request."""
    global _opener
    if _opener is None:
        tok = open(os.path.expanduser("~/.config/notion/token_v2")).read().strip()
        cj = http.cookiejar.CookieJar()
        cj.set_cookie(http.cookiejar.Cookie(0, "token_v2", tok, None, False, ".notion.so", True, True,
                                            "/", True, True, None, False, None, None, {}))
        _opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    return _opener


_n = [0]


def log(m):
    sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {m}\n"); sys.stderr.flush()


def undash(i):
    return (i or "").replace("-", "")


def dashed(i):
    i = undash(i)
    return f"{i[0:8]}-{i[8:12]}-{i[12:16]}-{i[16:20]}-{i[20:32]}" if len(i) == 32 else i


def v3(ep, body):
    for attempt in range(8):
        req = urllib.request.Request(f"https://www.notion.so/api/v3/{ep}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "x-notion-active-user-header": ctx()["user"], "User-Agent": "Mozilla/5.0"})
        try:
            with opener().open(req, timeout=90) as r:
                out = json.loads(r.read())
            _n[0] += 1
            time.sleep(1.0 / RPS)
            return out
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504):
                wait = float(e.headers.get("Retry-After") or min(2 ** attempt, 30))
                time.sleep(wait + 0.5); continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"exhausted retries on {ep}")


def rich(segs):
    return "".join(s[0] for s in (segs or []) if s and isinstance(s, list) and s)


def val(rec):
    v = rec.get("value") or {}
    return v.get("value") or v  # spaces vs pointer-form envelope


def load_page(pid):
    """Full recordMap for a page across chunks."""
    rm = {"block": {}, "discussion": {}, "comment": {}, "notion_user": {}}
    cursor = {"stack": []}
    for _ in range(12):
        d = v3("loadPageChunk", {"pageId": dashed(pid), "limit": 100, "cursor": cursor,
                                 "chunkNumber": 0, "verticalColumns": False})
        r = d.get("recordMap") or {}
        for tbl in rm:
            rm[tbl].update(r.get(tbl) or {})
        cursor = d.get("cursor") or {"stack": []}
        if not cursor.get("stack"):
            break
    return rm


def anchor_for(disc_v, blocks):
    ctx = rich(disc_v.get("context"))
    if ctx.strip():
        return ctx[:90]
    bid = disc_v.get("parent_id")
    bl = blocks.get(bid)
    if bl:
        bv = val(bl)
        props = bv.get("properties") or {}
        title = rich((props.get("title") or []))
        if title.strip():
            return title[:90]
        return f"({bv.get('type', 'block')})"
    return "(page-level)"


def owns_block(pid, blocks):
    """-> predicate(block_id) telling whether that block belongs to page `pid`.

    A block belongs to the page if it is the page itself or its parent chain
    reaches the page without passing through another page. Blocks absent from
    the record map are treated as foreign: loadPageChunk always includes the
    page's own subtree, so a missing block came from somewhere else."""
    want = undash(pid)
    memo = {}

    def owned(bid):
        if not bid:
            return False
        key = undash(bid)
        if key in memo:
            return memo[key]
        memo[key] = False  # cycle guard
        if key == want:
            memo[key] = True
            return True
        bl = blocks.get(bid) or blocks.get(dashed(bid))
        if not bl:
            return False
        bv = val(bl)
        parent = bv.get("parent_id")
        # a different page terminates the chain: its comments are its own
        if bv.get("type") == "page" and key != want:
            return False
        memo[key] = owned(parent)
        return memo[key]

    return owned


def fetch_comment_records(ids):
    """Batch-fetch comment records not present in a page chunk (loadPageChunk
    lists a resolved discussion's comment ids but omits their records) via
    syncRecordValues. -> {id: comment_value}."""
    out = {}
    ids = list(ids)
    for i in range(0, len(ids), 90):
        batch = ids[i:i + 90]
        reqs = [{"pointer": {"table": "comment", "id": dashed(c)}, "version": -1} for c in batch]
        try:
            d = v3("syncRecordValues", {"requests": reqs})
        except Exception:  # noqa: BLE001 - a bad batch shouldn't sink the page
            continue
        for cid, cv in ((d.get("recordMap") or {}).get("comment") or {}).items():
            out[cid] = val(cv)
    return out


def harvest(pid):
    """-> (entry dict or None, resolved_count, total_comment_count)."""
    rm = load_page(pid)
    disc, com, blocks = rm["discussion"], rm["comment"], rm["block"]
    if not disc and not com:
        return None, 0, 0
    # collect every comment id referenced by a discussion; fetch the ones the
    # page chunk didn't include (resolved threads' comment records live outside it)
    missing = []
    for did, dv in disc.items():
        for cid in (val(dv).get("comments") or []):
            if cid not in com and undash(cid) not in {undash(k) for k in com}:
                missing.append(cid)
    if missing:
        com = dict(com)
        com.update(fetch_comment_records(set(missing)))

    out_comments = []
    resolved_n = 0
    seen = set()
    own = owns_block(pid, blocks)
    for did, dv in disc.items():
        d = val(dv)
        # loadPageChunk hydrates ancestor context, so its recordMap carries
        # discussions belonging to parent pages and to the row's database. Keep
        # only threads anchored on a block this page actually owns, or every
        # descendant inherits its ancestors' comments.
        if not own(d.get("parent_id")):
            continue
        anchor = anchor_for(d, blocks)
        is_res = bool(d.get("resolved"))
        if is_res:
            resolved_n += 1
        for cid in (d.get("comments") or []):
            cv = com.get(cid) or com.get(dashed(cid))
            if not cv:
                continue
            c = cv if "text" in cv else val(cv)
            cb = c.get("created_by_id") or (c.get("created_by") or {}).get("id", "")
            out_comments.append({"id": undash(cid), "text": rich(c.get("text")),
                                 "author_id": cb, "created_time": _ts(c.get("created_time")),
                                 "discussion_id": undash(did), "anchor": anchor, "resolved": is_res})
            seen.add(undash(cid))
    # comments not attached to a discussion in the map (rare)
    for cid, cv in com.items():
        if undash(cid) in seen:
            continue
        c = cv if isinstance(cv, dict) and "text" in cv else val(cv)
        cb = c.get("created_by_id") or (c.get("created_by") or {}).get("id", "")
        out_comments.append({"id": undash(cid), "text": rich(c.get("text")),
                             "author_id": cb, "created_time": _ts(c.get("created_time")),
                             "discussion_id": undash(c.get("parent_id", "")),
                             "anchor": "(page-level)", "resolved": False})
    if not out_comments:
        return None, 0, 0
    # one capture entry per (page, anchor) group — matches webhook_captures() shape
    by_anchor = {}
    for c in out_comments:
        by_anchor.setdefault(c.pop("anchor"), []).append(c)
    entries = [{"captured_at": "backfill", "entity_id": undash(pid), "entity_type": "page",
                "page_id": undash(pid), "anchor": anchor, "comments": cs}
               for anchor, cs in by_anchor.items()]
    return entries, resolved_n, len(out_comments)


def _ts(ms):
    if not ms:
        return ""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(int(ms) / 1000))
    except (ValueError, TypeError):
        return str(ms)


def targets():
    """content pages + comment-bearing DB rows."""
    ids = []
    seen = set()
    if os.path.exists(META):
        for ln in open(META):
            try:
                m = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if m.get("parent_type") in ("page_id", "block_id", "workspace") \
                    and not m.get("in_trash") and not m.get("archived"):
                i = undash(m["id"])
                if i not in seen:
                    seen.add(i); ids.append(i)
    import re
    id32 = re.compile(r"([0-9a-f]{32})\.md$")
    for d in os.listdir(DBS):
        dd = os.path.join(DBS, d)
        if not os.path.isdir(dd):
            continue
        for f in os.listdir(dd):
            if f.endswith(".md") and not f.startswith("_schema"):
                mm = id32.search(f)
                if mm and mm.group(1) not in seen:
                    try:
                        if "\n## Comments" in open(os.path.join(dd, f)).read():
                            seen.add(mm.group(1)); ids.append(mm.group(1))
                    except OSError:
                        pass
    return ids


def main():
    done = set(json.load(open(PROGRESS)).get("done", [])) if os.path.exists(PROGRESS) else set()
    ids = [i for i in targets() if i not in done]
    if LIMIT:
        ids = ids[:LIMIT]
    log(f"backfill targets: {len(ids)} pages/rows (skipping {len(done)} done)")
    outf = open(OUT, "a")
    tot_res = tot_pages = 0
    for k, pid in enumerate(ids, 1):
        try:
            entries, res_n, com_n = harvest(pid)
        except Exception as e:  # noqa: BLE001 - keep going; progress not marked so it retries
            log(f"  err {pid[:8]}: {e}")
            continue
        if entries:
            for e in entries:
                outf.write(json.dumps(e, ensure_ascii=False) + "\n")
            outf.flush()
            tot_pages += 1
            tot_res += res_n
        done.add(pid)
        if k % 50 == 0:
            json.dump({"done": sorted(done)}, open(PROGRESS, "w"))
            log(f"  {k}/{len(ids)} | pages w/ comments {tot_pages} | resolved threads {tot_res} | reqs {_n[0]}")
    json.dump({"done": sorted(done)}, open(PROGRESS, "w"))
    outf.close()
    log(f"DONE: {tot_pages} pages had comments, {tot_res} resolved threads captured, {_n[0]} requests")
    print(json.dumps({"pages_with_comments": tot_pages, "resolved_threads": tot_res, "requests": _n[0]}))


if __name__ == "__main__":
    main()
