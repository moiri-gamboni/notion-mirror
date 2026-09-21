"""Shared Notion primitives that outlive any one script.

The bottom layer of both toolchains: the mirror engine imports these, and so does
tasksync. Nothing here imports anything above itself — not `refresh.py`, not `paths.py`,
not tasksync — so borrowing one regex costs neither a mounted mirror volume nor the
engine's 3,300 lines. `../tests/test_notion_core_layering.py` reads the source to keep
it that way.

  api        the paced, budgeted, Retry-After-honouring HTTP client, and the two
             exceptions every caller of a paginating helper must handle
  richtext   Notion rich text -> markdown, span tier
  walker     Notion blocks -> markdown, block tier (tasksync subclasses `Walker`)
  md_blocks  markdown -> Notion blocks: the inverse of the two above
  flatten    property values -> cell strings, plus the 25-item property expansion
  rowmd      the row file's grammar: the marker, the region delimiters, the cid trailer
  runcfg     per-mode request budgets and the `--rows` id parser
  util       the primitives those share: time, id spelling, the run log

`md_blocks` lived inside `notes/infra-task-triage/proposals/push.py` until 2026-08-10;
that directory was archived triage history, so a live daily tool importing its write
path from there would both have hidden an active dependency inside dead work and made
the directory undeletable. It was in fact deleted on 2026-08-13, which is what the move
was for. The other seven modules were carved out of `refresh.py` on 2026-08-16, for the
same reason in a different shape: tasksync borrowed 18 symbols from it and paid for all
of it, mirror assertion included.
"""
