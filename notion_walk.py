#!/usr/bin/env python3
"""Recursively export a Notion page subtree to Markdown via the raw REST API.

- Walks /blocks/{id}/children, recursing into EVERY has_children block so nested
  child_page/child_database blocks (inside columns, toggles, callouts) are found.
- child_page  -> enqueued as its own page (BFS), rendered to its own .md file.
- child_database -> noted inline (rows live in the CSV export); not recursed.
- have-set pruning: a page whose id is already in the drop export is skipped
  (logged as 'have') and NOT recursed (Notion export subtrees are complete).
- Rate limited (shared 3 rps token budget): --sleep is per-request sleep.
- Robust: honors Retry-After on 429/529, backs off on 5xx/network.
- Emits JSONL log: one {id,title,status,parent,nblocks} per page.

Env: NOTION_TOKEN
"""
import os, sys, json, time, argparse, re, urllib.parse, urllib.request, urllib.error

API = "https://api.notion.com/v1"
VER = "2022-06-28"
TOKEN = os.environ["NOTION_TOKEN"]

REQ_COUNT = 0
R429 = 0

def api_get(path, params, sleep):
    global REQ_COUNT, R429
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt in range(9):
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {TOKEN}",
            "Notion-Version": VER,
        })
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                data = json.loads(r.read().decode("utf-8"))
            REQ_COUNT += 1
            time.sleep(sleep)
            return data
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            if e.code in (429, 529):
                R429 += 1
                ra = e.headers.get("Retry-After")
                wait = float(ra) if ra else min(2 ** attempt, 30)
                sys.stderr.write(f"[rate] {e.code} retry-after {wait}s on {path}\n")
                time.sleep(wait + 0.5)
                continue
            if 500 <= e.code < 600:
                time.sleep(min(2 ** attempt, 30)); continue
            raise RuntimeError(f"HTTP {e.code} on {path}: {body[:300]}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            sys.stderr.write(f"[net] {e} on {path}, retry\n")
            time.sleep(min(2 ** attempt, 30)); continue
    raise RuntimeError(f"exhausted retries on {path}")

def rich(rts):
    out = []
    for rt in rts or []:
        t = rt.get("plain_text", "")
        a = rt.get("annotations", {}) or {}
        href = rt.get("href")
        if a.get("code"): t = f"`{t}`"
        if a.get("bold"): t = f"**{t}**"
        if a.get("italic"): t = f"*{t}*"
        if a.get("strikethrough"): t = f"~~{t}~~"
        if href: t = f"[{t}]({href})"
        out.append(t)
    return "".join(out)

def slug(title):
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (title or "untitled")).strip("-").lower()
    return (s or "untitled")[:50]

def fname(pid, title):
    return f"{pid[:8]}__{slug(title)}.md"

def walk_children(block_id, lines, indent, sleep, queue, seen, have):
    """Append markdown for children of block_id; enqueue child_pages."""
    cursor = None
    nblocks = 0
    while True:
        params = {"page_size": 100}
        if cursor: params["start_cursor"] = cursor
        d = api_get(f"/blocks/{block_id}/children", params, sleep)
        for b in d.get("results", []):
            nblocks += 1
            render_block(b, lines, indent, sleep, queue, seen, have)
        if d.get("has_more"):
            cursor = d.get("next_cursor")
        else:
            break
    return nblocks

def render_block(b, lines, indent, sleep, queue, seen, have):
    t = b.get("type")
    data = b.get(t, {}) or {}
    p = "  " * indent
    recursed = False

    if t == "child_page":
        pid = b["id"].replace("-", "")
        title = data.get("title", "untitled")
        tag = "have" if pid in have else "MISSING"
        lines.append(f"{p}- 📄 **{title}** — sub-page `{pid}` ({tag}) → `{fname(pid, title)}`")
        if pid not in seen:
            seen.add(pid)
            queue.append((b["id"], title, b.get("parent")))
        return
    if t == "child_database":
        title = data.get("title", "untitled")
        did = b["id"].replace("-", "")
        lines.append(f"{p}- 🗄️ **{title}** — database `{did}` (rows in CSV export)")
        return

    if t == "paragraph":
        txt = rich(data.get("rich_text")); lines.append(f"{p}{txt}" if txt else "")
    elif t in ("heading_1", "heading_2", "heading_3"):
        lvl = {"heading_1": "#", "heading_2": "##", "heading_3": "###"}[t]
        lines.append(f"{lvl} {rich(data.get('rich_text'))}")
    elif t == "bulleted_list_item":
        lines.append(f"{p}- {rich(data.get('rich_text'))}")
    elif t == "numbered_list_item":
        lines.append(f"{p}1. {rich(data.get('rich_text'))}")
    elif t == "to_do":
        chk = "x" if data.get("checked") else " "
        lines.append(f"{p}- [{chk}] {rich(data.get('rich_text'))}")
    elif t == "toggle":
        lines.append(f"{p}- ▸ {rich(data.get('rich_text'))}")
    elif t == "quote":
        lines.append(f"{p}> {rich(data.get('rich_text'))}")
    elif t == "callout":
        icon = ((data.get("icon") or {}).get("emoji") or "")
        lines.append(f"{p}> {icon} {rich(data.get('rich_text'))}")
    elif t == "code":
        lang = data.get("language", "") or ""
        body = rich(data.get("rich_text"))
        lines.append(f"{p}```{lang if lang != 'plain text' else ''}")
        for ln in body.split("\n"): lines.append(f"{p}{ln}")
        lines.append(f"{p}```")
        cap = rich(data.get("caption"))
        if cap: lines.append(f"{p}*{cap}*")
    elif t == "divider":
        lines.append(f"{p}---")
    elif t == "equation":
        lines.append(f"{p}$$ {data.get('expression','')} $$")
    elif t in ("image", "file", "pdf", "video", "audio"):
        f = (data.get("external") or data.get("file") or {})
        url = f.get("url", "")
        cap = rich(data.get("caption")) or t
        lines.append(f"{p}![{cap}]({url})")
    elif t in ("bookmark", "embed", "link_preview"):
        lines.append(f"{p}[{data.get('url','')}]({data.get('url','')})")
    elif t == "table_of_contents":
        lines.append(f"{p}*(table of contents)*")
    elif t == "breadcrumb":
        pass
    elif t == "table":
        # table_row children -> markdown table
        render_table(b, lines, indent, sleep)
        return
    elif t in ("column_list", "column", "synced_block"):
        pass  # pure containers -> just recurse below
    elif t == "link_to_page":
        ref = data.get("page_id") or data.get("database_id") or data.get("comment_id") or ""
        lines.append(f"{p}- 🔗 link to `{str(ref).replace('-','')}`")
    elif t == "template":
        lines.append(f"{p}*(template: {rich(data.get('rich_text'))})*")
    elif t == "table_of_contents":
        pass
    else:
        rt = data.get("rich_text")
        if rt:
            lines.append(f"{p}{rich(rt)}")
        else:
            lines.append(f"{p}<!-- unhandled block type: {t} -->")

    # generic recursion for nesting/containers (child_page/db/table handled above/returned)
    if b.get("has_children"):
        nest = indent
        if t in ("bulleted_list_item", "numbered_list_item", "to_do", "toggle", "quote", "callout"):
            nest = indent + 1
        walk_children(b["id"], lines, nest, sleep, queue, seen, have)

def render_table(b, lines, indent, sleep):
    p = "  " * indent
    rows = []
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor: params["start_cursor"] = cursor
        d = api_get(f"/blocks/{b['id']}/children", params, sleep)
        for rb in d.get("results", []):
            if rb.get("type") == "table_row":
                cells = rb["table_row"]["cells"]
                rows.append([rich(c) for c in cells])
        if d.get("has_more"): cursor = d.get("next_cursor")
        else: break
    if not rows: return
    ncol = max(len(r) for r in rows)
    def fmt(r): return "| " + " | ".join((r + [""] * ncol)[:ncol]) + " |"
    lines.append(p + fmt(rows[0]))
    lines.append(p + "| " + " | ".join(["---"] * ncol) + " |")
    for r in rows[1:]:
        lines.append(p + fmt(r))

def export_page(page_id, title, parent, out_dir, have, sleep, queue, seen, logf):
    pid = page_id.replace("-", "")
    lines = [f"# {title}", "", f"<!-- notion page id: {pid} | parent: {json.dumps(parent)} -->", ""]
    nblocks = walk_children(page_id, lines, 0, sleep, queue, seen, have)
    path = os.path.join(out_dir, fname(pid, title))
    with open(path, "w") as fh:
        fh.write("\n".join(lines).rstrip() + "\n")
    logf.write(json.dumps({"id": pid, "title": title, "status": "written",
                           "nblocks": nblocks, "file": os.path.basename(path)}) + "\n")
    logf.flush()
    return nblocks

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", required=True, help="comma-separated page ids (dashed or not)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--have", required=True, help="file of 32-hex ids already in export")
    ap.add_argument("--log", required=True)
    ap.add_argument("--sleep", type=float, default=0.4)
    ap.add_argument("--max-pages", type=int, default=3000)
    ap.add_argument("--titles", default="", help="optional file: id<TAB>title for roots")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    have = set()
    if os.path.exists(args.have):
        have = set(x.strip().lower() for x in open(args.have) if x.strip())
    root_titles = {}
    if args.titles and os.path.exists(args.titles):
        for ln in open(args.titles):
            if "\t" in ln:
                i, ti = ln.rstrip("\n").split("\t", 1)
                root_titles[i.replace("-", "")] = ti

    seen = set()
    queue = []
    for r in args.roots.split(","):
        r = r.strip()
        if not r: continue
        rid = r.replace("-", "")
        seen.add(rid)
        queue.append((r, root_titles.get(rid, f"page-{rid[:8]}"), {"type": "root"}))

    written = pruned = 0
    with open(args.log, "w") as logf:
        i = 0
        while queue and i < args.max_pages:
            page_id, title, parent = queue.pop(0)
            pid = page_id.replace("-", "")
            # root pages are always exported; discovered have-set pages are pruned
            if pid in have and parent.get("type") != "root":
                logf.write(json.dumps({"id": pid, "title": title, "status": "have",
                                       "parent": parent}) + "\n")
                logf.flush()
                pruned += 1
                continue
            try:
                nb = export_page(page_id, title, parent, args.out, have, args.sleep, queue, seen, logf)
                written += 1
                sys.stderr.write(f"[{written}] wrote {pid[:8]} '{title[:50]}' ({nb} blocks) | q={len(queue)} reqs={REQ_COUNT} 429={R429}\n")
            except Exception as e:
                logf.write(json.dumps({"id": pid, "title": title, "status": "error",
                                       "error": str(e)[:300]}) + "\n")
                logf.flush()
                sys.stderr.write(f"[ERR] {pid[:8]} '{title[:50]}': {e}\n")
            i += 1
    print(f"DONE roots={args.roots[:40]} written={written} pruned(have)={pruned} "
          f"requests={REQ_COUNT} rate429={R429} queue_left={len(queue)}")

if __name__ == "__main__":
    main()
