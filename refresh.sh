#!/usr/bin/env bash
# notion-mirror refresh wrapper — cron entrypoint.
#
#   refresh.sh [daily|full-comments]      (default: daily; weekly/monthly = legacy aliases)
#   refresh.sh rows <page-id[,page-id...]|--props-only>
#   refresh.sh notify                     (send the deferred digest of the last run)
#   refresh.sh reanalyze <sha>            (regenerate the changelog note of a committed run)
#   refresh.sh init                       (cold start: create the empty mirror skeleton)
#
# Pipeline: refresh.py (incremental API pull into the mirror) -> if changes:
# stage the mirror -> claude -p writes a reader-relevance-ranked changelog
# note -> CHANGELOG.md index line -> git commit -> ntfy digest (TL;DR).
# The mirror is a git repository of its own (its .git beside workspace/), and
# every git command here runs inside it. Failures ntfy loudly; a failed
# analysis degrades to a stub note so the data commit still lands. No git
# push (set NOTION_REFRESH_PUSH=1 to enable).
#
# rows mode refreshes only the rows it is given (plus a props-probe drain) and
# commits them straight: no changelog call, no CHANGELOG index line, no digest.
# Those three are for a nightly's worth of change; on an hourly tick they would
# be a model call and a phone notification per handful of rows — and whether
# they fired at all would be nondeterministic per tick, since has_changes counts
# report["comments"]["retained"], which moves whenever a comment vanishes.
# `--props-only` names no rows: the drain alone, which is what most hourly ticks
# are. rows mode also never ntfys — every outcome goes to the status marker
# (_meta/state/rows-refresh-status.json) and a health check's dead-man ages it
# (rows_status.py --check).
#
# Where the mirror is: NOTION_MIRROR, in the environment or in
# ~/.config/notion-mirror/env, resolved by mirror_root.py (which refuses a
# directory that is not a built mirror). The same file carries
# NOTION_MIRROR_AUTOMATION_SUBTREES, read by refresh.py.
#
# Env knobs: NOTION_REFRESH_RPS (3.0) · NOTION_REFRESH_BUDGET (mode default)
#            NOTION_REFRESH_MODEL (opus) · NOTION_REFRESH_EFFORT (medium)
#            NOTION_REFRESH_ANALYSIS_TIMEOUT (3600) · NOTION_REFRESH_PUSH (0)
#            NOTION_REFRESH_DEFER_NTFY (0) · NOTION_REFRESH_NTFY_DIGEST (1)
#            NOTION_REFRESH_RESUME (0)
#            NOTION_MIRROR_TOOLS — the ENGINE-CODE root (where refresh.py,
#            rows_status.py, row_floor.py and changelog-prompt.md are read from).
#            Defaults to this script's own directory; a sandbox points it at a
#            stub engine. Cron never sets it: the default — engine beside this
#            script — is the production one, and an override in a cron line
#            would be a wrong answer that never refuses.
#            NOTION_MIRROR_CHANGELOG_CONTEXT — the reader-context file appended
#            to the changelog prompt (default ~/.config/notion-mirror/changelog-context.md).
#            NOTION_MIRROR_ROW_FLOOR_{PCT,MIN_ROWS,ALLOW_SHRINK} — the pre-commit
#            row floor's thresholds and its escape hatch for a genuine mass
#            deletion (row_floor.py). NOTION_MIRROR_LOCK redirects the lock file
#            refresh.py takes; only a test or a rehearsal should ever set it.
#            CLAUDE_CONFIG_DIR — where the Notion token is read from
#            (.claude.json's MCP server env) when NOTION_TOKEN is not set.
set -uo pipefail

MODE="${1:-daily}"
MODEL="${NOTION_REFRESH_MODEL:-opus}"
EFFORT="${NOTION_REFRESH_EFFORT:-medium}"
export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"

say() { echo "[$(date -u -Is)] $*"; logger -t "notion-mirror" "$*" 2>/dev/null || true; }
# A phone notification through a local ntfy server, the same shape the webhook
# receiver uses: bearer token from ~/services/.ntfy-token when present, five
# seconds, and a log line rather than a failure when the server is not there.
ntfy() {
    local prio="$1" title="$2" body="$3" auth=()
    [ -r "$HOME/services/.ntfy-token" ] && auth=(-H "Authorization: Bearer $(cat "$HOME/services/.ntfy-token")")
    curl -sf -m 5 -H "Title: $title" -H "Priority: $prio" "${auth[@]}" -d "$body" \
        "http://localhost:2586/claude-$(whoami)" >/dev/null || say "ntfy failed"
}
fail() {
    say "FAIL: $*"
    ntfy high "Notion mirror refresh failed" "$*"
    exit 1
}

# A preflight refusal, at the volume the caller deserves. The nightly keeps
# fail() — two concurrent nightlies, or a tree it must not touch, is an
# incident. An hourly rows tick is not: losing the race with the nightly is its
# designed outcome (the lock is held 2-2.5h typically, up to ~4.5h, so 2-4 ticks
# a night are refused by construction) and a stray uncommitted mirror edit would
# otherwise produce 24 high-priority notifications a day. So rows mode records
# the reason in the status marker and exits non-zero, silently; the listener is
# a health check's dead-man, which ages the last success in a different cron.
# `skipped` = designed refusal, `failed` = something wrong; both age the clock.
guard_out() {
    local outcome="$1" reason="$2"
    shift 2
    if [ "$MODE" = "rows" ]; then
        say "rows refresh $outcome ($reason): $*"
        python3 "$TOOLS/rows_status.py" --state-dir "$STATE" --outcome "$outcome" --reason "$reason" --detail "$*"
        exit 1
    fi
    fail "$*"
}

command -v python3 >/dev/null || fail "python3 missing"

# --- roots -------------------------------------------------------------------
# Two of them, because code and data are different facts. SELF is where this
# script lives (code); NDIR is where the mirror lives (data), and only the
# resolver may answer that — a dirname climb from here lands in the clone, and
# every writer downstream would silently materialise an empty parallel mirror
# rather than refuse. Everything above this line is defined first on purpose:
# the refusal below has to be able to speak.
SELF="$(cd "$(dirname "$(realpath "${BASH_SOURCE[0]}")")" && pwd)"
TOOLS="${NOTION_MIRROR_TOOLS:-$SELF}"

# --- init: cold-start the mirror layout ---------------------------------------
# Dispatched ABOVE the mirror resolution below, which refuses on a fresh
# directory by design: a directory without workspace/_databases has no mirror to
# refresh yet. `init` is how the first one is bootstrapped. It takes the
# configured path unchecked, requires only that it exists and is writable,
# creates the empty skeleton the resolver and every writer require, and states
# the real cost honestly. No Notion, no token, no lock: it only makes directories.
# A person runs it at a terminal, so its refusals go to that terminal, never to
# the phone.
if [ "$MODE" = "init" ]; then
    refuse() { say "init refused: $*"; exit 1; }
    ierr=$(mktemp)
    if ! NDIR="$(python3 "$SELF/mirror_root.py" --unchecked 2>"$ierr")" || [ -z "$NDIR" ]; then
        msg="$(cat "$ierr")"; rm -f "$ierr"
        refuse "$msg"
    fi
    rm -f "$ierr"
    [ -d "$NDIR" ] || refuse "NOTION_MIRROR names $NDIR, which does not exist — create the directory (or mount the mirror volume) first, then re-run init"
    DBDIR="$NDIR/workspace/_databases"
    if [ -d "$DBDIR" ]; then
        refuse "mirror already initialised: $DBDIR exists — refusing to re-init. Run 'refresh.sh daily' to refresh it."
    fi
    mkdir -p "$DBDIR" "$NDIR/_meta/state" || refuse "could not create the mirror skeleton under $NDIR"
    say "initialised empty mirror skeleton at $NDIR"
    cat <<EOF
Mirror skeleton created:
  $DBDIR
  $NDIR/_meta/state

Next step — make the mirror a git repository of its own (git init there), then
run the first refresh:
  refresh.sh daily

Be honest about the cost: the first run performs FULL discovery over every
database shared with the integration, at Notion's ~3 req/s cap, which takes
hours. Set NOTION_REFRESH_BUDGET to cap a single run's requests and make it
resumable across nights — a budget-cut run stops cleanly and the next one
continues. A partially built mirror is incomplete, and the per-DB tools refuse
against the missing directories until discovery finishes.

--dbs cannot scope this: it is a single substring match that filters only the
validate/refetch probe paths, never discovery, so it cannot narrow a cold-start
refresh to a subset of databases.
EOF
    exit 0
fi

err=$(mktemp)
if ! NDIR="$(python3 "$SELF/mirror_root.py" 2>"$err")" || [ -z "$NDIR" ]; then
    if [ "$MODE" = "rows" ]; then
        # No ntfy and no marker: the marker lives under the root that just refused to
        # resolve, so writing one would either fail or land somewhere wrong. The 6h
        # dead-man is the listener for this, as it is for every other way an hourly
        # tick goes quiet.
        cat "$err" >&2
        rm -f "$err"
        exit 1
    fi
    msg="$(cat "$err")"
    rm -f "$err"
    fail "$msg"
fi
rm -f "$err"
STATE="$NDIR/_meta/state"

# The mirror is a git repository of its own, and every commit below goes into it.
# Its top level has to BE the mirror: a repository *around* it (a checkout that
# tracks the mirror directory) would receive a mirror's worth of churn per run,
# and a bare mount point — the mirror volume not mounted — has no repository at
# all. Checked before any lock is taken and before the token is read, so a wrong
# layout costs nothing and touches nothing.
MIRROR_TOP="$(git -C "$NDIR" rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$MIRROR_TOP" ] || [ "$MIRROR_TOP" != "$(realpath -- "$NDIR")" ]; then
    guard_out failed no_mirror_repo "$NDIR is not its own git repository (top level: ${MIRROR_TOP:-none}) — the mirror must be a repository rooted at that path, with its .git beside workspace/; if the directory is empty, the mirror volume is not mounted"
fi
cd "$NDIR" || guard_out failed no_repo "mirror missing: $NDIR"

# rows mode carries its subject in $2; check it before taking any lock, so a
# usage slip costs nothing and blocks nobody
ROWS_ARG=()
if [ "$MODE" = "rows" ]; then
    if [ -z "${2:-}" ]; then
        say "usage: refresh.sh rows <page-id[,page-id...]|--props-only>"
        exit 2
    fi
    # --props-only names no rows: refresh.py drains the props-probe queue and
    # nothing else. Spelled as a token rather than an empty argument so an
    # unset variable cannot silently become a no-row run.
    [ "$2" != "--props-only" ] && ROWS_ARG=(--rows "$2")
fi

# --- ntfy digest builder (deterministic payload from the note's ntfy JSON ----
# --- line; falls back to scraping TL;DR bullets for stub/legacy notes) --------
# NOTION_REFRESH_NTFY_DIGEST=0 sends no digest at all, for a deployment that reads
# the changelog note itself; failure alerts go through ntfy() and are unaffected.
send_note_ntfy() {
    local note="$1" summary="$2" rel="$3"
    local payload title prio body
    if [ "${NOTION_REFRESH_NTFY_DIGEST:-1}" = "0" ]; then
        say "digest not sent (NOTION_REFRESH_NTFY_DIGEST=0); note at $rel"
        return 0
    fi
    payload="$(python3 - "$note" <<'EOF'
import json, re, sys
txt = open(sys.argv[1]).read()
m = None
for m in re.finditer(r"<!-- ntfy: (\{.*?\}) -->", txt):
    pass
if m:
    try:
        d = json.loads(m.group(1))
        print(json.dumps({"title": str(d.get("title", ""))[:120],
                          "priority": "high" if d.get("priority") == "high" else "default",
                          "body": "\n".join(f"- {b}" for b in (d.get("bullets") or [])[:4])}))
    except (json.JSONDecodeError, AttributeError):
        pass
EOF
)"
    if [ -n "$payload" ]; then
        title="$(python3 -c "import json,sys;print(json.loads(sys.argv[1])['title'])" "$payload")"
        prio="$(python3 -c "import json,sys;print(json.loads(sys.argv[1])['priority'])" "$payload")"
        body="$(python3 -c "import json,sys;print(json.loads(sys.argv[1])['body'])" "$payload")"
    fi
    if [ -z "${title:-}" ]; then
        title="Notion mirror: $summary"
        prio="default"
        grep -q "ntfy-priority: high" "$note" && prio="high"
        body="$(awk '/^## TL;DR/{f=1;next} /^## /{f=0} f && /^- /' "$note" | head -6)"
        [ -z "$body" ] && body="$summary"
    fi
    ntfy "$prio" "$title" "$body
note: $rel"
}

# --- changelog prompt (one builder, both call sites: nightly + reanalyze) -----
# base prompt + optional per-installation reader context + the run-context tail the
# caller passes. The reader-context file names whose lane this installation is
# configured for, in that person's own words; it is absent on a fresh install, where
# the prompt itself falls back to ranking by the reader's open task rows. Written by
# the operator (or the deploy), never by this script. The env override exists for the
# test suite, which must not read or write the real ~/.config path.
CHANGELOG_CONTEXT="${NOTION_MIRROR_CHANGELOG_CONTEXT:-$HOME/.config/notion-mirror/changelog-context.md}"
changelog_prompt() {
    local run_context="$1"
    cat "$TOOLS/changelog-prompt.md"
    if [ -f "$CHANGELOG_CONTEXT" ]; then
        printf '\n---\nReader context — this installation is configured for the person described below:\n\n'
        cat "$CHANGELOG_CONTEXT"
    fi
    printf '\n---\n%s\n' "$run_context"
}

# --- notify: send the deferred digest for the most recent run, then exit -----
# (paired with NOTION_REFRESH_DEFER_NTFY so the run can be scheduled early and
# the phone notification arrives at a useful hour)
if [ "$MODE" = "notify" ]; then
    PEND="$STATE/pending-ntfy.tsv"
    [ -f "$PEND" ] || { say "no pending digest to send"; exit 0; }
    IFS=$'\t' read -r NOTE SUMMARY REL < "$PEND"
    [ -f "$NOTE" ] || { say "pending note missing: $NOTE"; rm -f "$PEND"; exit 0; }
    send_note_ntfy "$NOTE" "$SUMMARY" "$REL"
    rm -f "$PEND"
    say "deferred digest sent"
    exit 0
fi

# --- self-lock ---------------------------------------------------------------
# Distinct file from any lock a cron line takes on purpose: flock conflicts
# on the inode, so re-taking the file an ancestor already holds self-deadlocks.
# With its own file, the script is safe however it is invoked — an outer
# flock and this one compose, and a manual run can no longer race the nightly.
# notify mode (above) deliberately takes no lock. Standalone mirror writers
# (migrations, backfill) take this same file themselves; nothing that *invokes*
# this script may hold it.
mkdir -p "$HOME/.locks"
exec 9>"$HOME/.locks/notion-mirror-internal"
flock -n 9 || guard_out skipped lock_contention "another refresh run holds ~/.locks/notion-mirror-internal — a nightly or manual mirror run is in progress"
# refresh.py takes this same lock itself now — invoking the engine directly used
# to forfeit mutual exclusion silently, which is how a manual run and an hourly
# tick came to render one CSV concurrently on 2026-08-13. It is re-entrant under
# this script and nothing else: the marker names the fd we hold the lock on, and
# the engine believes the claim only after fstat'ing that fd onto the lock file.
# Bash reserves only fds >= 10 for itself, so 9 is inherited by children as-is.
export NOTION_MIRROR_LOCK_FD=9

# --- pre-flight -------------------------------------------------------------
# Every git command below runs in $NDIR, the mirror's own repository, which is a
# different one from the clone this script is committed in. Nothing here may
# reach for the clone, and `cd "$NDIR"` above is what keeps that true for the
# whole run.
git rev-parse --git-dir >/dev/null 2>&1 || guard_out failed no_repo "not a git repo: $NDIR"
[ -f "$(git rev-parse --git-dir)/index.lock" ] && guard_out skipped git_busy "git index.lock present — another git op in flight"
for f in MERGE_HEAD REBASE_HEAD CHERRY_PICK_HEAD; do
    [ -e "$(git rev-parse --git-dir)/$f" ] && guard_out skipped git_busy "git $f in progress — refusing to touch the tree"
done
# The dirty-tree preflight is kept in rows mode, not relaxed: an hourly commit
# over someone else's half-written tree is worse than an hour of staleness. What
# changes is the volume — a skip, not a high-priority alert, since the usual
# cause is a mirror-writing session mid-commit and it clears within minutes. A
# tree that stays dirty (a crashed nightly) stops the hourly job from succeeding
# at all, which is what the dead-man is for.
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    if [ "${NOTION_REFRESH_RESUME:-0}" = "1" ]; then
        say "resume mode: proceeding over a dirty mirror tree (assumed to be a prior run's own writes)"
    else
        guard_out skipped dirty_tree "the mirror has uncommitted changes (manual edits or a crashed run) — commit/stash them, or NOTION_REFRESH_RESUME=1 to continue a crashed run"
    fi
fi

# --- reanalyze: regenerate the changelog note for an already-committed refresh
# (used when the analysis failed or was skipped; writes only the note)
if [ "$MODE" = "reanalyze" ]; then
    SHA="${2:?usage: refresh.sh reanalyze <commit-sha>}"
    git cat-file -e "$SHA^{commit}" 2>/dev/null || fail "no such commit: $SHA"
    DATE_UTC="$(git show -s --format=%cs "$SHA")"
    NOTE="$NDIR/_meta/changelog/$DATE_UTC.md"
    [ -f "$NOTE" ] || fail "no changelog note for $DATE_UTC to replace"
    REL="_meta/changelog/$(basename "$NOTE")"
    SUMMARY="$(git show -s --format=%s "$SHA" | sed 's/^notion refresh ([a-z-]*): [0-9-]* — //')"
    say "reanalyzing refresh $SHA ($DATE_UTC)"
    command -v claude >/dev/null || fail "claude CLI missing"
    ANALYSIS_TIMEOUT="${NOTION_REFRESH_ANALYSIS_TIMEOUT:-3600}"
    PROMPT="$(changelog_prompt "REANALYSIS run context: the refresh being analyzed is already committed as
$SHA (date $DATE_UTC, summary: $SUMMARY). There is NO staged diff — instead of
'git diff --cached', use 'git show --stat $SHA' for shape and
'git diff $SHA^ $SHA -- <path>' for targeted content. The machine run report is
embedded in the existing stub note at $REL — read it first, then write
the replacement note.")"
    if timeout "$ANALYSIS_TIMEOUT" claude -p "$PROMPT" \
        --model "$MODEL" --effort "$EFFORT" \
        --allowedTools "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(git status:*)" \
        < /dev/null > "$NOTE.tmp" 2>>"${TMPDIR:-/tmp}/notion-mirror-claude.err" \
        && [ -s "$NOTE.tmp" ] && head -5 "$NOTE.tmp" | grep -q "^#"; then
        mv "$NOTE.tmp" "$NOTE"
        git add -- "$NOTE" && git commit -q -m "notion changelog: reanalyzed $DATE_UTC refresh ($SHA)"
        send_note_ntfy "$NOTE" "$SUMMARY" "$REL"
        # The run that wrote the stub may have deferred its digest. That queued
        # entry points at the note just replaced and just sent, so leaving it
        # would send the same digest a second time at the next `notify`, against
        # a summary the note no longer matches.
        PENDING="$STATE/pending-ntfy.tsv"
        if [ -f "$PENDING" ] && [ "$(cut -f1 "$PENDING")" = "$NOTE" ]; then
            rm -f "$PENDING"
            say "dropped the deferred digest for this note (sent above)"
        fi
        say "reanalysis committed + digest sent"
        exit 0
    fi
    rm -f "$NOTE.tmp"
    fail "reanalysis produced no usable note (see ${TMPDIR:-/tmp}/notion-mirror-claude.err)"
fi

# An inherited token wins, so a rehearsal can hand this its own credentials.
# Deriving one unconditionally meant a NOTION_MIRROR rehearsal read and wrote the
# production Notion workspace with the production token, whatever throwaway tree
# it committed to. Setting NOTION_MIRROR alone still does: the token has to be
# set too.
if [ -z "${NOTION_TOKEN:-}" ]; then
    NOTION_TOKEN="$(python3 - "$CLAUDE_CONFIG_DIR/.claude.json" <<'EOF'
import json, sys
print(json.load(open(sys.argv[1]))["mcpServers"]["notion"]["env"]["NOTION_TOKEN"])
EOF
)" || guard_out failed no_token "could not extract NOTION_TOKEN from $CLAUDE_CONFIG_DIR/.claude.json"
fi
export NOTION_TOKEN

# --- refresh ----------------------------------------------------------------
say "refresh start: mode=$MODE"
RPS="${NOTION_REFRESH_RPS:-3.0}"
BUDGET_ARG=()
[ -n "${NOTION_REFRESH_BUDGET:-}" ] && BUDGET_ARG=(--budget "$NOTION_REFRESH_BUDGET")
if ! OUT="$(python3 "$TOOLS/refresh.py" --mode "$MODE" --rps "$RPS" "${BUDGET_ARG[@]}" "${ROWS_ARG[@]}")"; then
    guard_out failed refresh_failed "refresh.py exited non-zero (mode=$MODE); see log"
fi
say "refresh.py: $OUT"

# --- pre-commit row floor ----------------------------------------------------
# A render that lost most of a table's rows must not reach a commit: on
# 2026-08-13 a raced hourly tick committed one large CSV at 57 of 8,682 rows and
# nothing in this pipeline objected. Run before anything is staged, so a breach
# leaves the working tree exactly as the engine left it, for inspection.
if ! FLOOR_OUT="$(python3 "$TOOLS/row_floor.py" --repo "$NDIR" 2>&1)"; then
    # The one thing rows mode ntfys about. It is not a designed outcome (losing
    # the lock race is), and it cannot storm: the tree it refuses to commit stays
    # dirty, so the next tick stops at the dirty-tree preflight instead of
    # reaching here. The other modes get their ntfy from fail(), via guard_out.
    if [ "$MODE" = "rows" ]; then
        ntfy high "Notion mirror: row floor tripped" "$FLOOR_OUT"
    fi
    guard_out failed row_floor "$FLOOR_OUT"
fi

# --- rows mode: slim commit, then out ----------------------------------------
if [ "$MODE" = "rows" ]; then
    ROWS_SUMMARY="$(python3 - "$STATE/last-run-report.rows.json" <<'EOF'
import json, sys
r = json.load(open(sys.argv[1]))
rw, pp = r.get("rows") or {}, r.get("props_probe") or {}
bits = [f"{len(rw.get('refreshed') or [])}/{rw.get('requested', 0)} rows"]
if pp.get("drained"): bits.append(f"{pp['drained']} props probes")
if pp.get("deferred"): bits.append(f"{pp['deferred']} deferred")
if rw.get("errors") or pp.get("errors"): bits.append(f"{len(rw.get('errors') or []) + len(pp.get('errors') or [])} errors")
if r.get("budget_exhausted"): bits.append("PARTIAL (budget)")
bits.append(f"{r['requests']} req")
print("; ".join(bits))
EOF
)" || ROWS_SUMMARY="rows refresh"
    if [ -n "$(git status --porcelain)" ]; then
        # Wholesale `git add -A`, not a slim pathspec. A probe writes tracked
        # files outside the row .md: attachments, .gitignore appends for
        # >100MB ones, _schema.json/_schema.md/_ALL-SCHEMAS.md, and a rename on
        # title change. A pathspec commit would leave those dirty and kill that
        # night's nightly on its own preflight. Named hazard: after a crashed
        # nightly this sweeps partial writes into an hourly commit, which makes
        # NOTION_REFRESH_RESUME=1 moot.
        git add -A >/dev/null || guard_out failed git_add_failed "git add failed"
        git commit -q -m "notion refresh (rows): $ROWS_SUMMARY" || guard_out failed git_commit_failed "git commit failed"
        say "committed: $ROWS_SUMMARY"
    else
        say "rows refresh: no changes ($ROWS_SUMMARY)"
    fi
    # The marker the dead-man reads. The verdict comes from the run's own report
    # rather than from this script's exit status, because the two ways a rows run
    # can finish cleanly while having done nothing — every named row refused, or
    # the budget exhausted partway — both exit 0 and leave a clean tree. See
    # rows_status.verdict_from_report.
    python3 "$TOOLS/rows_status.py" --state-dir "$STATE" --from-report "$STATE/last-run-report.rows.json" || exit 1
    exit 0
fi
CHANGES="$(python3 -c "import json,sys;print(json.loads(sys.argv[1]).get('changes') and 1 or 0)" "$OUT")" || CHANGES=0

if [ "$CHANGES" != "1" ]; then
    if [ -n "$(git status --porcelain)" ]; then
        say "no content changes reported, but tree dirty (state/format touch-ups) — committing quietly"
        git add -A >/dev/null
        git commit -q -m "notion refresh ($MODE): housekeeping, no content changes" || true
    fi
    say "no changes — done"
    exit 0
fi

git add -A >/dev/null || fail "git add failed"

# --- summary line for CHANGELOG.md / commit ---------------------------------
SUMMARY="$(python3 - "$STATE/last-run-report.json" <<'EOF'
import json, sys
r = json.load(open(sys.argv[1]))
d, p, c = r["dbs"], r["pages"], r["comments"]
add = sum(x["added_n"] for x in d["changed"]); chg = sum(x["changed_n"] for x in d["changed"])
dele = sum(x["deleted_n"] for x in d["changed"])
bits = []
if d["changed"]: bits.append(f"{len(d['changed'])} DBs (+{add}/~{chg}/-{dele} rows)")
if d["new"]: bits.append(f"{len(d['new'])} new DBs")
if d["deleted"]: bits.append(f"{len(d['deleted'])} DBs gone")
np_, cp = len(p["new"]), len(p["changed"])
if np_ or cp: bits.append(f"{cp} pages changed, {np_} new")
if p["deleted"]: bits.append(f"{len(p['deleted'])} pages deleted")
if c.get("added") or c.get("retained"): bits.append(f"comments +{c.get('added',0)}/~{c.get('retained',0)} resolved-kept")
if r.get("budget_exhausted"): bits.append("PARTIAL (budget)")
bits.append(f"{r['requests']} req · {max(1, r['duration_s'] // 60)}m")
print("; ".join(bits) or "changes")
EOF
)" || SUMMARY="changes"

DATE_UTC="$(date -u +%F)"
NOTE="$NDIR/_meta/changelog/$DATE_UTC.md"
[ -e "$NOTE" ] && NOTE="$NDIR/_meta/changelog/$DATE_UTC-$(date -u +%H%M).md"
mkdir -p "$NDIR/_meta/changelog"

# --- changelog analysis -----------------------------------------------------
# The analysis is the whole point of the pipeline and is a single model call,
# so it is never skipped on a usage guard — silently leaving a stub and an
# unhelpful notification is worse than the marginal usage of one call. If it
# does fail (rate-limited/timeout), the stub fallback + `reanalyze <sha>` cover it.
ANALYZED=0
if command -v claude >/dev/null 2>&1; then
    say "running $MODEL changelog analysis"
    PROMPT="$(changelog_prompt "Run context: mode=$MODE, date=$DATE_UTC (UTC). The refresh's staged diff and
reports are in place; the summary line is: $SUMMARY")"
    if timeout "${NOTION_REFRESH_ANALYSIS_TIMEOUT:-3600}" claude -p "$PROMPT" \
        --model "$MODEL" --effort "$EFFORT" \
        --allowedTools "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(git status:*)" \
        < /dev/null > "$NOTE.tmp" 2>>"${TMPDIR:-/tmp}/notion-mirror-claude.err"; then
        if [ -s "$NOTE.tmp" ] && head -5 "$NOTE.tmp" | grep -q "^#"; then
            mv "$NOTE.tmp" "$NOTE"
            ANALYZED=1
        else
            say "analysis output empty/malformed — falling back to stub note"
        fi
    else
        say "analysis failed/timed out — falling back to stub note"
    fi
fi
if [ "$ANALYZED" != "1" ]; then
    rm -f "$NOTE.tmp"
    {
        echo "# Notion changes — $DATE_UTC ($MODE refresh)"
        echo
        echo "_⚠ Automated analysis unavailable this run (claude -p failed/timed out or CLI missing). Raw refresh report below; re-run with \`refresh.sh reanalyze <sha>\` against this commit._"
        echo
        cat "$STATE/last-run-report.md"
        # a stub note still gets a clearly-labelled ntfy (not masquerading as analysis)
        echo
        echo "<!-- ntfy: {\"priority\": \"default\", \"title\": \"Notion mirror: analysis unavailable\", \"bullets\": [\"$SUMMARY\", \"analysis step did not run — reanalyze when convenient\"]} -->"
    } > "$NOTE"
fi

# --- CHANGELOG.md index -----------------------------------------------------
CHLOG="$NDIR/CHANGELOG.md"
REL="_meta/changelog/$(basename "$NOTE")"
# shellcheck disable=SC2016  # the backticks are markdown, not a command substitution
[ -f "$CHLOG" ] || printf '# Notion mirror changelog\n\nOne line per refresh; details in [_meta/changelog/](_meta/changelog/). Maintained by notion-mirror `refresh.sh` (cron).\n\n' > "$CHLOG"
python3 - "$CHLOG" "$DATE_UTC" "$MODE" "$SUMMARY" "$REL" <<'EOF'
import sys
path, date, mode, summary, rel = sys.argv[1:6]
lines = open(path).read().split("\n")
entry = f"- **{date}** ({mode}): {summary} — [note]({rel})"
idx = next((i for i, ln in enumerate(lines) if ln.startswith("- **")), None)
if idx is None:
    while lines and lines[-1] == "":
        lines.pop()
    lines += ["", entry, ""]
else:
    lines.insert(idx, entry)
open(path, "w").write("\n".join(lines))
EOF

# --- commit -----------------------------------------------------------------
git add -A >/dev/null
git commit -q -m "notion refresh ($MODE): $DATE_UTC — $SUMMARY

Automated mirror refresh; changelog note at $REL." || fail "git commit failed"
say "committed: $SUMMARY"
if [ "${NOTION_REFRESH_PUSH:-0}" = "1" ]; then
    if git push >/dev/null 2>&1; then
        say "pushed"
    else
        say "push failed (non-fatal)"
    fi
fi

# --- ntfy digest --------------------------------------------------------------
# The run itself can be scheduled early (low interference) while the digest is
# sent later (useful arrival time): NOTION_REFRESH_DEFER_NTFY=1 records the note
# for a subsequent `refresh.sh notify` to send.
if [ "${NOTION_REFRESH_DEFER_NTFY:-0}" = "1" ]; then
    printf '%s\t%s\t%s\n' "$NOTE" "$SUMMARY" "$REL" > "$STATE/pending-ntfy.tsv"
    say "digest deferred (pending-ntfy.tsv) — will send on the next 'notify' run"
else
    send_note_ntfy "$NOTE" "$SUMMARY" "$REL"
fi
say "done (analyzed=$ANALYZED)"
