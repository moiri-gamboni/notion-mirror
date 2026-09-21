#!/usr/bin/env python3
"""Notion webhook receiver for the mirror.

Listens on 127.0.0.1:8098 behind whatever public reverse proxy or tunnel the
deployment puts in front of it. Three jobs:

1. Subscription handshake: Notion POSTs {"verification_token": ...} once when a
   subscription is created — persist it (it doubles as the HMAC signing secret)
   and ntfy it so the human can complete verification in the integration UI.
2. Event intake: validate X-Notion-Signature (HMAC-SHA256 of the raw body with
   the verification token), append the event to _meta/state/webhook-events.jsonl,
   and queue the entity for capture.
3. Capture thread: within ~a minute of a comment event, fetch the comment
   thread's content via the REST API (comments are capture-before-resolve: the
   API only returns open comments, so fetching promptly is what makes later
   resolution non-destructive) and append it, with its block anchor text and
   containing page, to _meta/state/webhook-comments-capture.jsonl. The daily
   refresh merges captures into the mirror and prioritizes event pages.

Stateless besides the _meta/state files; safe to restart any time. Events are
at-most-once from Notion's side — the daily sweep remains the reconciliation
backstop.

That state directory must already exist: `main` asserts it and refuses to start
otherwise, and nothing here creates it. The individual files are still created on first
write — the capture file has no other creator, so a fresh workspace would deadlock
waiting for one — but the directory is the mount point's business, and creating it under
an unmounted volume would hide every capture the moment it mounts.
"""
import datetime as dt
import getpass
import hashlib
import hmac
import json
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import mirror_root

try:
    import paths
except mirror_root.MirrorError as e:
    # `paths` asserts the mirror at import (deliberately), so an unmounted volume
    # would otherwise present as a traceback; the refusal already says how to fix it.
    sys.stderr.write(f"refusing to start: {e}\n")
    sys.exit(1)
import refresh

STATE = paths.STATE
TOKEN_FILE = os.path.join(STATE, paths.WEBHOOK_SECRET)
EVENTS = os.path.join(STATE, "webhook-events.jsonl")
CAPTURE = os.path.join(STATE, paths.CAPTURE)
PRIORITY = os.path.join(STATE, "webhook-priority-pages.json")
# Never probe-queue.json: refresh.py rewrites that file wholesale from memory at
# end of run, so anything appended here during a multi-hour nightly would be
# erased. A separate file makes that structurally impossible.
PROPS_QUEUE = os.path.join(STATE, "props-probe-queue.json")
# base URL lives in refresh.Api; only the API version is ours to pin here
VER = "2022-06-28"
PORT = 8098
PATH_PREFIX = "/notion"

_lock = threading.Lock()
OFFSET = os.path.join(STATE, "webhook-capture-offset.json")
DB_EVENTS = os.path.join(STATE, "webhook-db-events.json")

# Requests/second this process paces itself to. The integration token's ~3 rps
# average is shared with the nightly mirror (3.0) and tasksync (2.0); every
# client honours Retry-After, so contention costs latency, never data.
RPS = 2.0

# Event-log rotation: ~1.6 MB/day observed, so 32 MB is ~20 days per segment and
# 3 segments ~60 days of retained history — well past the 24h liveness alarm.
EVENTS_MAX_BYTES = 32 * 1024 * 1024
EVENTS_SEGMENTS = 3

# Transient-failure retries before an event is abandoned. A failing event holds
# the watermark and is retried once per capture tick, so this bounds how long one
# bad event blocks every event behind it: 6 ticks of 45s is roughly 4.5 minutes.
CAPTURE_MAX_ATTEMPTS = 6

# One ntfy per bucket per hour; further ones are counted and reported on the next
# send, so a systemic Notion outage produces one message an hour, not one per page.
NTFY_COOLDOWN_S = 3600
_ntfy_last = {}
_ntfy_suppressed = {}

# page.* events that warrant a body+comment re-walk of the page. Deliberately
# excludes page.properties_updated: its payload carries data.updated_properties
# (property ids only, no values), i.e. Notion stating the body did NOT change.
# It is also 77% of all events — marking those pages priority made the probe
# queue 68% no-op work and cost 70% of the 07-31 request budget.
PRIORITY_PAGE_EVENTS = ("page.content_updated", "page.created",
                        "page.moved", "page.deleted")

# ... and that event class comes back here instead, on its own queue and its own
# probe kind: one GET, re-render the property table and the CSV line, never the
# body. The disaster was never the event volume, it was routing those events to
# the full body-and-comments probe. At ~1 request per changed page the event is
# affordable, and property changes reach the mirror in about an hour rather than
# about a day.
PROPS_PROBE_EVENTS = ("page.properties_updated",)


def log(msg):
    sys.stderr.write(f"[{dt.datetime.now(dt.timezone.utc).strftime('%H:%M:%S')}] {msg}\n")
    sys.stderr.flush()


def notion_token():
    """`$NOTION_TOKEN`, else the Notion MCP server's env in Claude's `.claude.json` under
    `$CLAUDE_CONFIG_DIR` (default `~/.claude`). Raises when neither is there; `main`
    calls it once before serving so that failure refuses the start.

    Never set NOTION_TOKEN via `Environment=` in the unit file: that file is
    world-readable and lives in git. The config it falls back to is `0600`, which is
    the point — the token is readable by this user either way, so the question is only
    whether it also sits in a tracked file.
    """
    tok = os.environ.get("NOTION_TOKEN")
    if tok:
        return tok
    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    with open(os.path.join(cfg, ".claude.json")) as f:
        return json.load(f)["mcpServers"]["notion"]["env"]["NOTION_TOKEN"]


def ntfy(title, message, priority="default"):
    try:
        tok = ""
        tp = os.path.expanduser("~/services/.ntfy-token")
        if os.path.exists(tp):
            tok = open(tp).read().strip()
        req = urllib.request.Request(
            f"http://localhost:2586/claude-{getpass.getuser()}",
            data=message.encode(), method="POST",
            headers={"Title": title, "Priority": priority,
                     **({"Authorization": f"Bearer {tok}"} if tok else {})})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:  # noqa: BLE001 - notification failure is never fatal
        log(f"ntfy failed: {e}")


def ntfy_throttled(bucket, title, message, priority="default"):
    """ntfy with a per-bucket cooldown. Returns True if it actually sent."""
    with _lock:
        now = time.monotonic()
        if now - _ntfy_last.get(bucket, -NTFY_COOLDOWN_S) < NTFY_COOLDOWN_S:
            _ntfy_suppressed[bucket] = _ntfy_suppressed.get(bucket, 0) + 1
            return False
        _ntfy_last[bucket] = now
        extra = _ntfy_suppressed.pop(bucket, 0)
    if extra:
        message += f"\n(+{extra} more since the last alert on this)"
    ntfy(title, message, priority)
    return True


_api = None


def api():
    """Lazily built so importing this module needs no token on disk."""
    global _api
    with _lock:
        if _api is None:
            # budget is a hard stop in refresh.Api; a daemon has no run to cap.
            _api = refresh.Api(notion_token(), RPS, float("inf"))
        return _api


def api_get(path, params=None):
    return api().get(path, params=params, ver=VER)


def plain(rts):
    return "".join(x.get("plain_text", "") for x in rts or [])


def jappend(path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def add_priority_page(pid):
    with _lock:
        try:
            cur = set(json.load(open(PRIORITY)))
        except (OSError, json.JSONDecodeError):
            cur = set()
        cur.add(pid.replace("-", ""))
        with open(PRIORITY + ".tmp", "w") as f:
            json.dump(sorted(cur), f)
        os.replace(PRIORITY + ".tmp", PRIORITY)


def add_props_probe(pid):
    """Queue a property-only probe, deduped by page id.

    This event class is 77% of traffic and a busy row emits one per edit, so
    without the dedupe the queue would grow faster than any drain empties it.
    Read-modify-write through the same atomic temp-file-plus-replace pattern as
    the other queues; this process is the only writer of new entries."""
    pid = pid.replace("-", "")
    with _lock:
        try:
            cur = json.load(open(PROPS_QUEUE))
        except (OSError, json.JSONDecodeError):
            cur = []
        if not isinstance(cur, list):
            cur = []
        if any(isinstance(e, dict) and e.get("row") == pid for e in cur):
            return
        cur.append({"kind": "props_probe", "row": pid})
        with open(PROPS_QUEUE + ".tmp", "w") as f:
            json.dump(cur, f)
        os.replace(PROPS_QUEUE + ".tmp", PROPS_QUEUE)


def route_event(etype, ent, data):
    """Non-comment events -> the queue that will act on them.

    Comment events are absent on purpose: they are consumed durably from the
    events log by capture_loop, which marks their page priority itself once the
    thread is captured."""
    if not ent.get("id"):
        return
    if etype.startswith(("database.", "data_source.")):
        record_db_event(ent["id"], etype, (data or {}).get("parent"))
    elif etype in PRIORITY_PAGE_EVENTS:
        add_priority_page(ent["id"])
    elif etype in PROPS_PROBE_EVENTS:
        # Only rows: a props probe re-renders a property table and a CSV line,
        # and a page that is not a database row has neither. Three of the first
        # 21,977 properties_updated events observed named a page parent, and
        # every one of those would have cost a request to learn there was
        # nothing to render.
        if ((data or {}).get("parent") or {}).get("type") == "database":
            add_props_probe(ent["id"])


def record_db_event(eid, etype, parent):
    """database.* / data_source.* lifecycle events -> consumed by the daily
    engine (deleted -> strike seed; created -> same-day capture)."""
    with _lock:
        try:
            cur = json.load(open(DB_EVENTS))
        except (OSError, json.JSONDecodeError):
            cur = {}
        cur[eid.replace("-", "")] = {
            "type": etype, "at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "parent": str((parent or {}).get("id", "")).replace("-", ""),
            "parent_type": (parent or {}).get("type", "")}
        with open(DB_EVENTS + ".tmp", "w") as f:
            json.dump(cur, f)
        os.replace(DB_EVENTS + ".tmp", DB_EVENTS)


def capture_entity(entity_id, entity_type, page_hint=""):
    """Fetch the comment thread on entity_id (a page or block, per the event's
    data.parent) + anchor text; page_hint = the event's data.page_id."""
    anchor = "(page-level)"
    page_id = page_hint or entity_id
    if entity_type == "block":
        b = api_get(f"/blocks/{entity_id}")
        t = b.get("type", "")
        data = b.get(t, {}) or {}
        anchor = (plain(data.get("rich_text")) or f"({t})")[:90]
        if not page_hint:
            # climb to the containing page (bounded)
            cur = b
            for _ in range(6):
                par = cur.get("parent", {}) or {}
                pt = par.get("type", "")
                if pt == "page_id":
                    page_id = par["page_id"]
                    break
                if pt == "block_id":
                    cur = api_get(f"/blocks/{par['block_id']}")
                    continue
                page_id = entity_id if pt not in ("database_id",) else par.get(pt, entity_id)
                break
    comments = []
    cur = None
    while True:
        params = {"block_id": entity_id, "page_size": 100}
        if cur:
            params["start_cursor"] = cur
        d = api_get("/comments", params)
        for c in d.get("results", []):
            comments.append({
                # rendered exactly as refresh.py's own scan renders a comment:
                # the two producers' output is compared as whole strings during
                # the merge, so they must never disagree on how one comment
                # looks. `rich_md` keeps @-mention targets as followable links.
                "id": c.get("id"), "text": refresh.rich_md(c.get("rich_text")),
                # Raw items alongside the flattened text: for a block-anchored
                # comment this capture is the only channel it ever travels, so
                # dropping mention targets and hrefs here loses them permanently.
                # Additive — records written before 2026-08-06 have no such key.
                "rich_text": c.get("rich_text") or [],
                "author_id": (c.get("created_by") or {}).get("id", ""),
                "created_time": c.get("created_time", ""),
                "discussion_id": c.get("discussion_id", "")})
        if not d.get("has_more"):
            break
        cur = d.get("next_cursor")
    jappend(CAPTURE, {"captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                      "entity_id": entity_id.replace("-", ""), "entity_type": entity_type,
                      "page_id": str(page_id).replace("-", ""), "anchor": anchor,
                      "comments": comments})
    add_priority_page(str(page_id))
    log(f"captured {len(comments)} comment(s) on {entity_type} {entity_id[:8]} (page {str(page_id)[:8]})")


def abandoned(page_id, entity_id, why):
    """A comment thread this receiver will never capture. The mirror's ~6-day
    rolling shard is the only remaining channel and reaches it only while the
    thread is still unresolved; task.md never sees it. An absent comment is
    indistinguishable from no comment, so this path has to be heard."""
    log(f"giving up capture for {str(entity_id)[:8]} (page {str(page_id)[:8]}): {why}")
    ntfy_throttled(
        "capture-give-up", "Notion webhook: comment capture abandoned",
        f"page {page_id}\nentity {entity_id}\n{why}\n"
        "The thread is unrecoverable here once it is resolved.", "high")


def read_offset():
    st = {"offset": 0, "attempts": {}}
    try:
        with open(OFFSET) as f:
            st.update(json.load(f))
    except (OSError, json.JSONDecodeError):
        pass
    return st


def write_offset(st):
    with open(OFFSET + ".tmp", "w") as f:
        json.dump(st, f)
    os.replace(OFFSET + ".tmp", OFFSET)


def maybe_rotate_events():
    """Size-cap webhook-events.jsonl, handing the capture watermark over.

    Only rotates when the watermark sits exactly at EOF with no pending retries,
    which makes the handover simply offset -> 0: every byte of the outgoing
    segment has already been captured, so nothing is skipped and nothing is
    replayed. Rotating while the reader is behind would either skip the tail or
    (worse) leave the watermark past the end of a fresh file, where the capture
    loop reads nothing and logs success forever.

    Offset is written BEFORE the rename on purpose: a crash between the two then
    replays a drained segment (captures are id-deduped downstream), whereas the
    other order strands the watermark past EOF and silently stops capture.
    """
    with _lock:
        try:
            size = os.path.getsize(EVENTS)
        except OSError:
            return False
        if size < EVENTS_MAX_BYTES:
            return False
        st = read_offset()
        if st.get("offset") != size or st.get("attempts"):
            log(f"events log {size}B over cap but capture is {size - st.get('offset', 0)}B "
                f"behind ({len(st.get('attempts') or {})} retrying) — deferring rotation")
            return False
        st["offset"] = 0
        write_offset(st)
        for i in range(EVENTS_SEGMENTS, 0, -1):
            src = EVENTS if i == 1 else f"{EVENTS}.{i - 1}"
            if os.path.exists(src):
                os.replace(src, f"{EVENTS}.{i}")  # segment EVENTS_SEGMENTS falls off
        log(f"rotated events log at {size}B, keeping {EVENTS_SEGMENTS} segments")
        return True


def capture_tick():
    """One pass over the un-captured tail of the events log.

    Durable capture: driven by the persisted events log + a byte-offset
    watermark, so restarts replay anything un-captured (capture-before-resolve
    must survive crashes). Transient failures (429/5xx/network) retry next tick
    with a bounded attempt count; permanent ones (401/403/404) are skipped."""
    st = read_offset()
    if not os.path.exists(EVENTS):
        return
    size = os.path.getsize(EVENTS)
    if st["offset"] > size:
        # Unreachable via rotation (the handover writes 0 before renaming), so
        # this means the state was corrupted from outside — a hand-edited or
        # restored file. Left alone the loop seeks past EOF, reads nothing and
        # reports success every 45s. Clamp to EOF so new events keep flowing and
        # say so loudly; what the gap contained is not knowable from here.
        log(f"capture watermark {st['offset']} is past EOF {size} — clamping")
        ntfy_throttled("offset-past-eof", "Notion webhook: capture watermark past EOF",
                       f"offset {st['offset']} > events log {size}; clamped to EOF. "
                       "Events between the two were never captured — reconcile from "
                       "the mirror's comment sweep.", "high")
        st["offset"] = size
        write_offset(st)
    advanced = False
    with open(EVENTS) as f:
        f.seek(st["offset"])
        while True:
            ln = f.readline()
            if not ln:
                break
            if not ln.endswith("\n"):  # partial line mid-append; retry next tick
                break
            try:
                e = json.loads(ln)
            except json.JSONDecodeError:
                st["offset"] = f.tell()
                advanced = True
                continue
            etype = e.get("type", "")
            ent = e.get("entity") or {}
            if not etype.startswith("comment.") or not ent.get("id"):
                st["offset"] = f.tell()
                advanced = True
                continue
            # comment events: the ENTITY is the comment; the thread lives on
            # data.parent (page or block), containing page = data.page_id
            d = e.get("data") or {}
            parent = d.get("parent") or {}
            target = str(parent.get("id") or ent.get("id", ""))
            ttype = "block" if parent.get("type") == "block" else "page"
            key = target
            page = str(d.get("page_id") or target)
            try:
                capture_entity(target, ttype if parent.get("id") else "",
                               page_hint=str(d.get("page_id") or ""))
                st["attempts"].pop(key, None)
                st["offset"] = f.tell()
                advanced = True
            except refresh.ApiError as ex:
                if ex.code in (401, 403, 404, 400):
                    abandoned(page, key, f"permanent capture failure {ex.code}")
                    st["attempts"].pop(key, None)
                    st["offset"] = f.tell()
                    advanced = True
                    continue
                st["attempts"][key] = st["attempts"].get(key, 0) + 1
                if st["attempts"][key] > CAPTURE_MAX_ATTEMPTS:
                    abandoned(page, key, f"{st['attempts'][key]} attempts exhausted (HTTP {ex.code})")
                    st["attempts"].pop(key, None)
                    st["offset"] = f.tell()
                    advanced = True
                    continue
                log(f"transient capture failure ({ex.code}) for {key[:8]} — will retry")
                break  # hold the watermark; retry this event next tick
            except Exception as ex:  # noqa: BLE001 - network etc: transient
                st["attempts"][key] = st["attempts"].get(key, 0) + 1
                if st["attempts"][key] > CAPTURE_MAX_ATTEMPTS:
                    abandoned(page, key, f"{st['attempts'][key]} attempts exhausted: {ex}")
                    st["attempts"].pop(key, None)
                    st["offset"] = f.tell()
                    advanced = True
                    continue
                log(f"transient capture failure for {key[:8]}: {ex} — will retry")
                break
    if advanced or st["attempts"]:
        write_offset(st)
    maybe_rotate_events()


def capture_loop():
    while True:
        time.sleep(45)
        try:
            capture_tick()
        except Exception as ex:  # noqa: BLE001 - an unguarded raise here ends the
            # thread while the HTTP side keeps 200-ing Notion: capture would stop
            # permanently and look exactly like a quiet week.
            log(f"capture tick failed: {ex}")
            ntfy_throttled("capture-tick", "Notion webhook: capture tick failed",
                           f"{type(ex).__name__}: {ex}", "high")


class Handler(BaseHTTPRequestHandler):
    server_version = "notion-mirror-hooks"

    def log_message(self, fmt, *args):  # route to our logger
        log(fmt % args)

    def _respond(self, code, body=b"ok"):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._respond(200 if self.path.startswith(PATH_PREFIX) else 404, b"notion-mirror webhook receiver")

    def do_POST(self):
        if not self.path.startswith(PATH_PREFIX):
            return self._respond(404, b"not found")
        n = int(self.headers.get("Content-Length", 0))
        if n > 1_000_000:
            return self._respond(413, b"too large")
        body = self.rfile.read(n)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return self._respond(400, b"bad json")

        # one-time subscription handshake
        if "verification_token" in payload:
            fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(payload["verification_token"])
            log("verification token received and stored")
            ntfy("Notion webhook: verification token received",
                 "Stored on the server. Return to the integration's webhook settings and "
                 "click Verify — Notion checks the endpoint echo automatically; if it asks "
                 "for the token, it's in notion/_meta/state/webhook-secret.", "high")
            return self._respond(200)

        # signature check on real events
        try:
            secret = open(TOKEN_FILE).read().strip()
        except OSError:
            log("event received but no verification token stored yet — rejecting")
            return self._respond(401, b"no secret")
        expect = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        got = self.headers.get("X-Notion-Signature", "")
        if not hmac.compare_digest(expect, got):
            log("signature mismatch — rejecting")
            return self._respond(401, b"bad signature")

        etype = payload.get("type", "")
        ent = payload.get("entity", {}) or {}
        # under _lock so an append can never land between rotation's watermark
        # write and its rename, which would drop the event
        with _lock:
            jappend(EVENTS, {"received_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                             "type": etype, "entity": ent, "timestamp": payload.get("timestamp"),
                             "data": payload.get("data")})
        route_event(etype, ent, payload.get("data") or {})
        self._respond(200)


def main():
    # The state directory is asserted, never created. This process is a daemon started by
    # systemd against a `nofail` bind mount: if `notion/` is not mounted, creating the
    # directory would put captures — the only copy of a block-anchored comment thread —
    # under the mount point, where the mount hides them and the drain never sees them.
    # Refusing to start is what makes that a restart loop the unit reports.
    if not os.path.isdir(STATE):
        log(f"refusing to start: {STATE} is not a directory "
            "(is the mirror volume mounted?)")
        return 1
    # The token is read here, once, and not lazily on the first comment event: read
    # there, a missing config lands in the capture loop's transient branch, is retried
    # to the cap, and the thread is abandoned while the unit reports healthy.
    try:
        notion_token()
    except (OSError, KeyError, ValueError) as e:
        log(f"refusing to start: no Notion token — {e!r} "
            "(set NOTION_TOKEN or CLAUDE_CONFIG_DIR)")
        return 1
    threading.Thread(target=capture_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log(f"listening on 127.0.0.1:{PORT}{PATH_PREFIX}")
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
