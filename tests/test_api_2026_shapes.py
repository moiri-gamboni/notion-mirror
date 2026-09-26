"""The mirror reads Notion at 2026-03-11 and writes what it wrote at 2022-06-28.

Two response shapes changed under the engine with the version, and a side-by-side
run of both versions over copies of the same mirror (2026-09-26) showed each:

* `GET /databases/{id}` is now the container. Its title, icon and description
  are the container's own: a blank title reads "New database" (13 directories
  renamed), a database mention in a title or description reads as a page
  mention "Untitled" (3 databases), and 5 containers carry a different icon.
  The collection the mirror has always shown is the data source, whose object
  has the 2022-06-28 database object's shape field for field. A single-source
  database's block is that object under the database's id and parent.
* A wiki database lists its child databases among its rows. 2022-06-28 gave
  each as a `database` object under the database's id; 2026-03-11 gives the
  child's data source, under the source's id. Kept as they came, 23 row files
  moved to new names and their body reads 404'd.

Payloads follow the shape of live responses from that run, with invented ids and names.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import refresh  # noqa: E402

DB = "00000000-0000-0000-0000-00000000745c"
DS = "00000000-0000-0000-0000-00000000dc60"
PAGE = "00000000-0000-0000-0000-000000009b55"
USER = {"object": "user", "id": "00000000-0000-0000-0000-00000000ca04"}
MENTION = {"type": "mention", "mention": {"type": "database", "database": {"id": "00000000-0000-0000-0000-000000009c85"}},
           "plain_text": "Archived Projects"}
PROPS = {"Name": {"id": "title", "name": "Name", "type": "title", "title": {}}}


def container(title="New database", icon=None):
    return {"object": "database", "id": DB, "title": [{"type": "text", "plain_text": title}],
            "description": [{"type": "mention", "mention": {"type": "page", "page": {"id": "x"}},
                             "plain_text": "Untitled"}],
            "parent": {"type": "page_id", "page_id": PAGE}, "is_inline": True, "database_type": None,
            "in_trash": False, "is_locked": False,
            "created_time": "2023-12-06T14:33:12.345+00:00", "last_edited_time": "2023-12-16T23:08:41.000+00:00",
            "data_sources": [{"id": DS, "name": title}], "icon": icon, "cover": None,
            "url": "https://app.notion.com/p/0000000000000000000000000000745c", "public_url": None,
            "request_id": "r1"}


def data_source(title=(), description=(MENTION,), icon=None):
    return {"object": "data_source", "id": DS, "cover": None, "icon": icon,
            "created_time": "2023-12-06T14:33:00.000Z", "created_by": USER, "last_edited_by": USER,
            "last_edited_time": "2023-12-16T23:08:00.000Z", "title": list(title),
            "description": list(description), "is_inline": True, "database_type": None,
            "properties": PROPS, "parent": {"type": "database_id", "database_id": DB},
            "database_parent": {"type": "page_id", "page_id": PAGE},
            "url": "https://app.notion.com/p/0000000000000000000000000000745c", "public_url": None,
            "in_trash": False, "request_id": "r2"}


def written_at_2022(title=(), description=(MENTION,), icon=None):
    """The block the mirror holds for this database, as 2022-06-28 served it."""
    return {"object": "database", "id": DB, "cover": None, "icon": icon,
            "created_time": "2023-12-06T14:33:00.000Z", "created_by": USER, "last_edited_by": USER,
            "last_edited_time": "2023-12-16T23:08:00.000Z", "title": list(title),
            "description": list(description), "is_inline": True, "database_type": None,
            "properties": PROPS, "parent": {"type": "page_id", "page_id": PAGE},
            "url": "https://app.notion.com/p/0000000000000000000000000000745c", "public_url": None,
            "in_trash": False, "archived": False}


class Api:
    def __init__(self, db, ds, rows=()):
        self.db, self.ds, self.rows = db, ds, list(rows)
        self.paths = []

    def get(self, path, params=None, ver=None):
        self.paths.append(path)
        if path == f"/databases/{DB}":
            return dict(self.db)
        if path == f"/data_sources/{DS}":
            return dict(self.ds)
        raise AssertionError(f"unexpected GET {path}")

    def query_rows(self, path, body=None, ver=None):
        self.paths.append(path)
        assert path == f"/data_sources/{DS}/query", path
        return [dict(r) for r in self.rows]


class SingleSourceBlock(unittest.TestCase):
    def test_the_block_is_the_2022_block_but_for_archived(self):
        """Same keys in the same order, so a steady database's file is unchanged
        but for the one field 2026-03-11 removed (`in_trash` carries it)."""
        d, sources = refresh.get_database(Api(container(), data_source()), undash(DB))
        want = written_at_2022()
        del want["archived"]
        self.assertEqual(list(d.items()), list(want.items()))
        self.assertEqual(sources, [{"id": DS, "name": "New database"}])

    def test_a_blank_title_stays_blank(self):
        d, _ = refresh.get_database(Api(container(), data_source()), undash(DB))
        self.assertEqual(refresh.plain(d["title"]), "")

    def test_a_database_mention_keeps_its_text(self):
        d, _ = refresh.get_database(Api(container(), data_source(title=(MENTION,))), undash(DB))
        self.assertEqual(refresh.plain(d["title"]), "Archived Projects")
        self.assertEqual(refresh.plain(d["description"]), "Archived Projects")

    def test_the_icon_is_the_collections(self):
        script = {"type": "icon", "icon": {"name": "script", "color": "gray"}}
        drafts = {"type": "icon", "icon": {"name": "drafts", "color": "gray"}}
        d, _ = refresh.get_database(Api(container(icon=drafts), data_source(icon=script)), undash(DB))
        self.assertEqual(d["icon"], script)

    def test_a_single_source_shows_no_sources_line(self):
        d, sources = refresh.get_database(Api(container(), data_source()), undash(DB))
        self.assertNotIn("Data sources", refresh.schema_md("T", undash(DB), 1, d["properties"], sources))


def child(db_id, ds_id, title):
    """A wiki's child database as its 2026-03-11 row query lists it."""
    return {"object": "data_source", "id": ds_id, "title": [{"plain_text": title}],
            "parent": {"type": "database_id", "database_id": db_id},
            "database_parent": {"type": "page_id", "page_id": PAGE},
            "last_edited_time": "2024-11-12T23:06:00.000Z", "properties": PROPS, "in_trash": False}


class WikiChildDatabases(unittest.TestCase):
    CHILD_DB, CHILD_DS = "00000000-0000-0000-0000-000000005552", "00000000-0000-0000-0000-000000003dc8"

    def rows(self):
        page = {"object": "page", "id": "00000000-0000-0000-0000-0000000001ec",
                "parent": {"type": "data_source_id", "data_source_id": DS, "database_id": DB}}
        api = Api(container(), data_source(), rows=[page, child(self.CHILD_DB, self.CHILD_DS, "Authorship")])
        rows, _extra = refresh.query_db_rows(api, undash(DB))
        return rows

    def test_a_child_database_keeps_its_database_id(self):
        entry = self.rows()[1]
        self.assertEqual(entry["id"], self.CHILD_DB)
        self.assertEqual(entry["object"], "database")
        self.assertEqual(entry["parent"], {"type": "page_id", "page_id": PAGE})
        self.assertNotIn("database_parent", entry)

    def test_a_page_row_is_untouched(self):
        self.assertEqual(self.rows()[0]["object"], "page")
        self.assertEqual(self.rows()[0]["id"], "00000000-0000-0000-0000-0000000001ec")


def undash(s):
    return s.replace("-", "")


if __name__ == "__main__":
    unittest.main()
