"""Coverage backfill: fetching the referenced-but-absent ids, resumably.

Every test is offline. `FakeApi` answers the four endpoints the capture paths
touch (`/databases/<id>`, its `/query`, `/blocks/<id>/children`, `/comments`) and
nothing reaches Notion. `refresh`'s module-level corpus paths are rebound to a
temp tree per test, which is what lets the DB-capture path run for real — the
parity test then asserts a backfilled database is byte-identical to what the
nightly's own `refresh.capture_new_db` would have written.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import coverage_backfill  # noqa: E402  (_tools is not a package; discover's top dir is tests/)
import coverage_census  # noqa: E402
import refresh  # noqa: E402


def rid(n):
    """A distinct 32-hex id per fixture, readable in failure output."""
    return f"{n:032x}"


DB_A, DB_B, DB_GONE, DB_VIEW = rid(0xA), rid(0xB), rid(0x60), rid(0x1E)
ROW_A, ROW_B = rid(0xA10), rid(0xA11)
PAGE_P = rid(0xB0)


def db_schema(db_id, title):
    return {"object": "database", "id": refresh.dashed(db_id), "request_id": "req-x",
            "title": [{"plain_text": title}],
            "properties": {"Name": {"id": "title", "type": "title", "title": {}}}}


def row(row_id, title, db_id):
    return {"object": "page", "id": refresh.dashed(row_id),
            "parent": {"type": "database_id", "database_id": refresh.dashed(db_id)},
            "created_time": "2026-01-01T00:00:00.000Z",
            "last_edited_time": "2026-01-02T00:00:00.000Z",
            "properties": {"Name": {"id": "title", "type": "title",
                                    "title": [{"plain_text": title}]}}}


def child_db_block(db_id, title):
    return {"id": refresh.dashed(db_id), "type": "child_database",
            "child_database": {"title": title}, "has_children": False}


def para(text):
    return {"id": refresh.dashed(rid(0xF00D)), "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "plain_text": text,
                                         "annotations": {}}]}, "has_children": False}


class FakeApi:
    """Notion, reduced to what the capture paths ask of it.

    `budget` behaves like the real `Api`'s: `check_budget` raises `refresh.Budget`
    once the request count reaches it, so a budget stop happens mid-queue exactly
    where the real one would."""

    def __init__(self, dbs=None, rows=None, blocks=None, pages=None, budget=10 ** 6,
                 refuse=None, refuse_query=None, on_get=None):
        self.dbs = dbs or {}          # id32 -> schema payload
        self.rows = rows or {}        # id32 -> [row payloads]
        self.blocks = blocks or {}    # id32 -> [block payloads]
        self.pages = pages or {}      # id32 -> page payload
        self.refuse = refuse or {}    # id32 -> HTTP code (any endpoint)
        self.refuse_query = refuse_query or {}  # id32 -> HTTP code (row query only)
        self.on_get = on_get          # callback(path) fired before each GET
        self.budget = budget
        self.n = 0
        self.r429 = 0
        self.seen = []                # every path requested, in order

    # -- plumbing ---------------------------------------------------------
    def check_budget(self):
        if self.n >= self.budget:
            raise refresh.Budget()

    def _spend(self, path):
        self.check_budget()
        self.n += 1
        self.seen.append(path)

    @staticmethod
    def _id(path, prefix, suffix=""):
        core = path[len(prefix):]
        if suffix:
            core = core[:-len(suffix)]
        return refresh.undash(core)

    # -- endpoints --------------------------------------------------------
    def get(self, path, params=None, ver=None):
        self._spend(path)
        if self.on_get:
            self.on_get(path)
        if path.startswith("/databases/"):
            i = self._id(path, "/databases/")
            if i in self.refuse:
                raise refresh.ApiError(self.refuse[i], "refused")
            if i not in self.dbs:
                raise refresh.ApiError(404, "no such database")
            return json.loads(json.dumps(self.dbs[i]))
        if path.startswith("/pages/"):
            i = self._id(path, "/pages/")
            if i in self.refuse:
                raise refresh.ApiError(self.refuse[i], "refused")
            if i not in self.pages:
                raise refresh.ApiError(404, "no such page")
            return json.loads(json.dumps(self.pages[i]))
        if path.startswith("/blocks/"):
            i = self._id(path, "/blocks/")
            if i in self.blocks or i in self.dbs:
                return {"object": "block", "id": refresh.dashed(i), "type": "child_database"}
            raise refresh.ApiError(404, "no such block")
        raise refresh.ApiError(404, f"unstubbed GET {path}")

    def post(self, path, body=None, ver=None):
        self._spend(path)
        raise refresh.ApiError(404, f"unstubbed POST {path}")

    def query_rows(self, path, body=None, ver=None):
        # the windowed row sweep (query_db_rows) calls this instead of paginate;
        # the fake serves complete row sets, so no windowing to emulate
        return list(self.paginate("POST", path, body=body, ver=ver))

    def paginate(self, method, path, body=None, params=None, ver=None):
        self._spend(path)
        if path.startswith("/databases/") and path.endswith("/query"):
            i = self._id(path, "/databases/", "/query")
            if i in self.refuse or i in self.refuse_query:
                raise refresh.ApiError(self.refuse.get(i) or self.refuse_query[i], "refused")
            return list(self.rows.get(i, []))
        if path.startswith("/blocks/") and path.endswith("/children"):
            return list(self.blocks.get(self._id(path, "/blocks/", "/children"), []))
        if path == "/comments":
            return []
        raise refresh.ApiError(404, f"unstubbed {method} {path}")


class BackfillCase(unittest.TestCase):
    """A temp mirror tree, a temp census/exclusions pair, and no network."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="a8-backfill-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.mirror_tree(self.tmp)
        self.census_path = os.path.join(self.tmp, "census.json")
        self.excl_path = os.path.join(self.tmp, "exclusions.json")
        self.write_exclusions({})

    def mirror_tree(self, base, name="notion"):
        """Rebind refresh's corpus paths at a fresh tree and return its root."""
        root = os.path.join(base, name)
        ws = os.path.join(root, "workspace")
        dbs = os.path.join(ws, "_databases")
        meta = os.path.join(root, "_meta")
        state = os.path.join(meta, "state")
        for d in (dbs, state):
            os.makedirs(d, exist_ok=True)
        for attr, val in (("NOTION", root), ("WS", ws), ("DBS", dbs),
                          ("META", meta), ("STATE", state)):
            old = getattr(refresh, attr)
            self.addCleanup(setattr, refresh, attr, old)
            setattr(refresh, attr, val)
        return root

    # -- fixtures ---------------------------------------------------------
    def write_census(self, absent):
        with open(self.census_path, "w") as f:
            json.dump({"generated": "2026-08-06T00:00:00Z", "corpus_root": "notion/workspace",
                       "totals": {}, "blind_spots": {},
                       "absent": [{"id": i, "kind": k, "title": t,
                                   "disposition": "unreviewed", "occurrences": []}
                                  for i, k, t in absent]}, f)

    def write_exclusions(self, d):
        with open(self.excl_path, "w") as f:
            json.dump(d, f)

    def args(self, **kw):
        import types
        base = {"dry_run": False}
        base.update(kw)
        return types.SimpleNamespace(**base)

    def ctx(self, api):
        return coverage_backfill.Ctx(api, refresh.Users(api), coverage_backfill.load_state(),
                                     refresh.new_report("test"), self.args(), self.excl_path)

    def run_backfill(self, api, progress=None, only=None, slice_n=None):
        progress = progress if progress is not None else coverage_backfill.load_progress()
        split = coverage_backfill.census_split(self.census_path, self.excl_path)
        ctx = self.ctx(api)
        items = coverage_backfill.work_items(split, progress, ctx.have, only=only)
        tally = coverage_backfill.run(ctx, progress, items, budget_slice=slice_n)
        return progress, tally

    def api_two_dbs(self, **kw):
        """Two one-row databases; A's row body points at B (the chain case)."""
        return FakeApi(
            dbs={DB_A: db_schema(DB_A, "Alpha"), DB_B: db_schema(DB_B, "Beta")},
            rows={DB_A: [row(ROW_A, "Row One", DB_A)], DB_B: []},
            blocks={ROW_A: [child_db_block(DB_B, "Beta")]},
            **kw)


class ArtifactParity(BackfillCase):
    """A backfilled database is the artifact set the nightly writes — asserted
    against the nightly's own first-time capture, byte for byte."""

    def api_one_db(self, **kw):
        """One database, one row, a plain body — so neither side chains and the
        comparison is of the same single capture."""
        return FakeApi(dbs={DB_A: db_schema(DB_A, "Alpha")},
                       rows={DB_A: [row(ROW_A, "Row One", DB_A)]},
                       blocks={ROW_A: [para("body text")]}, **kw)

    def test_matches_capture_new_db(self):
        self.write_census([(DB_A, "child_database", "Alpha")])
        mine, _ = self.run_backfill(self.api_one_db())
        self.assertIn(DB_A, mine["done"])
        theirs_root = self.mirror_tree(self.tmp, name="nightly")
        api = self.api_one_db()
        state = coverage_backfill.load_state()
        refresh.capture_new_db(api, refresh.Users(api), DB_A, state,
                               refresh.new_report("test"), self.args())

        got = self.tree(os.path.join(self.root, "workspace"))
        want = self.tree(os.path.join(theirs_root, "workspace"))
        self.assertEqual(sorted(want), sorted(got), "artifact set differs from the nightly's")
        for rel in want:
            self.assertEqual(open(os.path.join(theirs_root, "workspace", rel), "rb").read(),
                             open(os.path.join(self.root, "workspace", rel), "rb").read(),
                             f"{rel} differs from the nightly's bytes")

    def test_writes_the_expected_four_artifacts(self):
        """Named explicitly so the parity test can't pass by both sides being wrong."""
        self.write_census([(DB_A, "child_database", "Alpha")])
        self.run_backfill(self.api_two_dbs())
        rels = self.tree(os.path.join(self.root, "workspace"))
        self.assertIn(f"_databases/Alpha {DB_A}/_schema.json", rels)
        self.assertIn(f"_databases/Alpha {DB_A}/_schema.md", rels)
        self.assertIn(f"_databases/Alpha {DB_A}/Alpha {DB_A}.csv", rels)
        self.assertIn(f"_databases/Alpha {DB_A}/Row One {ROW_A}.md", rels)
        self.assertIn("_databases/_ALL-SCHEMAS.md", rels)

    def test_no_partial_directory_survives_a_failure(self):
        """A capture that dies mid-way leaves nothing the census would read as
        present — otherwise the gap freezes into the definition of expected."""
        self.write_census([(DB_A, "child_database", "Alpha")])
        # the schema GET lands, the row query dies: no CSV, so nothing complete
        api = self.api_two_dbs(refuse_query={DB_A: 500})
        progress, tally = self.run_backfill(api)
        self.assertEqual(tally["done"], 0)
        self.assertNotIn(DB_A, progress["done"])
        self.assertEqual([], [d for d in os.listdir(refresh.DBS) if DB_A in d])

    def test_a_database_in_flight_is_not_yet_visible_to_the_census(self):
        """The window the rename exists to close: while the rows are being
        written, a census must still call the database absent. A kill by signal
        skips the cleanup below, so whatever is on disk at this moment is what a
        later census reads."""
        self.write_census([(DB_A, "child_database", "Alpha")])
        seen = []
        real = refresh.refresh_db

        def spy(api, users, db_id, *a, **kw):
            out = real(api, users, db_id, *a, **kw)  # rows on disk, dir not yet renamed
            seen.append((db_id, coverage_census.present_ids(refresh.WS)))
            return out

        refresh.refresh_db = spy
        self.addCleanup(setattr, refresh, "refresh_db", real)
        self.run_backfill(self.api_two_dbs())
        self.assertTrue(seen, "the spy never fired — the capture path changed")
        for db_id, have in seen:
            self.assertNotIn(db_id, have["database"],
                             "a half-written database was already reported present")

    def test_the_capture_directory_name_reads_as_an_id_to_nobody(self):
        """Both id regexes anchor an optional `.md`/`.csv` at the end, so a name
        ending in the 32-hex id is a mirrored artifact — to the census, and to
        `refresh.db_dirs()`, which `phase_discovery` consults to decide an id
        needs no discovering. Pinned because the cheap simplification of this
        name (drop the suffix) is silent in both directions."""
        name = coverage_backfill.partial_name(DB_A)
        self.assertIsNone(refresh.ID32.search(name))
        self.assertIsNone(coverage_census.ID_IN_NAME.search(name))

    def test_the_nightly_cannot_index_rows_out_of_an_in_flight_capture(self):
        """`refresh.row_md_global_index` maps a row id to a *write destination*
        (`_dir_of_page` makedirs a folder beside the row and hands it to
        `walk_content_page`). If it can see into a capture directory, a nightly
        running between backfill slices writes a real content page inside one —
        and the next slice's startup sweep deletes it, uncommitted because the
        path is gitignored. `build_comment_index` reads the same shape.

        Both enumerations descend exactly one level, so the capture lives one
        level deeper than they look. Nothing in `refresh.py` changes."""
        self.write_census([(DB_A, "child_database", "Alpha")])
        seen = []
        real = refresh.refresh_db

        def spy(api, users, db_id, *a, **kw):
            out = real(api, users, db_id, *a, **kw)  # rows written, not yet renamed
            seen.append((set(refresh.row_md_global_index()),
                         set(refresh.build_comment_index()),
                         set(refresh.db_dirs())))
            return out

        refresh.refresh_db = spy
        self.addCleanup(setattr, refresh, "refresh_db", real)
        self.run_backfill(self.api_two_dbs())
        self.assertTrue(seen, "the spy never fired — the capture path changed")
        rows, comments, dbs = seen[0]
        self.assertNotIn(ROW_A, rows, "a nightly could target an in-flight capture dir")
        self.assertNotIn(ROW_A, comments)
        self.assertNotIn(DB_A, dbs, "an in-flight capture read as a mirrored database")

    def test_a_leftover_partial_directory_is_swept_at_startup(self):
        """A killed slice leaves one behind and nothing else ever removes it:
        `capture_database` only rmtrees the id it is working on, and once the
        census reads that id as present it never re-enters the work list."""
        self.write_census([])
        stale = os.path.join(refresh.DBS, coverage_backfill.partial_name(DB_A))
        os.makedirs(stale)
        os.environ.setdefault("NOTION_TOKEN", "test-token")
        rc = coverage_backfill.main(["--budget", "10", "--lock", os.path.join(self.tmp, "lock"),
                                     "--census", self.census_path,
                                     "--exclusions", self.excl_path])
        self.assertEqual(0, rc)
        self.assertFalse(os.path.exists(stale))

    @staticmethod
    def tree(root):
        out = []
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                out.append(os.path.relpath(os.path.join(dirpath, f), root))
        return out


class Resume(BackfillCase):
    """An interrupted run loses at most the id in flight, and never re-fetches
    an id it already finished."""

    def test_resume_after_interrupt_skips_done_ids(self):
        self.write_census([(DB_A, "child_database", "Alpha"), (DB_B, "child_database", "Beta")])
        first = self.api_two_dbs()
        progress, tally = self.run_backfill(first, slice_n=1)
        self.assertEqual("slice", tally["stopped"])
        self.assertEqual([DB_A], list(progress["done"]))

        second = self.api_two_dbs()
        progress2, tally2 = self.run_backfill(second)
        self.assertEqual(1, tally2["done"])
        self.assertEqual({DB_A, DB_B}, set(progress2["done"]))
        self.assertNotIn(f"/databases/{refresh.dashed(DB_A)}", second.seen,
                         "re-fetched a database the previous run had already captured")

    def test_progress_is_written_after_each_id_not_at_the_end(self):
        """The third id's request reads the progress file off disk: if progress
        were flushed at end of run it would still be empty there."""
        self.write_census([(DB_A, "child_database", "Alpha"),
                           (DB_B, "child_database", "Beta"),
                           (DB_GONE, "child_database", "Gone")])
        seen_at_third = {}

        def spy(path):
            if refresh.dashed(DB_GONE) in path:
                seen_at_third.update(coverage_backfill.load_progress())

        api = FakeApi(dbs={DB_A: db_schema(DB_A, "Alpha"), DB_B: db_schema(DB_B, "Beta"),
                           DB_GONE: db_schema(DB_GONE, "Gone")},
                      rows={DB_A: [], DB_B: [], DB_GONE: []}, on_get=spy)
        self.run_backfill(api)
        self.assertEqual({DB_A, DB_B}, set(seen_at_third.get("done", {})),
                         "progress file did not carry the earlier ids while the run was live")

    def test_resume_picks_up_a_chain_discovered_id(self):
        """A chain-discovered id survives an interrupt in `pending_extra`."""
        self.write_census([(DB_A, "child_database", "Alpha")])
        progress, _ = self.run_backfill(self.api_two_dbs(), slice_n=1)
        self.assertEqual([DB_B], [e["id"] for e in progress["pending_extra"]])
        progress2, tally2 = self.run_backfill(self.api_two_dbs())
        self.assertIn(DB_B, progress2["done"])
        self.assertEqual([], progress2["pending_extra"])


class Chain(BackfillCase):
    """Backfilling a database follows what its own rows reference."""

    def test_rows_of_an_already_captured_database_count_as_present_pages(self):
        """`capture_database` returns early when the directory is already there
        (a previous run died before recording progress). A reference out of those
        rows to a sibling row must resolve, or the sibling gets queued and comes
        back from `/pages` as a database row — an exclusion recording a decision
        about a row that was mirrored all along.

        Today the corpus walk behind `ctx.have` is what guarantees this, not the
        branch; the branch's own seeding is asymmetric with the writing path.
        This pins the property so whichever of the two carries it, it holds."""
        dirpath = os.path.join(refresh.DBS, f"Alpha {DB_A}")
        os.makedirs(dirpath)
        with open(os.path.join(dirpath, f"Row One {ROW_A}.md"), "w") as f:
            f.write("- 📄 **Row Two** — sub-page `%s`\n" % ROW_B)
        with open(os.path.join(dirpath, f"Row Two {ROW_B}.md"), "w") as f:
            f.write("body\n")

        self.write_census([(DB_A, "child_database", "Alpha")])
        api = FakeApi(dbs={DB_A: db_schema(DB_A, "Alpha")},
                      pages={ROW_B: {"object": "page", "id": refresh.dashed(ROW_B),
                                     "parent": {"type": "database_id",
                                                "database_id": refresh.dashed(DB_A)},
                                     "properties": {}}})
        progress, tally = self.run_backfill(api)
        self.assertEqual([], progress["done"][DB_A]["chained"])
        self.assertEqual({}, coverage_census.load_exclusions(self.excl_path))
        self.assertEqual(0, tally["excluded"])

    def test_row_body_inline_database_is_queued_and_captured(self):
        self.write_census([(DB_A, "child_database", "Alpha")])
        progress, tally = self.run_backfill(self.api_two_dbs())
        self.assertEqual(2, tally["done"], "the chained database was not captured")
        self.assertEqual([DB_B], progress["done"][DB_A]["chained"])
        self.assertTrue(os.path.isdir(os.path.join(refresh.DBS, f"Beta {DB_B}")))

    def test_only_seeds_the_chain_from_one_id(self):
        self.write_census([])  # not in the census at all: --only must still work
        progress, tally = self.run_backfill(self.api_two_dbs(), only=[DB_A])
        self.assertEqual({DB_A, DB_B}, set(progress["done"]))

    def test_an_already_mirrored_reference_is_not_refetched(self):
        os.makedirs(os.path.join(refresh.DBS, f"Beta {DB_B}"), exist_ok=True)
        self.write_census([(DB_A, "child_database", "Alpha")])
        api = self.api_two_dbs()
        progress, tally = self.run_backfill(api)
        self.assertEqual(1, tally["done"])
        self.assertEqual([], progress["done"][DB_A]["chained"])


class MixedKind(BackfillCase):
    """An id referenced as both a sub-page and a child_database needs *two*
    artifacts, but `progress["done"]` is keyed by id alone.

    The census keys `absent` by id too and takes the first-seen kind, so the id
    yields one work item against two presence requirements. Capture the page and
    the nightly reports the database half as a NEW gap every night, forever,
    while the backfill skips it — including under `--only`, which is the
    operator's only escape hatch. Recovery today means hand-editing a gitignored
    progress file. 20 mixed-kind ids exist in the corpus; all are currently
    present under both kinds, so this is latent rather than firing."""

    def page_payload(self, pid):
        return {"object": "page", "id": refresh.dashed(pid),
                "parent": {"type": "workspace", "workspace": True},
                "created_time": "2026-01-01T00:00:00.000Z",
                "last_edited_time": "2026-01-02T00:00:00.000Z",
                "archived": False, "in_trash": False, "url": "https://notion.so/p",
                "properties": {"title": {"type": "title",
                                         "title": [{"plain_text": "Both"}]}}}

    def api_both(self):
        return FakeApi(pages={DB_A: self.page_payload(DB_A)}, blocks={DB_A: [para("hi")]},
                       dbs={DB_A: db_schema(DB_A, "Both")}, rows={DB_A: []})

    def capture_as_page_first(self):
        self.write_census([(DB_A, "sub_page", "Both")])
        progress, _ = self.run_backfill(self.api_both())
        self.assertIn(DB_A, progress["done"])
        return progress

    def test_an_id_done_as_a_page_is_still_captured_as_a_database(self):
        progress = self.capture_as_page_first()
        self.write_census([(DB_A, "child_database", "Both")])
        progress, tally = self.run_backfill(self.api_both(), progress=progress)
        self.assertEqual(1, tally["done"], "the database half was skipped as already done")
        self.assertTrue(os.path.isdir(os.path.join(refresh.DBS, f"Both {DB_A}")))

    def test_only_still_reaches_an_id_recorded_done_under_another_kind(self):
        progress = self.capture_as_page_first()
        self.write_census([])
        progress, tally = self.run_backfill(self.api_both(), progress=progress,
                                            only=[DB_A])
        self.assertEqual(1, tally["done"], "--only is the escape hatch and it was closed")

    def test_a_chain_reference_to_an_id_done_under_another_kind_is_queued(self):
        """Chain closure is this tool's main discovery path, and it honours
        `done` by id too. `chain_refs` has already established the id is absent
        *under the kind it was referenced as*, so dropping it there re-opens the
        same hole one layer down."""
        progress = self.capture_as_page_first()
        self.write_census([(DB_B, "child_database", "Beta")])
        api = FakeApi(dbs={DB_B: db_schema(DB_B, "Beta"), DB_A: db_schema(DB_A, "Both")},
                      rows={DB_B: [row(ROW_A, "Row One", DB_B)], DB_A: []},
                      blocks={ROW_A: [child_db_block(DB_A, "Both")]})
        progress, tally = self.run_backfill(api, progress=progress)
        self.assertEqual([DB_A], progress["done"][DB_B]["chained"],
                         "the chained reference was dropped as already done")
        self.assertTrue(os.path.isdir(os.path.join(refresh.DBS, f"Both {DB_A}")))

    def test_an_id_done_and_present_under_this_kind_is_not_refetched(self):
        """The short-circuit still has to do its job — this is the whole reason
        a re-run costs nothing."""
        progress = self.capture_as_page_first()
        api = self.api_both()
        progress, tally = self.run_backfill(api, progress=progress)
        self.assertEqual(0, tally["done"])
        self.assertEqual([], api.seen)


class Refusals(BackfillCase):
    """Notion refusing an id moves it into the exclusion record, with a reason."""

    def test_404_becomes_a_db404_exclusion(self):
        self.write_census([(DB_GONE, "child_database", "Gone")])
        api = FakeApi(dbs={}, rows={})  # neither /databases nor /blocks knows it
        progress, tally = self.run_backfill(api)
        self.assertEqual(1, tally["excluded"])
        excl = coverage_census.load_exclusions(self.excl_path)
        self.assertEqual("db404", excl[DB_GONE]["reason"])
        self.assertIn("HTTP 404", excl[DB_GONE]["note"])
        self.assertIn(DB_GONE, progress["excluded"])

    def test_404_on_a_live_block_becomes_not_a_db(self):
        """A linked view refuses /databases but still exists as a block — the one
        distinction that keeps `db404` from absorbing every linked view."""
        self.write_census([(DB_VIEW, "child_database", "A view")])
        api = FakeApi(dbs={}, rows={}, blocks={DB_VIEW: []})
        progress, _ = self.run_backfill(api)
        excl = coverage_census.load_exclusions(self.excl_path)
        self.assertEqual("not_a_db", excl[DB_VIEW]["reason"])

    def test_403_is_excluded_too_with_the_status_in_the_note(self):
        self.write_census([(DB_GONE, "child_database", "Gone")])
        api = FakeApi(dbs={DB_GONE: db_schema(DB_GONE, "Gone")}, refuse={DB_GONE: 403})
        progress, tally = self.run_backfill(api)
        self.assertEqual(1, tally["excluded"])
        self.assertIn("HTTP 403", coverage_census.load_exclusions(self.excl_path)[DB_GONE]["note"])

    def test_an_excluded_id_is_not_retried_on_the_next_run(self):
        self.write_census([(DB_GONE, "child_database", "Gone")])
        self.run_backfill(FakeApi(dbs={}, rows={}))
        second = FakeApi(dbs={}, rows={})
        progress2, tally2 = self.run_backfill(second)
        self.assertEqual(0, tally2["excluded"])
        self.assertEqual([], second.seen, "a settled exclusion was requested again")

    def test_a_run_of_refusals_stops_before_emptying_the_work_list(self):
        """Ten refusals in a row is a fact about the token, not about ten ids."""
        ids = [rid(0x1000 + i) for i in range(coverage_backfill.ACCESS_FAIL_LIMIT + 5)]
        self.write_census([(i, "child_database", "?") for i in ids])
        progress, tally = self.run_backfill(FakeApi(dbs={}, rows={}))
        self.assertEqual("access", tally["stopped"])
        self.assertEqual(coverage_backfill.ACCESS_FAIL_LIMIT, tally["excluded"])
        self.assertLess(len(progress["excluded"]), len(ids))

    def test_definitive_400s_do_not_trip_the_access_breaker(self):
        """Unshared databases cluster, and their 400 is a per-id verdict from a
        working token — live slice 3 stopped at 43 requests when they fed the
        breaker. A run of them must exclude every one and keep going."""
        ids = [rid(0x2000 + i) for i in range(coverage_backfill.ACCESS_FAIL_LIMIT + 5)]
        self.write_census([(i, "child_database", "?") for i in ids])

        def refuse_all_as_unshared(path):
            if path.startswith("/databases/"):
                raise refresh.ApiError(400, json.dumps({
                    "object": "error", "status": 400, "code": "validation_error",
                    "message": "Database with ID x does not contain any data "
                               "sources accessible by this API bot."}))

        api = FakeApi(dbs={i: db_schema(i, "?") for i in ids},
                      on_get=refuse_all_as_unshared)
        progress, tally = self.run_backfill(api)
        self.assertEqual("", tally["stopped"])
        self.assertEqual(len(ids), tally["excluded"])

    def test_an_archived_page_is_excluded_as_archived(self):
        self.write_census([(PAGE_P, "sub_page", "A page")])
        page = {"object": "page", "id": refresh.dashed(PAGE_P), "archived": True,
                "parent": {"type": "workspace", "workspace": True}, "properties": {}}
        progress, tally = self.run_backfill(FakeApi(pages={PAGE_P: page}))
        self.assertEqual(1, tally["excluded"])
        self.assertEqual("archived", coverage_census.load_exclusions(self.excl_path)[PAGE_P]["reason"])

    def test_400_no_accessible_data_source_is_excluded_not_failed(self):
        """Notion reports a database none of whose data sources are shared as 400
        validation_error, not 403. Live slice 2 recorded 519 of these as retryable
        failures, which would have re-queued them at the head of every later run."""
        self.write_census([(DB_GONE, "child_database", "Unshared")])

        def refuse_with_notions_wording(path):
            if path.startswith("/databases/"):
                raise refresh.ApiError(400, json.dumps({
                    "object": "error", "status": 400, "code": "validation_error",
                    "message": f"Database with ID {refresh.dashed(DB_GONE)} does not "
                               f"contain any data sources accessible by this API bot."}))

        api = FakeApi(dbs={DB_GONE: db_schema(DB_GONE, "Unshared")},
                      on_get=refuse_with_notions_wording)
        progress, tally = self.run_backfill(api)
        self.assertEqual(1, tally["excluded"])
        self.assertEqual(0, tally["failed"])
        excl = coverage_census.load_exclusions(self.excl_path)
        # `no_access`, not `db404`: the database exists, this token cannot see it.
        # Sharing it tomorrow makes the id mirrorable again, and only a reason
        # that says so will ever get re-probed.
        self.assertEqual("no_access", excl[DB_GONE]["reason"])
        self.assertIn("no data source shared", excl[DB_GONE]["note"])
        self.assertNotIn(DB_GONE, progress["failed"])

    def unshared_api(self, *ids):
        """Notion's verdict for a database none of whose data sources are shared."""
        def refuse_as_unshared(path):
            if path.startswith("/databases/"):
                raise refresh.ApiError(400, json.dumps({
                    "object": "error", "status": 400, "code": "validation_error",
                    "message": "Database with ID x does not contain any data "
                               "sources accessible by this API bot."}))

        return FakeApi(dbs={i: db_schema(i, "Unshared") for i in ids},
                       on_get=refuse_as_unshared)

    def test_an_unshared_database_is_recorded_in_the_nightlys_own_state(self):
        """Its two sibling refusal paths set `not_a_db` so `phase_discovery`
        stops retrying the id; this one set nothing, so all 1,322 of them stay
        eligible and get re-probed — and `capture_new_db`'s unqualified except
        then mislabels every one as `not_a_db`, which is false."""
        self.write_census([(DB_GONE, "child_database", "Unshared")])
        ctx = self.ctx(self.unshared_api(DB_GONE))
        progress = coverage_backfill.load_progress()
        split = coverage_backfill.census_split(self.census_path, self.excl_path)
        coverage_backfill.run(ctx, progress,
                              coverage_backfill.work_items(split, progress, ctx.have))
        self.assertIn(DB_GONE, ctx.state["unshared"])
        self.assertNotIn(DB_GONE, ctx.state["not_a_db"],
                         "an unshared database is not a linked view")

    def test_the_unshared_bucket_survives_a_save_load_round_trip(self):
        """It has to reach `db-flags.json`, which is where the nightly reads it."""
        state = coverage_backfill.load_state()
        state["unshared"][DB_GONE] = "2026-08-07T00:00:00Z"
        coverage_backfill.save_state(state)
        self.assertEqual({DB_GONE: "2026-08-07T00:00:00Z"},
                         coverage_backfill.load_state()["unshared"])

    def test_a_run_that_never_touches_unshared_leaves_it_intact(self):
        """`phase_discovery` skips the ids in this bucket, so it is the nightly's
        working state and not the backfill's scratch. A save that wrote only the
        keys this tool happens to set would erase it on every run — silently, and
        the symptom would be ~1,300 wasted probes a night somewhere else."""
        pre = coverage_backfill.load_state()
        pre["unshared"][DB_VIEW] = "2026-08-01T00:00:00Z"
        pre["not_a_db"][DB_GONE] = "2026-08-01T00:00:00Z"
        coverage_backfill.save_state(pre)

        self.write_census([(DB_A, "child_database", "Alpha")])
        ctx = self.ctx(self.api_two_dbs())
        progress = coverage_backfill.load_progress()
        split = coverage_backfill.census_split(self.census_path, self.excl_path)
        coverage_backfill.run(ctx, progress,
                              coverage_backfill.work_items(split, progress, ctx.have))
        coverage_backfill.save_state(ctx.state)

        after = coverage_backfill.load_state()
        self.assertEqual({DB_VIEW: "2026-08-01T00:00:00Z"}, after["unshared"])
        self.assertEqual({DB_GONE: "2026-08-01T00:00:00Z"}, after["not_a_db"])

    def test_400_linked_database_is_excluded_as_not_a_db(self):
        """Notion's other permanent 400 — "is a linked database" — is the verdict
        the 404-then-block-probe path used to infer, now stated directly. 13 of
        slice 2's recorded failures were this class."""
        self.write_census([(DB_VIEW, "child_database", "A view")])

        def refuse_as_linked(path):
            if path.startswith("/databases/"):
                raise refresh.ApiError(400, json.dumps({
                    "object": "error", "status": 400, "code": "validation_error",
                    "message": f"Database with ID {refresh.dashed(DB_VIEW)} is a linked "
                               f"database. Database retrievals do not support linked databases."}))

        api = FakeApi(dbs={DB_VIEW: db_schema(DB_VIEW, "A view")}, on_get=refuse_as_linked)
        progress, tally = self.run_backfill(api)
        self.assertEqual(1, tally["excluded"])
        self.assertEqual(0, tally["failed"])
        self.assertEqual("not_a_db",
                         coverage_census.load_exclusions(self.excl_path)[DB_VIEW]["reason"])

    def test_any_other_400_stays_a_failure(self):
        """Only Notion's no-accessible-data-source wording is an access verdict; a
        different 400 (a malformed filter, an API change) must stay retryable."""
        self.write_census([(DB_GONE, "child_database", "Odd")])
        api = FakeApi(dbs={DB_GONE: db_schema(DB_GONE, "Odd")}, refuse={DB_GONE: 400})
        progress, tally = self.run_backfill(api)
        self.assertEqual(1, tally["failed"])
        self.assertEqual({}, coverage_census.load_exclusions(self.excl_path))

    def test_a_transient_error_is_recorded_as_a_failure_not_an_exclusion(self):
        self.write_census([(DB_GONE, "child_database", "Gone")])
        api = FakeApi(dbs={DB_GONE: db_schema(DB_GONE, "Gone")}, refuse={DB_GONE: 500})
        progress, tally = self.run_backfill(api)
        self.assertEqual(1, tally["failed"])
        self.assertEqual({}, coverage_census.load_exclusions(self.excl_path))
        self.assertEqual(1, progress["failed"][DB_GONE]["attempts"])


class BudgetStop(BackfillCase):
    """Running out of budget is a normal stop with the remainder intact."""

    def test_budget_stop_keeps_the_remainder(self):
        self.write_census([(DB_A, "child_database", "Alpha"), (DB_B, "child_database", "Beta")])
        # enough for Alpha (schema + query + row body + row comments), not for Beta
        api = self.api_two_dbs(budget=6)
        progress, tally = self.run_backfill(api)
        self.assertEqual("budget", tally["stopped"])
        self.assertIn(DB_A, progress["done"])
        self.assertNotIn(DB_B, progress["done"])
        self.assertEqual({}, coverage_census.load_exclusions(self.excl_path),
                         "a budget stop must not be recorded as a refusal")

        split = coverage_backfill.census_split(self.census_path, self.excl_path)
        counts = coverage_backfill.report_counts(split, progress)
        self.assertEqual(1, counts["remaining"])
        self.assertEqual(1, counts["done"])

    def test_a_database_interrupted_by_budget_is_not_marked_done(self):
        self.write_census([(DB_A, "child_database", "Alpha")])
        api = self.api_two_dbs(budget=1)  # dies right after the schema GET
        progress, tally = self.run_backfill(api)
        self.assertEqual("budget", tally["stopped"])
        self.assertEqual({}, progress["done"])
        self.assertEqual([], [d for d in os.listdir(refresh.DBS) if DB_A in d])


class Pages(BackfillCase):
    """The sub_page path writes the page and its metadata row."""

    def page_payload(self):
        return {"object": "page", "id": refresh.dashed(PAGE_P),
                "parent": {"type": "workspace", "workspace": True},
                "created_time": "2026-01-01T00:00:00.000Z",
                "last_edited_time": "2026-01-02T00:00:00.000Z",
                "archived": False, "in_trash": False, "url": "https://notion.so/p",
                "properties": {"title": {"type": "title",
                                         "title": [{"plain_text": "A page"}]}}}

    def test_page_and_metadata_are_written(self):
        self.write_census([(PAGE_P, "sub_page", "A page")])
        api = FakeApi(pages={PAGE_P: self.page_payload()},
                      blocks={PAGE_P: [para("hello")]})
        progress, tally = self.run_backfill(api)
        self.assertEqual(1, tally["done"])
        md = os.path.join(refresh.WS, f"A page {PAGE_P}.md")
        self.assertTrue(os.path.exists(md))
        self.assertIn("hello", open(md).read())
        meta, _order = refresh.load_meta_jsonl()
        self.assertEqual("A page", meta[PAGE_P]["title"])
        self.assertTrue(os.path.exists(os.path.join(refresh.META, "content-pages.tsv")))

    def test_a_page_body_inline_database_chains(self):
        self.write_census([(PAGE_P, "sub_page", "A page")])
        api = FakeApi(pages={PAGE_P: self.page_payload()},
                      blocks={PAGE_P: [child_db_block(DB_B, "Beta")]},
                      dbs={DB_B: db_schema(DB_B, "Beta")}, rows={DB_B: []})
        progress, tally = self.run_backfill(api)
        self.assertEqual(2, tally["done"])
        self.assertTrue(os.path.isdir(os.path.join(refresh.DBS, f"Beta {DB_B}")))


class Reporting(BackfillCase):
    """--report and --dry-run answer without touching Notion."""

    def test_report_splits_done_remaining_and_excluded_here(self):
        self.write_census([(DB_A, "child_database", "Alpha"), (DB_B, "child_database", "Beta"),
                           (DB_GONE, "child_database", "Gone")])
        self.write_exclusions({DB_B: {"reason": "deliberate", "note": "n", "added": "2026-08-06"}})
        api = FakeApi(dbs={DB_A: db_schema(DB_A, "Alpha")}, rows={DB_A: []})
        progress, _ = self.run_backfill(api)
        split = coverage_backfill.census_split(self.census_path, self.excl_path)
        c = coverage_backfill.report_counts(split, progress)
        self.assertEqual(3, c["absent"])
        self.assertEqual(1, c["pre_excluded"])
        self.assertEqual(1, c["done"])
        self.assertEqual(1, c["excluded_here"])
        self.assertEqual(0, c["remaining"])

    def test_dry_run_makes_no_requests_and_writes_nothing(self):
        self.write_census([(DB_A, "child_database", "Alpha")])
        rc = coverage_backfill.main(["--dry-run", "--census", self.census_path,
                                     "--exclusions", self.excl_path])
        self.assertEqual(0, rc)
        self.assertEqual([], os.listdir(refresh.DBS))
        self.assertFalse(os.path.exists(coverage_backfill.progress_path()))

    def test_a_writing_run_requires_a_budget(self):
        self.write_census([(DB_A, "child_database", "Alpha")])
        with self.assertRaises(SystemExit):
            coverage_backfill.main(["--census", self.census_path,
                                    "--exclusions", self.excl_path])


class Locking(BackfillCase):
    """The mirror's internal lock, taken directly — this is a mirror writer."""

    def test_second_holder_is_refused(self):
        path = os.path.join(self.tmp, "lock")
        held = coverage_backfill.take_lock(path)
        self.addCleanup(held.close)
        with self.assertRaises(coverage_backfill.Locked):
            coverage_backfill.take_lock(path)

    def test_main_exits_nonzero_when_the_lock_is_held(self):
        self.write_census([(DB_A, "child_database", "Alpha")])
        path = os.path.join(self.tmp, "lock")
        held = coverage_backfill.take_lock(path)
        self.addCleanup(held.close)
        os.environ.setdefault("NOTION_TOKEN", "test-token")
        rc = coverage_backfill.main(["--budget", "10", "--lock", path,
                                     "--census", self.census_path,
                                     "--exclusions", self.excl_path])
        self.assertEqual(1, rc)


if __name__ == "__main__":
    unittest.main()
