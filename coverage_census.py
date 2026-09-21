#!/usr/bin/env python3
"""Census the mirror's coverage gap: ids the mirror *references* but never
captured.

The mirror renders a `child_page`, `child_database` or `link_to_page` block as a
one-line stand-in carrying the target's id. When the target itself was never
walked, that stand-in is the only trace it ever existed — an inline database
full of real content, reachable from a mirrored page, absent from the mirror.

This scan is **shape-aware on purpose**. Four generations of renderer wrote the
corpus (`refresh.py`, `notion_walk.py`, the first build's row-body fetchers, and
an earlier one whose short form survives on disk but no longer has an emitter in
the tree), and they do not agree on the line. Matching only the
`— database \\`id\\`` form finds 609 of the 2,080 database stand-ins and none of
the one that motivated this work.

Output is a **work list, not a baseline**: every absent id lands with
`disposition: "unreviewed"` for a human to triage, and the backfill closes what
survives triage. Writing it as a baseline would freeze a real coverage hole into
the definition of "expected".

Two classes of reference are invisible here and are reported as counts instead
(see `count_blind_spots`): stand-ins that carry no id at all, and blocks that
render as nothing or as an `unhandled block type` comment. This census answers
"referenced but absent", never "never referenced".

Usage:
    coverage_census.py [ROOT] [--out PATH]

ROOT defaults to the configured mirror's `workspace/` corpus.
"""
import argparse
import collections
import datetime as dt
import json
import os
import re
import sys

import paths

DEFAULT_ROOT = paths.WS
DEFAULT_OUT = os.path.join(paths.META, "coverage", "census.json")
DEFAULT_EXCLUSIONS = os.path.join(paths.META, "coverage", "exclusions.json")

# The default disposition is backfill; an exclusion is an individually reasoned
# exception, never a bulk or pattern rule. Reasons form a closed set so a later
# reader can challenge a specific line rather than a policy:
#
#   deleted     the page is gone from Notion
#   archived    archived at the time it was checked
#   not_a_db    a linked view or other non-queryable child_database block
#   db404       the database itself is gone
#   no_access   shared with the workspace but not with this integration
#   deliberate  excluded on judgement; the note says why
#
# `no_access` is the only **reversible** one, and it is 94% of the record: of the
# 1,400 entries on disk, 1,322 are that and 78 are `not_a_db`. So a count of
# "1,400 reasoned exclusions" is nearer 78 unavailable objects plus one sharing
# decision applied 1,322 times. Nothing ever re-checks an exclusion, so those
# want a periodic manual re-probe — see `README.md` § About coverage, Exclusions.
EXCLUSION_REASONS = ("deleted", "archived", "not_a_db", "db404", "no_access", "deliberate")

ID = r"[0-9a-f]{32}"

# One entry per stand-in shape that occurs on disk. Order matters only for
# readability of the census; every pattern is anchored and mutually exclusive.
SHAPES = [
    # notion_core/walker.py:123 — the current renderer
    ("child_database", "db_long",
     re.compile(r"^\s*- 🗄️ \*\*(?P<title>.*?)\*\* — database `(?P<id>" + ID + r")` \(rows in workspace/_databases/\)\s*$")),
    # notion_walk.py:114 — the export-era walker
    ("child_database", "db_long_csv",
     re.compile(r"^\s*- 🗄️ \*\*(?P<title>.*?)\*\* — database `(?P<id>" + ID + r")` \(rows in CSV export\)\s*$")),
    # the short form: 1,471 of 2,080 database stand-ins, no surviving emitter
    ("child_database", "db_short",
     re.compile(r"^\s*- 🗄️ (?P<title>[^`]*?)\s*`(?P<id>" + ID + r")`\s*$")),
    # notion_core/walker.py:117
    ("sub_page", "page_long",
     re.compile(r"^\s*- 📄 \*\*(?P<title>.*?)\*\* — sub-page `(?P<id>" + ID + r")`\s*$")),
    # notion_walk.py:105 — carries a have/MISSING tag and the target filename
    ("sub_page", "page_long_status",
     re.compile(r"^\s*- 📄 \*\*(?P<title>.*?)\*\* — sub-page `(?P<id>" + ID + r")` \((?:have|MISSING)\) → `.*`\s*$")),
    ("sub_page", "page_short",
     re.compile(r"^\s*- 📄 (?P<title>[^`]*?)\s*`(?P<id>" + ID + r")`\s*$")),
    # notion_core/walker.py:195 / notion_walk.py:166 — agree on this one
    ("link_to_page", "link_to_page",
     re.compile(r"^\s*- 🔗 link to `(?P<id>" + ID + r")`\s*$")),
]

# Blind spots: real references this scan cannot resolve to an id.
IDLESS_DB = re.compile(r"^\s*- 🗄️ (?![^`]*`" + ID + r"`)")
IDLESS_PAGE = re.compile(r"^\s*- 📄 (?![^`]*`" + ID + r"`)")
UNHANDLED = re.compile(r"^\s*<!-- unhandled block type: ")

# Mirrors refresh.py's ID32: a mirrored artifact is named `<title> <id32>` with
# an optional extension — a page .md, a row .md/.csv, or a database directory.
ID_IN_NAME = re.compile(r"(" + ID + r")(?:\.md|\.csv)?$")

# `coverage_backfill.py` builds a database under `_databases/.partial/` and
# renames it on completion, so nothing half-written is ever named like a real
# artifact. That guarantee is only as good as this scan honouring it: a kill by
# signal skips the backfill's own cleanup, and every file left behind is a real
# file with a real id. Counted, they would report a half-captured database as
# present forever — the precise hole the rename exists to prevent.
PARTIAL_PREFIX = ".partial"


def _prune_partials(dirnames):
    """Drop in-flight capture directories from an `os.walk` descent, in place."""
    dirnames[:] = [d for d in dirnames if not d.startswith(PARTIAL_PREFIX)]


Ref = collections.namedtuple("Ref", "kind shape id title file line")


def scan_text(path, text):
    """Every id-bearing stand-in in one file, in line order."""
    out = []
    for n, line in enumerate(text.split("\n"), 1):
        for kind, shape, pat in SHAPES:
            m = pat.match(line)
            if m:
                out.append(Ref(kind, shape, m.group("id"),
                               (m.groupdict().get("title") or "").strip(), path, n))
                break
    return out


def count_blind_spots(text):
    """References this census cannot enumerate, counted so the gap is at least
    sized: id-less stand-ins (`- 🗄️ Title` with no id, as the first build's fetchers
    wrote them) and blocks the renderer could not represent."""
    counts = {"idless_child_database": 0, "idless_sub_page": 0, "unhandled_block_type": 0}
    for line in text.split("\n"):
        if IDLESS_DB.match(line):
            counts["idless_child_database"] += 1
        elif IDLESS_PAGE.match(line):
            counts["idless_sub_page"] += 1
        elif UNHANDLED.match(line):
            counts["unhandled_block_type"] += 1
    return counts


def present_ids(root):
    """What the mirror actually captured, split by artifact kind — the two are
    not interchangeable. A database is mirrored when its row directory exists
    (`_databases/<title> <id32>/`); a page is mirrored when its `.md` exists.

    Keeping them distinct matters: 112 corpus ids are referenced as databases and
    exist only as some page's `.md`. Counting those as present would report the
    database as captured while every one of its rows is still missing.

    Presence is a fact about the filesystem, not about state files, so a census
    run needs nothing but the corpus."""
    dirs, pages = set(), set()
    for _dirpath, dirnames, filenames in os.walk(root):
        _prune_partials(dirnames)
        for name in dirnames:
            m = ID_IN_NAME.search(name)
            if m:
                dirs.add(m.group(1))
        for name in filenames:
            if name.endswith(".md"):
                m = ID_IN_NAME.search(name)
                if m:
                    pages.add(m.group(1))
    return {"database": dirs, "page": pages}


# Which captured artifact satisfies a reference. A `link_to_page` block carries
# a page_id *or* a database_id (notion_core/walker.py:194), so either resolves it.
PRESENCE = {
    "child_database": ("database",),
    "sub_page": ("page",),
    "link_to_page": ("database", "page"),
}


def is_present(kind, id32, have):
    return any(id32 in have[k] for k in PRESENCE[kind])


def scan_corpus(root):
    """(refs, blind_spot_counts) over every mirrored .md under root."""
    refs, blind = [], collections.Counter()
    for dirpath, dirnames, filenames in os.walk(root):
        _prune_partials(dirnames)
        for name in sorted(filenames):
            if not name.endswith(".md"):
                continue
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError as e:
                print(f"  ! unreadable: {rel}: {e}", file=sys.stderr)
                continue
            refs.extend(scan_text(rel, text))
            blind.update(count_blind_spots(text))
    return refs, blind


def census(root):
    refs, blind = scan_corpus(root)
    have = present_ids(root)

    by_kind = collections.defaultdict(set)
    absent = {}
    for r in refs:
        by_kind[r.kind].add(r.id)
        if is_present(r.kind, r.id, have):
            continue
        item = absent.setdefault(r.id, {
            "id": r.id, "kind": r.kind, "title": r.title,
            "disposition": "unreviewed", "occurrences": [],
        })
        if not item["title"] and r.title:
            item["title"] = r.title
        item["occurrences"].append({"file": r.file, "line": r.line, "shape": r.shape})

    totals = {}
    for kind in ("child_database", "sub_page", "link_to_page"):
        ids = by_kind.get(kind, set())
        totals[kind] = {"referenced": len(ids),
                        "absent": sum(1 for i in ids if not is_present(kind, i, have))}

    return {
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        # Anchored on the corpus itself ("<mirror>/workspace"), not on the caller's
        # cwd or the script's location: a census run from elsewhere must not write
        # "../../../mirror/workspace" into a committed file and churn the diff for
        # the next runner.
        "corpus_root": "/".join(os.path.abspath(root).split(os.sep)[-2:]),
        "totals": totals,
        "blind_spots": {k: blind.get(k, 0) for k in
                        ("idless_child_database", "idless_sub_page", "unhandled_block_type")},
        "absent": [absent[i] for i in sorted(absent)],
    }


def load_exclusions(path=DEFAULT_EXCLUSIONS):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def add_exclusion(page_id, reason, note="", path=DEFAULT_EXCLUSIONS, today=None):
    if reason not in EXCLUSION_REASONS:
        raise ValueError(f"reason must be one of {EXCLUSION_REASONS}, got {reason!r}")
    if reason == "deliberate" and not note:
        raise ValueError("a 'deliberate' exclusion needs a free-text note saying why")
    excl = load_exclusions(path)
    excl[page_id] = {"reason": reason, "note": note,
                     "added": today or dt.date.today().isoformat()}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as f:
        json.dump(dict(sorted(excl.items())), f, indent=1, ensure_ascii=False)
        f.write("\n")
    return excl[page_id]


def report(census_path=DEFAULT_OUT, exclusions_path=DEFAULT_EXCLUSIONS, out=sys.stdout):
    """Split the census into to-backfill and excluded; return the split."""
    with open(census_path) as f:
        cen = json.load(f)
    excl = load_exclusions(exclusions_path)
    by_reason = collections.Counter()
    excluded, backfill = [], []
    for e in cen["absent"]:
        x = excl.get(e["id"])
        if x:
            by_reason[x["reason"]] += 1
            excluded.append(e)
        else:
            backfill.append(e)
    unref = sorted(set(excl) - {e["id"] for e in cen["absent"]})
    print(f"absent total     {len(cen['absent']):6d}", file=out)
    for r in EXCLUSION_REASONS:
        if by_reason[r]:
            print(f"  excluded {r:12s} {by_reason[r]:4d}", file=out)
    print(f"to-backfill      {len(backfill):6d}", file=out)
    if unref:
        print(f"(exclusions not in the absent set: {len(unref)} — harmless, they subtract nothing)", file=out)
    return {"backfill": backfill, "excluded": excluded, "by_reason": dict(by_reason)}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("root", nargs="?", default=DEFAULT_ROOT,
                    help="corpus root (default: the configured mirror's workspace/)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="census JSON path")
    ap.add_argument("--report", action="store_true",
                    help="split the existing census into to-backfill vs excluded and exit")
    ap.add_argument("--exclude", metavar="ID",
                    help="record one exclusion (with --reason, optional --note) and exit")
    ap.add_argument("--reason", choices=EXCLUSION_REASONS)
    ap.add_argument("--note", default="")
    ap.add_argument("--exclusions", default=DEFAULT_EXCLUSIONS,
                    help="exclusions JSON path")
    args = ap.parse_args(argv)

    if args.exclude:
        if not args.reason:
            ap.error("--exclude needs --reason")
        add_exclusion(args.exclude, args.reason, args.note, path=args.exclusions)
        print(f"excluded {args.exclude} ({args.reason})")
        return 0
    if args.report:
        report(args.out, args.exclusions)
        return 0

    if not os.path.isdir(args.root):
        ap.error(f"corpus root not found: {args.root}")

    out = census(args.root)
    text = json.dumps(out, indent=1, ensure_ascii=False) + "\n"
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        f.write(text)

    for kind, t in out["totals"].items():
        print(f"{kind:16s} referenced {t['referenced']:6d}   absent {t['absent']:6d}")
    print("blind spots (no id, not enumerable): " +
          ", ".join(f"{k}={v}" for k, v in out["blind_spots"].items()))
    print(f"-> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
