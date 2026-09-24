#!/usr/bin/env python3
"""Which uncommitted paths in the mirror repository are the engine's, and staging only those.

The mirror is also a repository people and agents work in — its README, `summaries/`,
a hand-run coverage census — and the refresh used to treat any uncommitted file as a
crashed run: every nightly and every hourly tick refused until someone cleaned up. On
2026-09-22 that was one README edit left uncommitted overnight. What the engine writes is
a fixed set of paths (`ENGINE_PATHS`), so the wrapper can distinguish the two: an
uncommitted engine path still stops a run (it is a crashed or refused run's output), a
path outside them is someone else's and is left alone, unstaged and uncommitted.

  tree_status.py --repo R            one line per dirty path: `engine<TAB>path`,
                                     `foreign<TAB>path` or `staged<TAB>path` (a foreign
                                     change already in the index, which a commit would take)
  tree_status.py --repo R --stage    `git add -A` over the engine's paths only

Stdlib only; asserts nothing at import.
"""
import argparse
import os
import subprocess
import sys

# Everything refresh.sh and refresh.py write that git tracks: the workspace tree (pages,
# rows, CSVs, schemas, attachments, _comments.md), the page metadata, the changelog
# notes and their index, structure.md, and .gitignore (appended for attachments over
# 100 MB). A directory entry ends in "/".
ENGINE_PATHS = ("workspace/", "_meta/pages-metadata.jsonl", "_meta/content-pages.tsv",
                "_meta/changelog/", "structure.md", "CHANGELOG.md", ".gitignore")


def owned(path):
    return any(path == p or (p.endswith("/") and path.startswith(p)) for p in ENGINE_PATHS)


def _git(repo, *args):
    return subprocess.run(("git", "-C", repo) + args, check=True, capture_output=True).stdout


def dirty(repo):
    """[(index_status, worktree_status, path)] for every uncommitted path, untracked
    included; both sides of a rename are reported."""
    out = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    items = out.decode("utf-8", "surrogateescape").split("\0")
    res, i = [], 0
    while i < len(items):
        rec = items[i]
        i += 1
        if len(rec) < 4:
            continue
        x, y, path = rec[0], rec[1], rec[3:]
        res.append((x, y, path))
        if x in "RC":  # the source path follows as its own field
            res.append((x, y, items[i]))
            i += 1
    return res


def classify(repo):
    """-> {"engine": [...], "foreign": [...], "staged": [...]} of paths."""
    out = {"engine": [], "foreign": [], "staged": []}
    for x, y, path in dirty(repo):
        if owned(path):
            out["engine"].append(path)
        elif x not in " ?":
            out["staged"].append(path)
        else:
            out["foreign"].append(path)
    return out


def stage(repo):
    """Stage every change under the engine's paths and nothing else. A pathspec that
    matches nothing is an error to `git add`, so only paths that exist or are tracked
    are passed."""
    specs = [p.rstrip("/") for p in ENGINE_PATHS
             if os.path.exists(os.path.join(repo, p)) or _git(repo, "ls-files", "--", p.rstrip("/"))]
    if specs:
        _git(repo, "add", "-A", "--", *specs)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--stage", action="store_true")
    args = ap.parse_args(argv)
    try:
        if args.stage:
            stage(args.repo)
            return 0
        for kind, paths in classify(args.repo).items():
            for p in paths:
                print(f"{kind}\t{p}")
    except subprocess.CalledProcessError as e:
        sys.stderr.write(f"tree_status: git failed: {e.stderr.decode(errors='replace').strip()}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
