#!/usr/bin/env python3
"""Merge the resolved-comment backfill (webhook-comments-capture.jsonl, produced
by backfill_resolved_comments.py) into the mirror promptly, instead of waiting
for the rolling scan to reach every page over ~a week.

For each captured page/row, adds comments not already present:
  * resolved threads  -> annotated `_[resolved/deleted ≤date]_`
  * still-open threads -> plain (a later scan confirms; retention re-annotates if
    they resolve). Dedup keys on the bullet's text minus our own annotations
    (`refresh.bullet_text_key`), so a bullet already carrying its cid trailer
    still matches the trailer-free one rendered here.
Content pages merge into workspace/_comments.md; DB rows into their row `.md`.
Local only (no API). Idempotent. Reuses refresh.py's helpers, including its
mirror lock: this writes the tree, so it takes `~/.locks/notion-mirror-internal`
and refuses rather than interleaving with a refresh or another backfill.
"""
import datetime as dt
import importlib.util
import json
import os
import re
import sys

import paths

# `TOOLS` is this file's own directory — code, not data: it is where the sibling engine
# is loaded from. The data roots come from `refresh`, which resolves them through `paths`.
TOOLS = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("refresh", os.path.join(TOOLS, "refresh.py"))
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)

TODAY = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
CAP = os.path.join(R.STATE, paths.CAPTURE)


def load_captures():
    """page_id -> list of (anchor, comment) from backfill entries only."""
    out = {}
    if not os.path.exists(CAP):
        return out
    for ln in open(CAP):
        try:
            e = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if e.get("captured_at") != "backfill":
            continue
        for c in e.get("comments", []):
            out.setdefault(e["page_id"], []).append((e.get("anchor", "(page-level)"), c))
    return out


class NameCache:
    """Resolve notion_user ids -> names from the mirror's user cache; fall back
    to the short id (no API calls)."""
    def __init__(self):
        self.m = R.jload(os.path.join(R.STATE, paths.USERS), {})

    def name(self, ref):
        uid = ref.get("id", "") if isinstance(ref, dict) else ref
        return self.m.get(uid) or (uid[:8] if uid else "?")


def page_bullet(anchor, c, users):
    who = users.name({"id": c.get("author_id", "")})
    when = (c.get("created_time") or "")[:10]
    b = f'- **on** "{anchor}" — {who} ({when}): {c.get("text", "")}'
    return b + (f" _[resolved/deleted ≤{TODAY}]_" if c.get("resolved") else "")


def row_bullet(c, users):
    who = users.name({"id": c.get("author_id", "")})
    when = (c.get("created_time") or "")[:10]
    t = c.get("text", "").replace("\r", "").replace("\n", "\n  ")
    b = f"- _{who} ({when}):_ {t}"
    first, sep, rest = b.partition("\n")
    return (first + (f" _[resolved/deleted ≤{TODAY}]_" if c.get("resolved") else "")) + (sep + rest if sep else "")


def main():
    # A standalone mirror writer: it rewrites row `.md` files and _comments.md
    # directly, so it takes the same lock `refresh.sh` and the backfills take.
    # Without it a merge started while a refresh is mid-render puts two writers
    # on one tree, which is how a 99%-truncated CSV reached a commit on
    # 2026-08-13. Taken before the first read so a refused run does no work.
    try:
        R.take_lock()
    except R.MirrorLocked:
        print(f"merge_backfill: another mirror writer holds {R.lock_path()} "
              "(a refresh, or another backfill) - refusing to write the mirror "
              "concurrently. Wait for it to finish, then re-run.", file=sys.stderr)
        raise SystemExit(1)

    caps = load_captures()
    if not caps:
        print("no backfill captures to merge"); return
    users = NameCache()

    # split targets into content pages (in _comments.md domain) vs DB rows
    row_index = R.row_md_global_index()
    page_index = R.index_workspace_pages()

    # --- content pages -> _comments.md ---
    head, sections, appendix = R.load_comments_md()
    by_id = {s["id"]: s for s in sections}
    updates = {}
    page_added = 0
    for pid, cl in caps.items():
        if pid in row_index:
            continue  # handled below
        if pid not in page_index and pid not in by_id:
            # not a mirrored content page (e.g. a row of an unmirrored db) — skip
            continue
        existing = R.split_bullets(by_id[pid]["body"]) if pid in by_id else []
        # Key through refresh.py: the bullets on disk carry the cid trailer and may
        # carry the resolved mark, while the ones rendered here carry neither, so a
        # key that strips only one of the two re-appends everything already merged.
        keys = {R.bullet_text_key(b) for b in existing}
        merged = list(existing)
        add = 0
        for anchor, c in cl:
            b = page_bullet(anchor, c, users)
            k = R.bullet_text_key(b)
            if k in keys:
                continue
            merged.append(b); keys.add(k); add += 1
        if add:
            title = by_id[pid]["title"] if pid in by_id else _title_from_path(page_index.get(pid))
            updates[pid] = {"title": title, "bullets": merged}
            page_added += add
    if updates:
        rep = R.new_report("backfill-merge")
        R.update_comments_md(updates, rep)

    # --- DB rows -> row .md ## Comments ---
    row_added = row_files = 0
    from collections import defaultdict
    for pid, cl in caps.items():
        hit = row_index.get(pid)
        if not hit:
            continue
        dirname, fname = hit
        path = os.path.join(R.DBS, dirname, fname)
        try:
            txt = open(path).read()
        except OSError:
            continue
        cbody = R.extract_comments_body(txt.split(R.MARKER, 1)[1] if R.MARKER in txt else "")
        existing = R.split_bullets(cbody, prefix="- _")
        keys = {R.bullet_text_key(b) for b in existing}
        merged = list(existing)
        add = 0
        for _anchor, c in cl:
            b = row_bullet(c, users)
            k = R.bullet_text_key(b)
            if k in keys:
                continue
            merged.append(b); keys.add(k); add += 1
        if not add:
            continue
        # rebuild the file: head (through marker) + body + ## Comments(merged).
        # Both region operations go through refresh.py so this stays correct on
        # delimiter-bearing files without a second copy of the grammar.
        head_txt, _, tail = txt.partition(R.MARKER)
        body_part = R.strip_comments_section(tail)
        newtxt = head_txt + R.MARKER + body_part.rstrip("\n") + R.comments_section(merged) + "\n"
        if newtxt != txt:
            open(path, "w").write(newtxt)
            row_added += add; row_files += 1

    print(json.dumps({"page_comments_added": page_added, "rows_updated": row_files,
                      "row_comments_added": row_added}))


def _title_from_path(path):
    if not path:
        return "(untitled)"
    base = os.path.basename(path)[:-3]
    return re.sub(r" [0-9a-f]{32}$", "", base) or "(untitled)"


if __name__ == "__main__":
    main()
