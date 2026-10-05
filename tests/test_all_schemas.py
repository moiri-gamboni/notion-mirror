"""`_ALL-SCHEMAS.md` keeps one section per database, whichever one is written.

The section pattern ran `^## .*` under re.S, so `.*` crossed lines and a match
began at the file's first heading: updating or removing any database's section
replaced every section above it. The committed index swung between 0 and 731
sections from one night to the next.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import refresh  # noqa: E402

PROPS = {"Name": {"id": "title", "name": "Name", "type": "title", "title": {}}}
A, B, C = "a" * 32, "b" * 32, "c" * 32


class AllSchemasTest(unittest.TestCase):
    def setUp(self):
        self.dbs = tempfile.mkdtemp(prefix="all-schemas-")
        self.addCleanup(shutil.rmtree, self.dbs, ignore_errors=True)
        p = mock.patch.object(refresh, "DBS", self.dbs)
        p.start()
        self.addCleanup(p.stop)
        for title, db in (("Alpha", A), ("Beta", B), ("Gamma", C)):
            refresh.update_all_schemas(title, db, 1, PROPS)

    def headings(self):
        txt = open(os.path.join(self.dbs, "_ALL-SCHEMAS.md")).read()
        return [l for l in txt.splitlines() if l.startswith("## ")]

    def test_updating_a_later_section_keeps_the_ones_above(self):
        refresh.update_all_schemas("Beta", B, 7, PROPS)
        self.assertEqual(len(self.headings()), 3)
        self.assertIn(f"## Beta  `{B}`  (7 rows, 1 props)", self.headings())

    def test_removing_a_later_section_keeps_the_ones_above(self):
        refresh.remove_all_schemas_section(C)
        self.assertEqual([h.split("`")[1] for h in self.headings()], [A, B])


if __name__ == "__main__":
    unittest.main()
