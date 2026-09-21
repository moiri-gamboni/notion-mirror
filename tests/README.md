# Renderer tests

```bash
cd <repo root>
NOTION_MIRROR=/path/to/mirror python3 -m unittest discover tests   # offline, zero API requests
```

`NOTION_MIRROR` must name a built mirror (or any directory holding `workspace/_databases`): the engine asserts it at import, and the tests that import `refresh` inherit that. Nothing is written there — the sandbox tests set their own `NOTION_MIRROR` under a temp dir.

`refresh.py --mode validate` re-renders row **property tables** and diffs them against disk. It never touched `Walker.render`, so every change to the block renderer was an unverified rewrite. These tests are the missing oracle.

## What is here

| File | What it pins |
|---|---|
| `test_renderer.py` | every fixture renders to its expected file; every type `render()` dispatches on has a fixture |
| `test_validate_refetch.py` | the `--refetch` row sampler and its date mask (the parts that could make the live gate pass vacuously) |
| `test_fuzz_roundtrip.py` | seeded fuzz over the render→parse round trip on **all three** entry points — `inline_md` (spans), `lines_blocks` (blocks) and `prop_rich` (property values: Summary, Owner, Notes) — through the real renderer and parser: per-character annotation survival, convergence, nothing refused. Case N is `random.Random(N)`, so a failure reproduces by index; `FUZZ_CASES=200000` for a deep pass, sized by experience (defects surfaced past 8k and past 20k). Nothing is shielded any more: the generator emits every shape the renderer can produce, including quote/callout children, hard breaks, marker-leading text and backtick-bearing code runs. `RoundTripRegressions` beside it pins the named shapes. A suite covering only the first two entry points is how a `prop_rich` precedence bug destroyed every span containing a page id — if you add a fourth entry point, it needs its own generator here |
| `fixture_support.py` | `FakeApi`/`FakeUsers` and the fixture loader. `FakeApi` raises on any call other than a fixture-served `/blocks/{id}/children`, so a green run *is* the proof that fixture mode cost nothing |
| `fixtures/blocks/*.json` | captured (or hand-authored) block payloads + the children the walker would fetch |
| `fixtures/expected/*.md` | their rendered form — the golden output |

Each fixture records its own `provenance`: `live:` ones were captured from a workspace (`capture` writes them; none ship in this repository), `synthetic —` ones were hand-authored per Notion's block-object docs, in the shape a capture of that type has, with invented prose and placeholder ids. `test_every_synthetic_fixture_matches_what_the_generator_would_write` pins each committed synthetic fixture to its `capture_fixtures.SYNTHETIC` entry.

## Changing the renderer

1. Make the change.
2. `python3 capture_fixtures.py expected` — rewrites every expected file from the current renderer.
3. **Read the diff.** That diff is the whole review: it is the exact list of what the mirror's next full run will rewrite. An expected-file diff you cannot explain line by line is a bug you are about to commit.
4. Run the suite, then `--mode validate --refetch` (below) against live rows.

Adding a block type to `render()` fails `test_every_dispatched_type_is_covered` until a fixture covers it — that test reads the branch list out of `render()`'s own source.

## The deliberate-break drill

The suite is only worth running if it fails when the renderer breaks. Confirm it does, roughly quarterly and after any change to the harness itself:

Restore from a **file copy**, never `git checkout` — this drill gets run while you are editing the renderer, and `git checkout` silently discards every unstaged line in the file. It did exactly that during this drill's first run; the fix was to retype 75 lines.

The renderer is `notion_core/walker.py`; it was part of `refresh.py` until 2026-08-16.

```bash
cp notion_core/walker.py /tmp/walker.py.bak                 # 1. back up first
python3 - <<'BREAK'                                         # 2. break to_do
import pathlib
p = pathlib.Path("notion_core/walker.py")
s = p.read_text()
old = '''lines.append(f"{p}- [{chk}] {rich_md(data.get('rich_text'))}")'''
new = '''lines.append(f"{p}- {rich_md(data.get('rich_text'))}")'''
assert old in s, "mutation target moved — update this drill"
p.write_text(s.replace(old, new, 1))
BREAK
python3 -m unittest discover tests                          # 3. MUST fail
cp /tmp/walker.py.bak notion_core/walker.py                 # 4. restore
python3 -m unittest discover tests                          # 5. green again
```

Last run 2026-08-16 (after the renderer moved to `notion_core/walker.py`): 4 failures — `test_to_do_renders_a_checkbox`, the `to_do` and `nesting` golden fixtures, and the `to_do` round trip. Green again after restore. The 2026-08-06 run, against the same mutation in `refresh.py`, caught 3; the round-trip case is new since.

## Capturing fixtures

```bash
NOTION_TOKEN=... python3 capture_fixtures.py capture   # live, ~1 rps, read-only
python3 capture_fixtures.py synthetic                  # the hand-authored set
python3 capture_fixtures.py expected                   # re-render golden output
```

`capture` never overwrites an existing fixture (`--force` does) and strips signed-URL credentials (`X-Amz-*`) from file blocks before writing, so nothing short-lived and secret lands in git. A live capture carries real block, page and user ids: keep it out of a public tree, or replace it with a synthetic entry of the same shape (`synthetic --force` rewrites every entry in `SYNTHETIC`).

## `--mode validate --refetch`

The offline half of this harness cannot see a change in what Notion *returns*. The other half re-probes live rows and diffs `probe_row`'s enrichment string against what is on disk:

```bash
python3 refresh.py --mode validate --refetch --dbs "Tasks"
python3 refresh.py --mode validate --refetch --refetch-sample 40
```

It samples comment-bearing rows first (they exercise the retention merge, not just the body walk), masks `_[resolved/deleted ≤date]_` stamps before comparing — a re-probe re-stamps them with today's date — and prints per-row request counts plus a total. On an unmodified tree it must report zero mismatches; hand-edit one comment bullet in a row `.md` and it must report one.
