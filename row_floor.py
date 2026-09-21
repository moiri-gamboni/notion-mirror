#!/usr/bin/env python3
"""Pre-commit row floor: no mirrored CSV may lose most of its rows in one run.

`refresh.sh` calls this after the engine returns and before it stages anything.
It compares every changed CSV in the mirror repository (a git repository of its
own) against the committed copy and refuses the commit when one of them has lost
more than `--pct` of its rows, so a partial render becomes a skipped tick with a
reason instead of a committed regression.

Why it exists: on 2026-08-13 an hourly rows tick raced a manual `refresh.py` run
(the engine took no lock of its own then — it does now) and committed one of the
largest tables at 57 of 8,682 rows. Nothing in the pipeline objected; the
truncation was caught hours later by a downstream analysis's own row floors. The
mirror's worst failure class is wrong data that looks clean, and a row count is
the cheapest signal that separates the two.

Deliberately narrow, because the penalty for a false positive is a wedged
pipeline — a refusal leaves the mirror dirty, and a dirty tree stops every later
nightly and hourly tick until a human clears it:

  * **Only CSVs, and only ones present in both HEAD and the working tree.** A
    file the run *deleted* is out of scope: the engine deletes a DB directory
    when Notion says the database is gone, which is legitimate and reported, and
    guarding it here would wedge the pipeline on a real upstream deletion. The
    shape being caught is a truncated render, not a removal.
  * **Tables below `--min-rows` are exempt.** A twelve-row DB losing five rows is
    a normal cleanup and crosses any percentage; the check is meaningless there.
  * **A row count, not a diff.** Rows can legitimately change wholesale (a
    re-render, a schema change); losing most of them cannot.

A working-tree CSV that no longer parses counts as a breach: an unreadable table
is exactly the torn write this guard is for, and the previous copy is committed.

    row_floor.py                      # check the configured mirror, one line per breach
    row_floor.py --repo /tmp/sandbox  # or a throwaway mirror repository
    row_floor.py --pct 60             # a looser floor for one run
    row_floor.py --allow-shrink       # accept the shrink (a real mass deletion)

Exit 0 = clean (or nothing changed), 1 = at least one breach, 2 = usage/git error.
Env: NOTION_MIRROR_ROW_FLOOR_PCT, NOTION_MIRROR_ROW_FLOOR_MIN_ROWS,
NOTION_MIRROR_ROW_FLOOR_ALLOW_SHRINK — the same three knobs as the flags, so the
cron entry (or a one-off `refresh.sh` run) can set them without new arguments.
"""
import argparse
import csv
import io
import os
import subprocess
import sys

import mirror_root

# Notion cells hold whole documents; the 128 KB default limit raises _csv.Error
# on the largest tables, which would read as "unparseable" and refuse every run.
csv.field_size_limit(64 * 1024 * 1024)

DEFAULT_PCT = 40.0
DEFAULT_MIN_ROWS = 20


def git(repo, *args):
    """Run git in `repo`, returning stdout as bytes. Raises on a git failure."""
    return subprocess.run(("git", "-C", repo) + args, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def changed_csvs(repo):
    """Paths (repo-relative) of tracked CSVs anywhere in `repo` that differ from HEAD.

    The whole repository, because the repository is the mirror and nothing else.
    `git diff HEAD` covers staged and unstaged changes alike, so it does not
    matter whether the caller has already run `git add`. `-z` because the mirror's
    filenames carry spaces, quotes and emoji, which porcelain output escapes."""
    out = git(repo, "diff", "--name-only", "-z", "HEAD")
    return [p for p in out.decode("utf-8", "surrogateescape").split("\0")
            if p.endswith(".csv")]


def count_rows(text):
    """Data rows in a CSV (header excluded), or None when it does not parse.

    Counted with the csv module rather than by lines: mirrored cells contain
    newlines, so an 8,682-row table spans 65,787 physical lines."""
    try:
        n = sum(1 for _ in csv.reader(io.StringIO(text)))
    except (csv.Error, UnicodeDecodeError):
        return None
    return max(0, n - 1)


def head_text(repo, path):
    """The committed copy of `path`, or None when HEAD has no such file."""
    try:
        return git(repo, "show", f"HEAD:{path}").decode("utf-8", "surrogateescape")
    except subprocess.CalledProcessError:
        return None


def check(repo, pct=DEFAULT_PCT, min_rows=DEFAULT_MIN_ROWS):
    """Breach lines for the changed CSVs, one string per offending table."""
    breaches = []
    for path in changed_csvs(repo):
        full = os.path.join(repo, path)
        if not os.path.exists(full):
            continue  # a deletion: see the module docstring
        before = head_text(repo, path)
        if before is None:
            continue  # new file — no committed count to fall from
        was = count_rows(before)
        if was is None or was < min_rows:
            continue  # no usable baseline, or too small for a percentage to mean anything
        with open(full, encoding="utf-8", errors="surrogateescape") as f:
            now = count_rows(f.read())
        if now is None:
            breaches.append(f"{path}: {was} rows committed, working copy does not parse as CSV")
            continue
        if now < was * (1 - pct / 100.0):
            breaches.append(f"{path}: {was} -> {now} rows "
                            f"({100.0 * (was - now) / was:.1f}% lost)")
    return breaches


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    # Resolved after parsing, not as a default: `--repo /tmp/sandbox` has to keep
    # working on a machine where no mirror resolves, and an argparse default is
    # evaluated whether or not the flag was given.
    ap.add_argument("--repo", default=None,
                    help="the mirror repository (default: the configured mirror)")
    ap.add_argument("--pct", type=float,
                    default=float(os.environ.get("NOTION_MIRROR_ROW_FLOOR_PCT", DEFAULT_PCT)),
                    help="refuse when a table loses more than this %% of its rows")
    ap.add_argument("--min-rows", type=int,
                    default=int(os.environ.get("NOTION_MIRROR_ROW_FLOOR_MIN_ROWS",
                                               DEFAULT_MIN_ROWS)),
                    help="tables smaller than this are exempt")
    ap.add_argument("--allow-shrink", action="store_true",
                    default=os.environ.get("NOTION_MIRROR_ROW_FLOOR_ALLOW_SHRINK") == "1",
                    help="report breaches but exit 0 (a genuine mass deletion)")
    args = ap.parse_args(argv)

    try:
        repo = args.repo or mirror_root.mirror_dir()
    except mirror_root.MirrorError as e:
        print(e, file=sys.stderr)
        return 2

    try:
        breaches = check(repo, args.pct, args.min_rows)
    except subprocess.CalledProcessError as e:
        print(f"row_floor: git failed: {e.stderr.decode('utf-8', 'replace').strip()[:200]}",
              file=sys.stderr)
        return 2
    if not breaches:
        return 0
    for line in breaches:
        print(f"row floor: {line}")
    if args.allow_shrink:
        print(f"row floor: {len(breaches)} table(s) shrank; accepted (--allow-shrink)")
        return 0
    print(f"row floor: refusing to commit — {len(breaches)} table(s) lost more than "
          f"{args.pct:g}% of their rows. The working tree is left as it is: inspect it, "
          f"and if the shrink is real (a mass deletion in Notion) re-run with "
          f"NOTION_MIRROR_ROW_FLOOR_ALLOW_SHRINK=1.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
