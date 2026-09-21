# notion-mirror

A full-fidelity local mirror of a Notion workspace, kept as a git repository of its own: every database as a CSV plus one markdown file per row, every content page as markdown with its comments and attachments, refreshed incrementally so the diffs stay meaningful. A daily cron run refreshes the mirror, has a model write a changelog note ranked by what matters to the mirror's reader, commits, and sends a phone digest. A webhook receiver captures comment threads within a minute of their creation, before they can be resolved and vanish from the API. `notion_core/` is the shared bottom layer (Notion → markdown, markdown → Notion, property flattening, the paced HTTP client), importable on its own by other tools that read or write the same repository.

Stdlib Python 3.12 and bash; no packages. `NOTION_TOKEN` comes from the environment or from Claude's Model Context Protocol (MCP) configuration (`.claude.json`); the changelog step needs the `claude` CLI, everything else does not.

- [How-to guides](#how-to-guides) — set a mirror up, run it, recover from the refusals it makes on purpose.
- [Reference](#reference) — configuration, commands, layout, the module surface other tools import.
- [Design notes](#design-notes) — why the safety checks are shaped the way they are, and what each one can and cannot see.

## How-to guides

### How to set up a mirror

1. Clone this repository anywhere; it is code, never data. The mirror is a separate directory.
2. Name the mirror directory. Either export `NOTION_MIRROR=/path/to/mirror`, or write `NOTION_MIRROR=/path/to/mirror` to `~/.config/notion-mirror/env` (cron lines and systemd units read the file; a shell reads either).
3. Create the directory (or mount the volume), then `./refresh.sh init`. It makes `workspace/_databases/` and `_meta/state/` and refuses if the mirror already exists.
4. Make the mirror a git repository: `git -C /path/to/mirror init`, and ignore its run state — `_meta/state/` must be in the mirror's `.gitignore`, or every marker write would dirty the tree the preflight guards.
5. Create a Notion internal integration and share it with read access to the pages and databases you want mirrored: [https://developers.notion.com/docs/create-a-notion-integration](https://developers.notion.com/docs/create-a-notion-integration).
6. Put the token where the wrapper reads it: `NOTION_TOKEN` in the environment, or Claude's `.claude.json` under `CLAUDE_CONFIG_DIR` (default `~/.claude`) with the Notion MCP server's `env.NOTION_TOKEN`.
7. If the workspace has machine-generated subtrees (bot-written delta logs, recordings), name them: `NOTION_MIRROR_AUTOMATION_SUBTREES=Hub/Automations:Other/Logs` in the env file. Those prefixes get a slower comment scan instead of starving the human corpus.
8. Run the first refresh: `./refresh.sh daily`. It performs full discovery over every database shared with the integration at ~3 req/s, which takes hours; set `NOTION_REFRESH_BUDGET` to cap one run and let the next continue.

A wrong `NOTION_MIRROR` never produces an empty parallel mirror: `mirror_root.py` refuses a directory without `workspace/_databases` and says what it tried, where the value came from and what to change.

### How to run the refresh from cron

Two lines: the run early, the digest at a useful hour. The daily run takes an outer lock so a manual run cannot overlap a scheduled one (the script takes its own inner lock too, and the two compose); `notify` runs unlocked, since it only sends the digest the last run left behind, and a tick skipped while the nightly still held the lock would let the next run overwrite that digest unsent.

```cron
NOTION_MIRROR=/path/to/mirror
0 3 * * * user NOTION_REFRESH_DEFER_NTFY=1 flock -n ~/.locks/notion-mirror /path/to/notion-mirror/refresh.sh daily >> ~/.local/state/notion-mirror.log 2>&1
0 8 * * * user /path/to/notion-mirror/refresh.sh notify >> ~/.local/state/notion-mirror.log 2>&1
```

Do not hold `~/.locks/notion-mirror-internal` around the invocation: that is the script's own file, `flock` conflicts on the inode, and a parent holding it makes the script report "a mirror run is in progress" about itself.

### How to run the webhook receiver

`webhook_receiver.py` listens on `127.0.0.1:8098` and expects a public reverse proxy or tunnel in front of it. A systemd unit needs `NOTION_MIRROR` (or the env file under the unit user's home) and `CLAUDE_CONFIG_DIR` when the token comes from `.claude.json`; never put `NOTION_TOKEN` in `Environment=`, since unit files are world-readable. `Restart=on-failure` does not reload on file change — restart the unit after editing the receiver, and roll back by reverting the file and then restarting.

To subscribe: in the integration's settings create a webhook subscription pointing at the public URL; the receiver stores the verification token it receives at `_meta/state/webhook-secret` and ntfys it; complete verification in Notion's UI; select all event types (unknown ones are logged harmlessly).

The receiver refuses to start when `NOTION_MIRROR` does not resolve, with the resolver's message in the journal rather than a traceback.

### How to refresh a handful of rows

```bash
./refresh.sh rows <id>,<id>       # these rows + a props-probe drain, slim commit
./refresh.sh rows --props-only    # the drain alone, no rows named
```

Rows mode never ntfys: every outcome, including a refused tick, lands in `_meta/state/rows-refresh-status.json`, and the only listener is `rows_status.py --check` run from a health check in a different cron. Get one success on the clock before wiring the check, or it alarms immediately and correctly.

```bash
python3 rows_status.py --check           # exit 1 + one line per alarm; silent and 0 when healthy
python3 rows_status.py --outcome ok      # hand-clear the clock after a fixed outage
```

A scheduler that resolves which rows to refresh belongs to whatever tool owns the rows; this repository ships the wrapper and the marker.

### How to recover from a refused run

- **The mirror has uncommitted changes.** Every later nightly refuses at the preflight and every hourly tick records `skipped dirty_tree` until a human clears the tree. Read `_meta/state/last-run-report.md` (or `last-run-report.rows.md`) first: a dirty tree after a crash is a partial run, after a row-floor or contamination refusal it is the suspect write, left for inspection. Commit, stash or revert it; `NOTION_REFRESH_RESUME=1 ./refresh.sh daily` continues over a crashed run's own writes.
- **Row floor tripped** (`row floor: refusing to commit — N table(s) lost more than 40% of their rows`). Inspect the CSV named in the message. If the shrink is real (a mass deletion in Notion), `NOTION_MIRROR_ROW_FLOOR_ALLOW_SHRINK=1 ./refresh.sh daily` accepts it and still logs it. If it is a torn render, revert the tree.
- **Comment-contamination breach.** One comment text is attributed to more than 30 pages. If it is genuine repetition (judging boilerplate posted onto many rows), raise `MAX_COMMENT_PAGES` rather than baselining the text; `--write-baseline` exists for a real pre-existing mess and a recorded breach still fails if it spreads.
- **The changelog note is a stub** (`claude -p` failed or timed out). `./refresh.sh reanalyze <sha>` regenerates the note for that commit and sends its digest.
- **Two writers.** `refresh.py` exits 3 without writing when another writer holds `~/.locks/notion-mirror-internal`; wait for it, or run through `refresh.sh`.
- **The mirror volume is not mounted.** Every writer refuses at import with the resolver's message; the dead-man reports one line, `mirror alarms unavailable: …`.

### How to change the renderer

1. Make the change in `notion_core/walker.py` or `notion_core/richtext.py`.
2. `python3 capture_fixtures.py expected` rewrites every golden file; read that diff line by line — it is the exact list of what the next full run will rewrite across the mirror.
3. `NOTION_MIRROR=/path/to/mirror python3 -m unittest discover tests`, then `python3 refresh.py --mode validate --refetch` against live rows. `tests/README.md` has the fixture mechanics and the deliberate-break drill.

The parser in `notion_core/md_blocks.py` inverts the renderer; a rendering change it stops recognising fails `tests/test_md_blocks.py` rather than a live page.

## Reference

### Configuration

`~/.config/notion-mirror/env` is a `KEY=VALUE` file (`#` comment lines, a leading `export `, quotes around values and a leading `~` in a path are tolerated, so a file a shell could `source` reads the same here; a trailing comment on a value line does not). `~/.config/notion-mirror/env.d/*.env` are read after it in sorted order and the last assignment wins; that directory is how a second deployer ships a value beside the box's own file. A missing file is an ordinary state, but an `env.d` entry that is a symlink to a missing target refuses (`MirrorError` naming the link and its target): a deploy that linked the file before its target existed must not read as "no values". The environment wins over both files; an empty environment value counts as unset.

| Key | Meaning |
|---|---|
| `NOTION_MIRROR` | The mirror directory. Resolved to its realpath; must hold `workspace/_databases` (except for `init`). |
| `NOTION_MIRROR_AUTOMATION_SUBTREES` | Colon-separated `workspace/`-relative prefixes of machine-generated subtrees. The rolling comment scan gives them 10% of its budget. Empty when unset. |

`refresh.sh` reads the following from the environment only (never the file), each with the default shown:

| Variable | Default | Meaning |
|---|---|---|
| `NOTION_MIRROR_TOOLS` | the script's own directory | Where `refresh.py`, `rows_status.py`, `row_floor.py` and `changelog-prompt.md` are read from. For sandboxes; cron never sets it. |
| `NOTION_MIRROR_CHANGELOG_CONTEXT` | `~/.config/notion-mirror/changelog-context.md` | Reader context appended to the changelog prompt: whose lane this installation ranks for, in that person's words. Absent on a fresh install. |
| `NOTION_MIRROR_ROW_FLOOR_PCT` | `40` | A changed CSV that lost more than this percentage of its rows refuses the commit. |
| `NOTION_MIRROR_ROW_FLOOR_MIN_ROWS` | `20` | Tables smaller than this are exempt from the floor. |
| `NOTION_MIRROR_ROW_FLOOR_ALLOW_SHRINK` | unset | `1` accepts a breach (still logged). |
| `NOTION_MIRROR_LOCK` | `~/.locks/notion-mirror-internal` | The engine's lock file. Tests and rehearsals only. |
| `NOTION_REFRESH_RPS` | `3.0` | Requests per second. |
| `NOTION_REFRESH_BUDGET` | per mode | Request cap for one run; a budget-cut run stops cleanly and the next continues. |
| `NOTION_REFRESH_COMMENT_BUDGET` | `4000` | Requests for the rolling comment shard. |
| `NOTION_REFRESH_COMMENT_BUDGET_WEBHOOK` | `1500` | The shard's budget while webhook events are flowing. |
| `NOTION_REFRESH_QUEUE_BUDGET` | `max(1000, budget // 3)` | Cap on the webhook probe queue per run. |
| `NOTION_REFRESH_AUTOMATION_WALK_CAP` | `40` | Per-page request cap inside the automation subtrees. |
| `NOTION_REFRESH_MODEL` | `claude-sonnet-5` | Model for the changelog analysis. |
| `NOTION_REFRESH_EFFORT` | `xhigh` | Its `--effort`. |
| `NOTION_REFRESH_ANALYSIS_TIMEOUT` | `3600` | Seconds before the analysis is abandoned for a stub note. |
| `NOTION_REFRESH_DEFER_NTFY` | `0` | `1` records the digest for a later `notify` run instead of sending it. |
| `NOTION_REFRESH_PUSH` | `0` | `1` pushes the mirror repository after the commit. |
| `NOTION_REFRESH_RESUME` | `0` | `1` proceeds over a dirty tree (a crashed run's own writes). |
| `NOTION_TOKEN` | from `$CLAUDE_CONFIG_DIR/.claude.json` | The integration token. |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Where `.claude.json` is read from. |

Notifications go to a local ntfy server at `http://localhost:2586/claude-<user>`, with a bearer token from `~/services/.ntfy-token` when that file exists; a failed send is logged and never fatal.

### `refresh.sh`

```
refresh.sh [daily|full-comments]        default daily; weekly/monthly are legacy aliases
refresh.sh rows <id[,id...]|--props-only>
refresh.sh notify                       send the deferred digest of the last run
refresh.sh reanalyze <sha>              regenerate the changelog note of a committed run
refresh.sh init                         create the empty mirror skeleton
```

Preflight, in order: the mirror resolves (`mirror_root.py`); the mirror directory is the top level of its own git repository; the self-lock (`~/.locks/notion-mirror-internal`, `flock -n`); no `index.lock`, merge, rebase or cherry-pick in flight; a clean tree. Then the token, the engine, the row floor (`row_floor.py --repo <mirror>`), and in `daily`/`full-comments` the changelog analysis from inside the mirror (`claude -p` with `changelog-prompt.md` plus the reader context), a `CHANGELOG.md` index line, the commit, the digest. `rows` commits with `git add -A` and no analysis, index line or digest, then writes the status marker from the run's own report.

Refusals: the nightly modes route through `fail()` (a high-priority ntfy, exit 1). Rows mode routes through `guard_out`, which records `skipped` (a designed refusal: the lock, a dirty tree, a git operation in flight) or `failed` (anything else) in the marker and exits 1 silently — except a row-floor breach, which ntfys in every mode. A refused mirror resolution in rows mode writes no marker, since the marker lives under the root that did not resolve.

### Python tools

| Piece | What it does |
|---|---|
| `refresh.py` | The engine. Full per-DB row sweep every run (`/search` misses whole databases, so per-DB `/query` is the only reliable change and deletion source). Sweeps stay complete past Notion's 10,000-results-per-query cap: `Api.query_rows` windows by `created_time` (ascending sort, `on_or_after` the boundary, id-dedupe on the tie) per [Notion's guide](https://developers.notion.com/guides/data-apis/query-large-data-sources), and an unwindowable truncation (a >10k `created_time` tie) raises `Truncated` so an incomplete set never reaches the deletion diff. CSVs re-render preserving existing column and row order; per-row `last_edited_time` state triggers body and comment re-probes only for changed rows (sparse feed DBs — under 5% enriched rows — probe only rows that already carry enrichment). Content pages via a full `/search` sweep every run, deletions verified with a GET before removal, changed pages re-walked with comment harvest and attachment download in one pass. Schema sweep every run. Rolling comment shard every run; `--mode full-comments` is the manual whole-corpus sweep that also rebuilds `_comments.md`. Honours `Retry-After`; per-run request budget with a persisted overflow queue capped at `NOTION_REFRESH_QUEUE_BUDGET`. The run report records per-phase request cost. State in `_meta/state/` (gitignored). Modes: `daily`, `full-comments`, `rows` (`--rows`), `place`, `validate` (`--refetch`, `--refetch-sample`, `--dbs`), `contamination-check` (`--write-baseline`); `--dry-run`, `--rps`, `--budget`. |
| `mirror_root.py` | The resolver: where the mirror is, and the refusal when it is not there. See [Module surface](#module-surface-for-other-tools). |
| `paths.py` | `NOTION`, `WS`, `DBS`, `META`, `STATE` — the engine's roots, resolved and asserted at import. Importing any engine module asserts the mirror. |
| `rows_status.py` | The rows-mode status marker: writer (`--outcome`, `--from-report`) and reader (`--check`, `--max-age-hours`, `--state-dir`). |
| `row_floor.py` | The pre-commit row floor: counts the rows of every changed CSV in the mirror repository against the committed copy and exits 1 when one lost more than `--pct` (40) of them; tables under `--min-rows` (20) are exempt; `--allow-shrink` reports and exits 0; `--repo` defaults to the configured mirror. Counts with the csv module, not by lines, since mirrored cells hold newlines. |
| `webhook_receiver.py` | Subscription handshake, HMAC-validated event intake into `_meta/state/webhook-events.jsonl` (rotated at 32 MB, 3 segments), comment capture into `_meta/state/webhook-comments-capture.jsonl`, priority-page and props-probe queueing. Shares `refresh.Api`. Abandoned captures (a permanent 4xx, or six exhausted attempts) ntfy at high priority, bucket-cooled to one message an hour with a suppressed count. |
| `notion_core/` | The shared bottom layer: `api` (the paced, budgeted, `Retry-After`-honouring HTTP client), `richtext` + `walker` (Notion → markdown, at the span and block tiers), `md_blocks` (markdown → Notion), `flatten` (property values → cell strings, plus the 25-item expansion), `rowmd` (the row file's marker, region delimiters and comment-bullet trailer), `runcfg` (per-mode request budgets, the `--rows` parser), `util`. Imports nothing above itself — not `refresh.py`, not `paths.py` — so a caller can borrow one regex without a mounted mirror; `tests/test_notion_core_layering.py` reads the source to keep it that way. |
| `coverage_census.py` | Referenced-but-absent ids → `_meta/coverage/census.json` (a work list); `--report`, `--exclude ID --reason R [--note TEXT]`, `--out`. |
| `coverage_backfill.py` | Closes the census's to-backfill set with the engine's own capture code; `--dry-run`, `--budget N`, `--slice N`, `--only <db-id>`, `--report`. Takes the writer lock; never commits. |
| `migrate_comment_ids.py` | Stamps comment ids onto bullets already on disk; dry-run by default, `--apply`, `--refetch --budget N`, `--report`, `--include-comments-md`. |
| `merge_backfill.py`, `backfill_resolved_comments.py`, `repair_backfill_attribution.py` | Standalone comment-record writers over the capture log; each takes the writer lock. |
| `capture_fixtures.py` | Renderer fixtures: `capture` (live, ~1 rps, read-only, strips signed-URL credentials, never overwrites), `synthetic` (`--force` rewrites every hand-authored entry), `expected` (re-renders the golden output), `trim`. |
| `changelog-prompt.md` | The changelog-analysis prompt, addressed to a model running inside the mirror repository: it reads the run report, the staged diff, `../tasks/.sync/ls.md` when a task-tracking checkout sits around the mirror, and the appended reader context; ranks by the reader's lane, compresses the rest, flags mirror-health issues. |
| `notion_walk.py` | The first build's page walker; `walker.py` cites it as the compatibility reference for the rendered form. |
| `tests/` | Offline, stdlib `unittest`, zero API requests. `tests/README.md`. |

Manual operations, from the repository root with `NOTION_MIRROR` set:

```bash
python3 refresh.py --mode daily                      # everything incremental
python3 refresh.py --mode full-comments              # manual full comment sweep (hours)
python3 refresh.py --mode validate --dbs "Meetings"  # property-table regression check
python3 refresh.py --mode validate --refetch --dbs "Tasks"   # body+comment renderer, live
python3 refresh.py --mode contamination-check        # comment-spread assert, offline, no token
python3 refresh.py --mode rows --rows <id>,<id>      # just these rows + a props-probe drain
python3 coverage_census.py                           # coverage gap -> _meta/coverage/census.json
python3 coverage_census.py --out /tmp/c.json && python3 coverage_census.py --report --out /tmp/c.json
                                                     # the nightly's coverage assert, by hand
python3 coverage_backfill.py --dry-run               # what the backfill would fetch
python3 coverage_backfill.py --budget 4000           # fetch a budgeted slice of it
python3 migrate_comment_ids.py                       # id-stamp coverage over row bullets, no writes
python3 migrate_comment_ids.py --apply --refetch --budget 3000
python3 row_floor.py                                 # would the current tree be allowed to commit?
python3 rows_status.py --check                       # the dead-man
python3 mirror_root.py                               # where the mirror is (exit 2 + the refusal otherwise)
NOTION_MIRROR=/path/to/mirror python3 -m unittest discover tests
```

### Layout of the mirror

```
<mirror>/
  workspace/                  pages as <Title> <id32>.md; _comments.md; attachments
    _databases/<Title> <id32>/   _schema.json, _schema.md, <Title> <id32>.csv, one .md per row,
                                 <Row> <id32>/ for a row's child pages
    _unplaced/                pages whose parent chain could not be resolved yet
  _meta/
    pages-metadata.jsonl, content-pages.tsv, structure.md
    changelog/YYYY-MM-DD.md   one note per refresh
    coverage/census.json, coverage/exclusions.json   tracked
    state/                    gitignored run state: last-run-report.{json,md}, last-run.json,
                              rows-refresh-status.json, users.json, probe-queue.json,
                              props-probe-queue.json, webhook-secret, webhook-events.jsonl,
                              webhook-comments-capture.jsonl, webhook-capture-offset.json,
                              webhook-priority-pages.json, pending-ntfy.tsv, ...
  CHANGELOG.md                one index line per refresh
```

### Module surface for other tools

`mirror_root.py` is stdlib-only and asserts nothing at import:

- `ENV = "NOTION_MIRROR"`, `ENV_FILE = ~/.config/notion-mirror/env`, `ENV_DIR = ~/.config/notion-mirror/env.d`.
- `config(key, env=None) -> str | None` — the environment, else the last `KEY=VALUE` across the env file and `env.d/*.env`.
- `mirror_dir(env=None) -> str` — the realpath of `NOTION_MIRROR`; raises `MirrorError` (a `RuntimeError` carrying the full refusal: resolved as, missing, fix, and the `refresh.sh init` hint when the directory exists without `workspace/_databases`).
- `state_dir(root=None) -> str` — `<mirror>/_meta/state`.
- `CAPTURE = "webhook-comments-capture.jsonl"`, `ROWS_STATUS = "rows-refresh-status.json"`, `USERS = "users.json"`, `WEBHOOK_SECRET = "webhook-secret"` — bare filenames under `state_dir()`.
- `main(argv)`: bare prints `mirror_dir()`; `--unchecked` prints the configured value without the fingerprint; exit 2 with the refusal on stderr.

`rows_status.py` (also asserting nothing at import) exports `OK`, `SKIPPED`, `FAILED`, `MAX_AGE_H` (6), `path(state_dir=None)`, `load(state_dir=None)`, `record(outcome, reason="", detail="", *, state_dir)`, `verdict_from_report(report)` and `check(max_age_h=MAX_AGE_H, state_dir=None) -> list[str]`. The tasksync tool composes its own dead-man on top of this module and imports exactly `check`, `record`, `path`, `verdict_from_report`, `OK` and `FAILED` (its tests also read `load`); a rename here breaks its suite, which is the drift detection.

### Rows mode and its status marker

`refresh.sh rows <id[,id...]>` refreshes exactly the rows it is given and nothing else, then drains the props-probe queue under a cap shared with the row refresh, so a burst of property edits cannot turn an hourly tick into an unbounded run. It does not reuse the probe-queue phase (which folds `webhook-priority-pages.json` into the persisted queue, drains all of it and clears `webhook-db-events.json`); every named row is probed regardless of the enriched-only policy; it writes `last-run-report.rows.{json,md}` rather than the nightly's `last-run-report.*`; it persists nothing but `comment-rows.json` and any database it noticed inside a row body (`pending-discovery.json`, folded into the next nightly's discovery).

The guaranteed staleness window for rows refreshed this way is about six hours: the nightly holds the writer lock for ~2–2.5h typically and up to ~4.5h including the in-lock changelog call, `flock -n` is non-blocking, so 2–4 consecutive hourly ticks are refused every night by construction, up to 5 on a bad one. Comment-only edits do not move `last_edited_time` and reach the mirror through the webhook capture and the rolling scan instead.

Rows mode never ntfys, and that is the whole design: routing contention through `fail()` would send up to five high-priority notifications a night for working correctly, and one stray uncommitted mirror edit would send 24 a day. Every refusal is recorded in `_meta/state/rows-refresh-status.json` (gitignored, so the marker can never dirty the tree it guards) and the job exits non-zero silently; the dirty-tree preflight is kept, not relaxed.

The marker is the whole alerting surface, and `rows_status.py --check` is its only reader. It keys on the age of the last **success** (`--max-age-hours`, default 6), never on the last exit status, because there is no wall-clock bound on `refresh.sh`: a hung nightly holds the lock indefinitely and every tick behind it exits non-zero from `flock -n`, which is not the job failing. Two verdicts are downgraded to failures inside `verdict_from_report` because both otherwise exit 0 and leave a clean tree: a run that named rows and refreshed none of them (`all_rows_refused`), and a run that exhausted its budget partway and left the tail stale (`budget_exhausted`). The clock measures freshness, not cron liveness: an interactive rows run advances it exactly as a tick does.

### Reading a rendered block

Markdown has no spelling for most of what Notion holds, so the renderer marks the blocks that would otherwise read as something they are not:

| Mark | Block | Line |
|---|---|---|
| `- 📄` | sub-page | `- 📄 **Title** — sub-page \`<id32>\`` |
| `- 🗄️` | child database | `- 🗄️ **Title** — database \`<id32>\` …` |
| `- 🔗` | link to a page | `` - 🔗 link to `<id32>` `` |
| `- 🔖` | bookmark | `- 🔖 [caption or url](url)` |
| `- 🖼️` | embed | `- 🖼️ [caption or url](url)` |
| `- 👁️` | link preview | `- 👁️ [url](url)` |
| `- 📑` | table of contents | `- 📑 (table of contents)` |
| `- 🧩` | template | `- 🧩 (template: …)` |

A bare `[url](url)` in the mirror is prose, not a block. Media keeps its own spelling (`![caption](url)` external, `![caption](ATTACH:<id32>)` Notion-hosted). A code block's caption renders as an italic line under the fence, the one rendering a reader cannot tell from ordinary text.

### Row-body grammar

A DB row's `.md` is the property table, the `<!-- body+comments fetched -->` marker, then at most two regions in this order:

```
## Body

<!-- notion:body -->
- a top-level block
  - one level of nesting
<!-- notion:/body -->

## Comments

<!-- notion:comments -->
- _Someone (2026-01-01):_ a comment
<!-- notion:/comments -->
```

Bodies are walked at indent 0: leading spaces mean nesting depth and nothing else. `Walker.render` prefixes only the first physical line of a block — `paragraph`, `code` and `equation` split their text and prefix every line, but `bulleted_list_item`, `numbered_list_item`, `to_do`, `toggle`, `quote` and the headings emit the whole thing in one append — so a shift+enter inside any of those six leaves its continuation at column 0; treat a column-0 line following a deeper one as a continuation. Readers resolve the delimiters first and fall back to the headings only for files that predate them.

Line 2 of every row `.md` says the file is generated, stamped with the row's own `last_edited_time` (not the run time, so `render_row_md` stays a pure function of the page and unchanged rows are never rewritten):

```
<!-- notion:generated row_last_edited=2026-08-01T12:34:00.000Z | generated file: edits here are overwritten on the next mirror run. If tasks/ holds a folder for this row, tasks/<slug>/task.md is the working copy. -->
```

Every comment bullet carries its comment id in a trailing HTML comment on its first line, one of three forms; a task-tracking tool that syncs comments parses exactly these:

```
<!-- notion:cid <comment_id32> d=<discussion_id32> -->   id and discussion known
<!-- notion:cid <comment_id32> -->                       id known, discussion not
<!-- notion:cid legacy -->                               no recoverable id
```

### `notion_core/md_blocks.py` — the write direction

`md_blocks(md)` returns `(visible blocks, blocks for the Details toggle or None)`; `lines_blocks(md)` returns one flat list; `chunks(blocks)` splits either into batches Notion accepts in one request. Indentation is nesting, two spaces per level, as the renderer writes it; Notion takes two levels of nested children per request, so a deeper tree raises. It refuses rather than degrades: `Refused` carries `.line_no`, `.line` and `.reason` and aborts the body. The refused classes are blocks that survive the render only as a stand-in line (sub-pages, databases, page links, url-only blocks carrying no keep-sentinel, Notion-hosted media without one, `![](url)`), the mirror's own diagnostic comments, nesting past two levels, a keep-sentinel that has drifted off its line, an unclosed region sentinel, and a keep-sentinel inside a quote or callout region. Hand it the body region only.

Keep-sentinels (`<!-- notion:keep block=<id32> type=<t> -->`) immediately precede the line a block rendered; `standin_extent(kind, lines, j)` says how many lines that type renders as. Region sentinels (`… type=<t> -->` … `<!-- notion:keep-end block=<id32> -->`) bracket a `column_list` or `synced_block`, whose markdown is otherwise indistinguishable from content. Sentinelled `bookmark` and `embed` are rebuilt from the line (URL and caption); a sentinelled `link_preview` is kept, the API having no way to create one. Externally hosted media (`![cap](https://…)`) converts to a media block; the caption slot carries the block type when it holds one of `image`/`file`/`pdf`/`video`/`audio`.

`escape_md`/`unescape_md` are the pair `rich_md` calls before it wraps a run in any marker, so literal `*`, `` ` ``, `~~`, `<u>`, `[text](url)` round-trip as characters; a code span is escaped in neither direction. The emphasis patterns implement CommonMark's flanking rule. A link goes around a run's trimmed core. Two sibling paragraphs are separated by a blank line so a push does not merge them; inside a quote or callout the separator is off. Three conversions remain silent and byte-stable: an inline `$expr$` becomes literal text, a code block's caption becomes a paragraph, and a paragraph whose text starts with `- `, `# `, `> `, `|` or `---` comes back as the block that marker names.

## Design notes

### About the safety checks

**One writer at a time, enforced in the engine.** `refresh.py` takes `~/.locks/notion-mirror-internal` itself for every mode that writes and exits 3 without writing anything when another writer holds it. It used to rely on `refresh.sh` for that, which meant running the engine directly forfeited mutual exclusion silently — and on 2026-08-13 a manual re-pull of one large table did exactly that, the hourly rows tick fired mid-render, and the tick committed the CSV at 57 of 8,682 rows. `refresh.sh` still takes the lock, because the commit belongs inside the same critical section, so the engine is re-entrant under the wrapper and nothing else: the wrapper exports `NOTION_MIRROR_LOCK_FD=9` beside its `flock -n 9`, and the engine believes the claim only after `fstat`-ing that fd onto the lock file. The lock is held on a bare fd, not a Python file object, since a flock is released the moment its file object is garbage-collected. Standalone writers (`coverage_backfill.py`, the `migrate_*` scripts, `merge_backfill.py`, `repair_backfill_attribution.py --apply`) take the same file themselves. `validate` and `contamination-check` do not lock: they read.

**The pre-commit row floor** is the second half of the same fix: the truncated commit was caught hours later by a downstream analysis's own row floors, never by the mirror. It is deliberately narrow, because a false positive costs more than one run — the refusal leaves the tree dirty, and the dirty-tree preflight then stops every nightly and every hourly tick until a human clears it. So: only CSVs; only ones present in both HEAD and the working tree, since the engine legitimately deletes a DB directory when Notion says the database is gone; only tables of 20 rows or more. A working copy that no longer parses as CSV counts as a breach. This one refusal ntfys even in rows mode: it is not a designed outcome, and it cannot storm, because the dirty tree it leaves stops the next tick at the preflight.

**The comment-contamination assert.** Every `daily` / `full-comments` / `rows` run rebuilds the whole comment index (both tiers: row `.md` files and `_comments.md`) and fails if any single comment text is attributed to more than `MAX_COMMENT_PAGES` (30) pages. It is the regression check for a backfill-misattribution incident where an ancestor-context bug put one discussion on up to 2,287 pages; a task-scoped check could not see that shape, because a foreign thread landing on one row looks like one comment. The constant is calibrated against a live corpus rather than guessed: genuine repetition (judging-criteria boilerplate on the rows of one database) topped out at 26 pages, and 30 is the bottom edge of the empty band above it. It was briefly 45 because events vary in size (one had 106 project rows), so the same boilerplate on 31–45 rows fires at 30; if that happens, raise the constant rather than baselining the text. The assert runs last, in `main()`'s `finally`, after every state write, and is wrapped so a scan that could not run records the reason and still exits non-zero. Text is a weak key — `comment_text_key` drops mention-only comments, since every mention renders as `‣` — and id-keyed comments retire the guesswork properly.

**What a breach costs.** A failing run exits non-zero before `refresh.sh` stages anything, so the suspect write stays uncommitted. The nightly is loud (a high-priority ntfy); the hourly job is not (the marker, and the next nightly failing its dirty-tree preflight, which pages about a dirty tree rather than about contamination). `--dry-run --mode daily` reports a breach and returns 1 too, writing nothing.

### About the comment model

Comments do not bump a page's `last_edited_time` and the API has no global comments feed, so full freshness costs one request per block. Every run: pages edited that day get a full per-block rescan; on top of that a rolling shard (`NOTION_REFRESH_COMMENT_BUDGET`) rescans the longest-unscanned pages, 90% of the budget on human-authored content pages and 10% on the automation subtrees, which on the workspace this was built against were 95% of all block volume with near-zero comment traffic. The run report prints the current worst-case latency for a new inline comment on an untouched page. DB-row comments refresh when the row changes.

Comments are append-only. The API only ever returns *open* comments — resolved threads are invisible to it — so a comment that disappears between scans is kept and annotated `_[resolved/deleted ≤date]_` rather than dropped. Threads resolved before the mirror's first build remain unrecoverable via the API.

**The webhook feed** is what makes capture-before-resolve possible: the receiver captures a comment thread's content within about a minute of a `comment.*` event, and the daily refresh unions those captures into every scan (annotating ones no longer API-visible), scans event-named pages first, probes event-named DB rows, and shrinks the blind rolling scan to a slow audit while events flow. Delivery is at-most-once (8 retries over 24h) — the sweep stays as the backstop. Only body-affecting page events (`page.content_updated` / `created` / `moved` / `deleted`) mark a page for re-walk. `page.properties_updated` — property ids only, no values, and about three quarters of all events — routes to its own queue file (`props-probe-queue.json`, never `probe-queue.json`, which `refresh.py` rewrites wholesale at end of run) and costs one `GET /pages/{id}`: the property table and the CSV line are re-rendered, the body carried over from disk, so property changes reach the mirror in about an hour instead of about a day. The drain is snapshot-and-swap, and the receiver appending in the instant between the rename and its next write is a benign race, not a bug.

Capture failures are audible: an abandoned capture (a permanent 4xx, or six exhausted attempts) is the one permanently-lossy path, since a block-anchored thread that misses capture reaches the mirror only via the rolling shard, only while still unresolved, and an absent comment is indistinguishable from no comment. `webhook-capture-offset.json` is, despite its name, the capture loop's byte watermark into the events file; rotation of that file only fires when the watermark sits exactly at EOF with no pending retries.

**Comment identity.** Comments used to be keyed by rendered text, which is not an identity: two people writing "TBD" in two threads collapsed into one bullet, and one comment whose rendering shifted split into two. Every bullet now carries its id (the three forms above), both merge paths key on it, dedup is by id and never by text, and an edited comment updates in place. `migrate_comment_ids.py` did the same for what was already on disk; it stamps ids and never rewrites text, since the next probe re-renders the text anyway. A `legacy` shim is not a verdict — a later `--refetch` run upgrades it in place. Comment text renders through `rich_md` at both producers (`Walker.comments_for` and the receiver's capture record) so the two can never disagree mid-transition: an @-mention degrades to an un-titled but followable link rather than a dead word.

**Concurrency posture.** The mirror paces itself at 3.0 rps and the receiver at 2.0, against the same ~3 rps integration token; other tools on the token add to it. Every client honours `Retry-After`, so contention costs latency, never data.

### About coverage

The mirror renders a `child_page`, `child_database` or `link_to_page` block it did not walk as a one-line stand-in carrying the target's id; where the target was never captured, that line is the only trace it exists. `coverage_census.py` diffs referenced ids against captured artifacts and writes the gap to `_meta/coverage/census.json` as a work list — every absent id lands `disposition: "unreviewed"` — deliberately not as a baseline, which would freeze a real hole into the definition of "expected". Presence is kind-specific: a database is captured when its row directory exists, a page when its `.md` exists, and a `link_to_page` resolves against either. Many ids are referenced as databases and exist only as some page's `.md`; counting those as present would report a database as captured while every one of its rows is missing. The scan is shape-aware because four generations of renderer wrote the corpus and do not agree on the line; each occurrence records which shape it matched.

Two limits bound what any assert built on this can claim. It sees probed bodies only: most row files carry no enrichment, so an inline database on any of them emits no stand-in and is invisible. And two reference classes carry no id at all and are reported as counts (`blind_spots`) rather than enumerated: the id-less stand-in the first build wrote (`- 🗄️ Title` with no id), and blocks that render as nothing (`column_list`, `column`, `synced_block`) or as an `unhandled block type` comment. So the census detects referenced-but-absent, never never-referenced.

**Exclusions** (`--exclude ID --reason R [--note TEXT]`) name individually reasoned exceptions from a closed set — `deleted` / `archived` / `not_a_db` / `db404` / `no_access` / `deliberate`+note — never bulk or pattern rules. `no_access` is the one reversible reason: Notion answers a database whose data sources are not shared with the integration with a 400 `validation_error`, a per-id verdict from a working token, and nothing re-checks an exclusion, so a workspace that shares those sources later stays blind to them while the nightly reports "no new gaps". They want a periodic manual re-probe (`GET /databases/<id>` over the `no_access` ids, clearing any that now return 200), deliberately not automated: a standing job that edits the exclusion record without a human reading the result is how a reasoning file turns back into a baseline.

**The backfill** closes the work list with the engine's own code — a `child_database` through `refresh.refresh_db`, a `sub_page` through `refresh.walk_content_page` — and a test asserts a backfilled database is byte-identical to what the nightly would have written. It is budgeted and resumable (per-id progress after each id; a database is built under `.partial-<id>` and renamed on completion, so a killed run never leaves a half-captured directory the next census reads as present). Refusals become exclusions, not silence: a 404 on a database id is ambiguous — a deleted database and a linked *view* of one both refuse `/databases` — so one `GET /blocks` separates them. Ten consecutive refusals stop the run: that is a fact about the token, not about ten ids. Chain closure re-scans each capture with the census's own scanner, so a row body carrying its own inline database joins the queue; discovery stops at the census's definition and does not follow relation targets.

**The coverage assert** inside every full run re-measures the referenced-but-absent set, subtracts `exclusions.json`, and expects zero findings. It reports and never fails the run: a new inline database appearing overnight is information, and by the time it runs the tree is written, so a malformed exclusion file lands as a note rather than aborting the commit. Two arms rule out a false clean: a scan finding zero stand-ins anywhere reports `INCONCLUSIVE` (the root moved or the checkout omits it), and so does a reference count more than 10% under the previous run's (a corpus missing a subset loses references and targets together); the floor lives in `_meta/state/coverage-floor.json` and an inconclusive run does not lower it. A budget-exhausted run skips the assert and says so. A `sub_page` finding names the tool that closes it (`coverage_backfill.py`, since no nightly phase captures a row-body sub-page), and an id the nightly itself flagged `not_a_db` stays a finding until a human triages it; that flag is written on a 400/403/404 only. Databases the backfill found unshared land in an `unshared` bucket of `db-flags.json` that `phase_discovery` skips, since row-body discovery would otherwise re-ask every one of them nightly.

### About the renderer's fidelity

Cell rendering replicates the first build byte-for-byte, validated via `refresh.py --mode validate`, which re-renders rows unedited since the build and diffs against disk. Known quirks preserved deliberately: formula/rollup nulls render as `None`, checkboxes as `true`/`false`, buttons as `{}`, numbers via raw `str()`, untitled rows as `untitled`, `\|` escaping in row-md tables. One deliberate deviation from the build: query payloads cap list-valued properties (title/rich_text/relation/people) at 25 items, which silently truncated big relation cells (a job feed's relation to everyone who clicked each posting, for instance); `refresh.py` re-fetches capped values in full via the per-property endpoint (`expand_truncated_props`, ~1–2 extra requests per capped cell per run). Rollup arrays remain capped at 25 — their elements come back in a different shape, and the underlying rows are in the target DB's own CSV.

A downstream fork ported `cell()`/`_cell()` and `expand_truncated_props()` (copied, not imported) with named deviations; a semantic change to `notion_core/flatten.py` does not propagate there.

Three checks, and what each covers — they do not overlap:

| Check | Covers | Cost |
|---|---|---|
| `--mode validate` | `cell()` / `md_cell()` and `render_row_md`'s header and property table, only. It never calls `Walker.render`, `probe_row` or the comment merge. | live, ~1 req per row batch |
| `python3 -m unittest discover tests` | `Walker.render` against golden fixtures (a new dispatch branch without a fixture fails the suite), nesting, quote/callout children, synced-block indirection, the `rich_md` annotation matrix; plus the seeded render→parse→re-render fuzz (`FUZZ_CASES` scales it). | offline, zero requests |
| `--mode validate --refetch` | the seam the other two leave open: re-probes a sample of enriched rows live and diffs `probe_row`'s enrichment against disk, masking `_[resolved/deleted ≤date]_` stamps. `--refetch-sample` (20), `--dbs`. Read-only on the mirror except that an attachment not already on disk downloads. | live, ~1–5 req per row |

Two corpus-wide migrations (body indent 0 with delimited regions; the generated-file header on every row) ran once as one-shot scripts and were removed once spent; their source is in git history immediately before the removal, and `probe_row` and `render_row_md` now emit both forms directly, so nothing produces the old shape for them to normalise. Three more one-shot scripts did the original rebuild of the mirror and were removed after it; the first build's fetchers, which hardcoded that machine's paths, were removed when this repository became public.
