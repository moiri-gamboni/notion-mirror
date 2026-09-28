"""A database row is mirrored once: as its row file.

A workspace export, and one later re-capture, wrote a page file under the page tree for
many database rows as well: the row's properties as `Key: value` lines and a body
snapshot, never updated since, because the content phase walks content pages only.
`--mode row-page-dedup` removes those page files once the row file carries the probe
marker (so the row's body has been read), repoints markdown links that led to them at
the row file, drops folders the removal empties, and keeps any page file whose row has
not been probed. No requests.
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import refresh  # noqa: E402
from fake_notion import MirrorSandbox, page_obj  # noqa: E402

DB, ROW, ROW2, PAGE = "c3" * 16, "b2" * 16, "b4" * 16, "a1" * 16


def row_obj(rid, title):
    p = page_obj(rid, title)
    p["parent"] = {"type": "data_source_id", "data_source_id": refresh.dashed("d5" * 16),
                   "database_id": refresh.dashed(DB)}
    return p


class RowPageDedup(MirrorSandbox):
    def setUp(self):
        super().setUp()
        dbdir = os.path.join(self.dbs, f"Tasks {DB}")
        os.makedirs(dbdir)
        self.row = os.path.join(dbdir, f"Draft the plan {ROW}.md")
        with open(self.row, "w") as f:
            f.write("<!-- notion db row -->\n# Draft the plan\n\n| Property | Value |\n|---|---|\n"
                    "| Status | Done |\n| Owner | Someone |\n\n" + refresh.MARKER + "\n")
        self.row2 = os.path.join(dbdir, f"Unprobed row {ROW2}.md")
        with open(self.row2, "w") as f:
            f.write("<!-- notion db row -->\n# Unprobed row\n\n| Property | Value |\n")
        self.dup = self.write_page("Hub/Tasks", ROW, "Draft the plan", body="Status: Done\nOwner: Someone")
        self.dup2 = self.write_page("Hub/Tasks", ROW2, "Unprobed row", body="Status: Open")
        self.hub = self.write_page("", PAGE, "Hub",
                                   body=f"See [Draft the plan](Hub/Tasks/Draft%20the%20plan%20{ROW}.md) today.")
        self.write_meta(row_obj(ROW, "Draft the plan"), row_obj(ROW2, "Unprobed row"), page_obj(PAGE, "Hub"))
        self.report = refresh.new_report("row-page-dedup")

    def run_pass(self, dry=False):
        refresh.phase_row_page_dedup(self.report, types.SimpleNamespace(dry_run=dry))
        return self.report["row_page_dedup"]

    def test_a_probed_rows_page_file_goes(self):
        st = self.run_pass()
        self.assertFalse(os.path.exists(self.dup))
        self.assertTrue(os.path.exists(self.row))
        self.assertEqual(st["removed"], 1)

    def test_an_unprobed_rows_page_file_stays(self):
        st = self.run_pass()
        self.assertTrue(os.path.exists(self.dup2))
        self.assertEqual(st["kept_unprobed"], [ROW2])

    def test_a_link_to_the_page_file_now_leads_to_the_row_file(self):
        self.run_pass()
        txt = self.read(self.hub)
        self.assertIn(f"](_databases/Tasks%20{DB}/Draft%20the%20plan%20{ROW}.md)", txt)
        self.assertEqual(self.report["row_page_dedup"]["links_rewritten"], 1)

    def test_a_folder_the_removal_empties_goes_too(self):
        os.remove(self.dup2)
        self.run_pass()
        self.assertFalse(os.path.exists(os.path.dirname(self.dup)))
        self.assertTrue(os.path.exists(self.hub))

    def test_a_dry_run_changes_nothing(self):
        before = (self.read(self.dup), self.read(self.hub))
        st = self.run_pass(dry=True)
        self.assertEqual(before, (self.read(self.dup), self.read(self.hub)))
        self.assertEqual(st["removed"], 1)

    def test_a_page_file_holding_a_value_the_row_lacks_stays(self):
        """An export lists a relation into a database the integration cannot see;
        the API leaves that property out, so the page file is its only copy."""
        with open(self.dup, "w") as f:
            f.write("# Draft the plan\n\nStatus: Done\nLinked sprint: Winter Sprint (Sprints/Winter%20Sprint.md)\n")
        st = self.run_pass()
        self.assertTrue(os.path.exists(self.dup))
        self.assertEqual(st["kept_unique"], {ROW: ["Linked sprint"]})

    def test_the_mirrors_own_header_comment_is_not_a_property(self):
        with open(self.dup, "w") as f:
            f.write("# Draft the plan\n\n<!-- notion page id: x | parent: {\"type\": \"root\"} -->\n\nbody\n")
        self.run_pass()
        self.assertFalse(os.path.exists(self.dup))

    def test_a_value_the_row_file_carries_does_not_keep_it(self):
        with open(self.row, "a") as f:
            f.write("\n## Body\n\nOwner: Someone\n")
        with open(self.dup, "w") as f:
            f.write("# Draft the plan\n\nOwner: Someone\n")
        self.run_pass()
        self.assertFalse(os.path.exists(self.dup))

    def test_a_content_page_is_never_touched(self):
        self.run_pass()
        self.assertTrue(os.path.exists(self.hub))


class ContentPhaseNeverWritesARowAsAPage(MirrorSandbox):
    def test_a_changed_row_in_the_page_search_gets_no_page_file(self):
        """The content phase walks pages whose parent is a page, a block or the
        workspace; a row (parent a data source) only has its metadata refreshed,
        so nothing recreates the files the pass removes."""
        from fake_notion import FakeNotion, paragraph
        self.write_meta(row_obj(ROW, "Draft the plan"))
        api = FakeNotion(search=[dict(row_obj(ROW, "Draft the plan"),
                                      last_edited_time="2026-09-24T00:00:00.000Z")],
                         children={ROW: [paragraph("44" * 16, "body")]})
        refresh.phase_content(api, self.users, self.state(), refresh.new_report("daily"),
                              types.SimpleNamespace(dry_run=False), "daily", set())
        self.assertEqual(api.walked(), [])
        self.assertEqual(refresh.index_workspace_pages(), {})


if __name__ == "__main__":
    unittest.main()
