#!/usr/bin/env python3
"""Repair comments the resolved-comment backfill attributed to the wrong pages.

`backfill_resolved_comments.harvest()` used to attribute every discussion in a
page's loadPageChunk recordMap to that page. That recordMap hydrates ancestor
context, so parent-page and database-level threads were copied onto every
descendant row the backfill visited (one thread landed on 2,287 pages).

This finds each discussion attributed to more than one page, asks Notion which
block it is really anchored on, walks that block up to its owning page, then:
  * rewrites webhook-comments-capture.jsonl with the corrected attribution, and
  * removes the wrong bullets from the mirror (row `.md` files + _comments.md),
    adding the comment to its true owner where that page is mirrored.

Matching is on merge_backfill's own dedup key (the bullet with the
`_[resolved/deleted ≤date]_` mark stripped), so the merge date is irrelevant and
re-runs are idempotent. Comments the backfill got right are never touched.

Dry-run by default; `--apply` writes and takes `~/.locks/notion-mirror-internal`
(a dry run only reads, so it does not lock). Auth: same token_v2 as the backfill.

    python3 _tools/repair_backfill_attribution.py            # report only
    python3 _tools/repair_backfill_attribution.py --apply
"""
import argparse
import collections
import importlib.util
import json
import os
import shutil
import sys

import paths

TOOLS = os.path.dirname(os.path.abspath(__file__))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(TOOLS, path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


R = _load("refresh", "refresh.py")
MB = _load("merge_backfill", "merge_backfill.py")
BF = _load("backfill", "backfill_resolved_comments.py")

CAP = os.path.join(R.STATE, paths.CAPTURE)
undash, dashed, val = BF.undash, BF.dashed, BF.val


def key_of(bullet):
    """The comment-bullet membership key: text minus the resolved and cid marks.

    Must strip everything the mirror may stamp onto a bullet after capture, or
    the kept-filter at the re-add sites matches nothing and duplicates every
    comment already on the page. bullet_text_key strips both marks."""
    return R.bullet_text_key(bullet)


def load_backfill_entries():
    out = []
    with open(CAP) as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                e = json.loads(ln)
            except json.JSONDecodeError:
                continue
            out.append(e)
    return out


def fetch_records(table, ids):
    """-> {undashed id: value}. Batched syncRecordValues."""
    out, ids = {}, list(ids)
    for i in range(0, len(ids), 90):
        reqs = [{"pointer": {"table": table, "id": dashed(x)}, "version": -1} for x in ids[i:i + 90]]
        try:
            d = BF.v3("syncRecordValues", {"requests": reqs})
        except Exception as ex:  # noqa: BLE001 - report and continue; a bad batch shouldn't sink the run
            print(f"  ! {table} batch {i // 90} failed: {type(ex).__name__}: {str(ex)[:120]}", file=sys.stderr)
            continue
        for rid, rv in ((d.get("recordMap") or {}).get(table) or {}).items():
            v = val(rv)
            if v:
                out[undash(rid)] = v
    return out


def owner_pages(discussion_ids, max_depth=12):
    """discussion id -> owning page id, by walking the anchor block up to its page."""
    discs = fetch_records("discussion", discussion_ids)
    anchor = {d: undash(v.get("parent_id") or "") for d, v in discs.items() if v.get("parent_id")}

    blocks, frontier = {}, set(anchor.values())
    for _ in range(max_depth):
        frontier = {b for b in frontier if b and b not in blocks}
        if not frontier:
            break
        got = fetch_records("block", frontier)
        blocks.update(got)
        frontier = {undash(v.get("parent_id") or "") for v in got.values() if v.get("parent_id")}

    def to_page(bid, depth=0):
        v = blocks.get(bid)
        if not v or depth > max_depth:
            return None
        if v.get("type") == "page":
            return bid
        return to_page(undash(v.get("parent_id") or ""), depth + 1)

    return {d: to_page(b) for d, b in anchor.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: report only)")
    args = ap.parse_args()

    # `--apply` rewrites row `.md` files, _comments.md and the capture file, so
    # it is a mirror writer and takes the same lock as `refresh.sh` and the
    # backfills. A dry run only reads and does not lock, which is the rule
    # coverage_backfill.py and migrate_comment_ids.py already follow.
    if args.apply:
        try:
            R.take_lock()
        except R.MirrorLocked:
            print(f"repair_backfill_attribution: another mirror writer holds "
                  f"{R.lock_path()} (a refresh, or another backfill) - refusing "
                  "to write the mirror concurrently. Wait for it to finish, then "
                  "re-run.", file=sys.stderr)
            raise SystemExit(1)

    entries = load_backfill_entries()
    bf_entries = [e for e in entries if e.get("captured_at") == "backfill"]
    disc_pages = collections.defaultdict(set)
    for e in bf_entries:
        for c in e.get("comments", []):
            if c.get("discussion_id"):
                disc_pages[c["discussion_id"]].add(e["page_id"])
    multi = {d for d, p in disc_pages.items() if len(p) > 1}
    print(f"backfill entries: {len(bf_entries)} | discussions: {len(disc_pages)} | "
          f"attributed to >1 page: {len(multi)}")
    if not multi:
        print("nothing to repair")
        return

    print("resolving true owners via Notion ...")
    owners = owner_pages(multi)
    resolved = {d: p for d, p in owners.items() if p}
    print(f"  resolved {len(resolved)}/{len(multi)}; unresolved stay on every page they are on now")

    # --- rebuild captures with corrected page attribution ---
    users = MB.NameCache()
    row_index, page_index = R.row_md_global_index(), R.index_workspace_pages()

    new_entries, moved, dropped = [], 0, 0
    correct = collections.defaultdict(list)   # page_id -> [(anchor, comment)]
    wrong = collections.defaultdict(list)     # page_id -> [(anchor, comment)] to un-merge
    for e in entries:
        if e.get("captured_at") != "backfill":
            new_entries.append(e)
            continue
        keep = []
        for c in e.get("comments", []):
            did = c.get("discussion_id")
            owner = resolved.get(did) if did in multi else None
            if owner and owner != e["page_id"]:
                wrong[e["page_id"]].append((e.get("anchor", "(page-level)"), c))
                if owner in row_index or owner in page_index:
                    correct[owner].append((e.get("anchor", "(page-level)"), c))
                    moved += 1
                else:
                    dropped += 1
                continue
            keep.append(c)
        if keep:
            new_entries.append({**e, "comments": keep})

    # fold relocated comments into the owner's entry (dedup by comment id)
    by_page = collections.defaultdict(list)
    for e in new_entries:
        if e.get("captured_at") == "backfill":
            by_page[e["page_id"]].extend(c.get("id") for c in e.get("comments", []))
    for pid, cl in correct.items():
        have = set(by_page.get(pid, []))
        add = [c for _a, c in cl if c.get("id") not in have]
        if add:
            new_entries.append({"captured_at": "backfill", "entity_id": pid, "entity_type": "page",
                                "page_id": pid, "anchor": cl[0][0], "comments": add})

    n_wrong = sum(len(v) for v in wrong.values())
    print(f"misattributed comment instances: {n_wrong} across {len(wrong)} pages")
    print(f"  relocated to a mirrored owner: {moved} | owner not mirrored, removed: {dropped}")

    # --- repair the mirror files ---
    files_changed = removed = added = 0
    targets = set(wrong) | set(correct)
    for pid in targets:
        drop_keys = {key_of(MB.row_bullet(c, users)) for _a, c in wrong.get(pid, [])}
        drop_keys |= {key_of(MB.page_bullet(a, c, users)) for a, c in wrong.get(pid, [])}
        hit = row_index.get(pid)
        if hit:
            path = os.path.join(R.DBS, *hit)
            try:
                txt = open(path).read()
            except OSError:
                continue
            body = R.extract_comments_body(txt.split(R.MARKER, 1)[1] if R.MARKER in txt else "")
            bullets = R.split_bullets(body, prefix="- _")
            kept = [b for b in bullets if key_of(b) not in drop_keys]
            n_rm = len(bullets) - len(kept)
            keys = {key_of(b) for b in kept}
            n_ad = 0
            for _a, c in correct.get(pid, []):
                b = MB.row_bullet(c, users)
                if key_of(b) not in keys:
                    kept.append(b); keys.add(key_of(b)); n_ad += 1
            if not (n_rm or n_ad):
                continue
            head_txt, _, tail = txt.partition(R.MARKER)
            newtxt = head_txt + R.MARKER + R.strip_comments_section(tail).rstrip("\n")
            if kept:
                newtxt += R.comments_section(kept)
            newtxt += "\n"
            if newtxt != txt:
                files_changed += 1; removed += n_rm; added += n_ad
                if args.apply:
                    open(path, "w").write(newtxt)

    # --- content pages live in _comments.md ---
    head, sections, appendix = R.load_comments_md()
    by_id = {s["id"]: s for s in sections}
    updates, md_removed, md_added = {}, 0, 0
    for pid in targets:
        if pid not in by_id:
            continue
        drop_keys = {key_of(MB.page_bullet(a, c, users)) for a, c in wrong.get(pid, [])}
        bullets = R.split_bullets(by_id[pid]["body"])
        kept = [b for b in bullets if key_of(b) not in drop_keys]
        n_rm = len(bullets) - len(kept)
        keys = {key_of(b) for b in kept}
        n_ad = 0
        for a, c in correct.get(pid, []):
            b = MB.page_bullet(a, c, users)
            if key_of(b) not in keys:
                kept.append(b); keys.add(key_of(b)); n_ad += 1
        if n_rm or n_ad:
            updates[pid] = {"title": by_id[pid]["title"], "bullets": kept}
            md_removed += n_rm; md_added += n_ad

    print(f"row files: {files_changed} changed ({removed} bullets removed, {added} added)")
    print(f"_comments.md: {len(updates)} sections ({md_removed} removed, {md_added} added)")

    if not args.apply:
        print("\nDRY RUN - nothing written. Re-run with --apply.")
        return

    if updates:
        R.update_comments_md(updates, R.new_report("backfill-repair"))
    shutil.copy2(CAP, CAP + ".pre-repair")
    with open(CAP, "w") as f:
        for e in new_entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    print(f"\napplied. captures rewritten ({len(new_entries)} entries); "
          f"previous kept at {os.path.basename(CAP)}.pre-repair")


if __name__ == "__main__":
    main()
