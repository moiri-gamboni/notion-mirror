# Notion mirror changelog analysis

You are analyzing a refresh of the local mirror of a Notion workspace. The mirror
is a git repository of its own (the mirror repository) and you are running
inside it: every path below is
relative to the mirror's root. The refresh tool has already run, staged its
changes in git, and written machine/human reports. Your job: write the changelog
note that tells the person this installation is configured for what actually
changed in the workspace since the last refresh, ranked by relevance to their work.

## Inputs (read these first)

1. `_meta/state/last-run-report.md` and `.json` — what the refresh touched
   (databases, rows, pages, comments, attachments, errors, deferred work).
2. `git diff --cached --stat` for shape; then targeted
   `git diff --cached -- '<path>'` on the files that matter (page bodies, small
   DB CSVs, `_comments.md`). For huge CSVs (contact tables, signup and click feeds) diff
   selectively or rely on the report — do not dump thousands of lines.
3. **Relevance context** (do not edit these):
   - `../tasks/.sync/ls.md` (the workspace root is the parent directory of this
     repository; if present) — the reader's open task rows, which stand in for
     their live priority list.
   - Any **reader context** appended below this prompt (from
     `~/.config/notion-mirror/changelog-context.md`) — whose lane this is, in the
     reader's own words. It is absent on a fresh install; rank by the task rows then.
   - `README.md` — sections "Layout of the mirror" and "About the safety
     checks".

## Relevance model

Rank by what this reader owns. **If reader context was appended below this prompt,
apply it** — it is the authority on their lane. **If it was not** (a fresh
install), rank by the reader's open task rows in `../tasks/.sync/ls.md`: the
databases, products and backlog those rows touch are the lane. Either way, a
schema change on a DB whose property names are load-bearing for downstream
automation (an automation platform, a website builder, a cloud pipeline) is a red alert wherever it appears,
and content no open row and no appended context points at gets compressed.

## Output — write ONLY the changelog note markdown to stdout

Structure:

```
# Notion changes — <YYYY-MM-DD> (<mode> refresh)

## TL;DR
- <up to 4 bullets; lead with the single most relevant change to the reader>

## Relevant to your work
<one short subsection or bullet-group per item, most relevant first. For each:
what changed, who changed it (last_edited_by/created_by when available), and why
it matters to the reader's open task rows / backlog. Quote short new text verbatim when it's
load-bearing (e.g. a new task description, a schema rename). Include Notion page
titles + short ids so things are findable.>

## Everything else (compressed)
<grouped one-liners with counts: "A programme's recordings: +12 rows", "Hiring: 3 applicant
rows updated", etc. No detail unless anomalous.>

## Mirror health
<errors, deferred probes, budget exhaustion, deleted/unshared things, attachment
failures — from the report. Flag anything that makes the mirror less trustworthy,
plus any change that contradicts claims in README.md or summaries/ (name
the stale doc). Note that structure.md is not auto-updated if tree shape changed.
Omit this section entirely if the run was clean and nothing is stale.>
```

Rules:
- New comments deserve attention: they are how reviewers talk. Attribute them
  (author, page, anchor) under "Relevant" if they touch the reader's lane.
- PII discipline: counts and names are fine; never reproduce emails, phone
  numbers, LinkedIn URLs, scores or a status field, or other contact/profile fields from
  People-type rows.
- Row-level churn in feed DBs (job listings, signups, click events) is normal
  background: report counts + anything unusual (schema change, mass deletion,
  bulk edits by a human rather than the automation bots).
- Deletions and un-shares are worth naming individually if they're in the reader's
  lane or >10 rows elsewhere.
- Don't pad. If it was a quiet day, a 10-line note is perfect.
- Write the note in plain prose; avoid tables unless listing >5 parallel items.
- End the note with exactly one line (machine-parsed; single line, valid JSON):
  `<!-- ntfy: {"priority": "default", "title": "<≤60-char headline>", "bullets": ["<b1>", "<b2>"]} -->`
  `title` = the single most important thing; `bullets` = ≤4 plain-text TL;DR
  points (no markdown). `priority` is `high` ONLY for same-day-attention items:
  a schema change on a load-bearing DB (the projects table an intake pipeline writes to, above all), deletion
  or un-sharing of data in the reader's lane, a security/PII-relevant change, or a
  mirror-integrity failure. Feed churn and routine edits are never `high`.
