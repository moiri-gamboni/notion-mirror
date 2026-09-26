"""A small offline Notion for phase-level tests, and a temp mirror to run them in.

`FakeNotion` serves the handful of endpoints the content, comment and row phases
use from plain dicts and records every request by path, so a test can assert on
what a phase asked for — which is what the comment design is about — rather than
only on what it wrote. Anything it was not given raises: a green run is the
proof that nothing reached Notion.
"""
import collections
import contextlib
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

import refresh


def rt(text):
    return [{"type": "text", "plain_text": text, "href": None,
             "text": {"content": text, "link": None},
             "annotations": {"bold": False, "italic": False, "strikethrough": False,
                             "underline": False, "code": False, "color": "default"}}]


def paragraph(bid, text, has_children=False):
    return {"object": "block", "id": refresh.dashed(bid), "type": "paragraph",
            "has_children": has_children, "paragraph": {"rich_text": rt(text)}}


def comment(cid, text, did=None, who="uu" * 16, when="2026-09-01T09:00:00.000Z"):
    return {"id": refresh.dashed(cid), "discussion_id": refresh.dashed(did or cid),
            "rich_text": rt(text), "created_by": {"id": who, "name": "Someone"},
            "created_time": when}


def page_obj(pid, title, parent_type="workspace", parent_id="",
             le="2026-09-24T00:00:00.000Z", db_props=None):
    par = {"type": parent_type}
    if parent_type == "workspace":
        par["workspace"] = True
    else:
        par[parent_type] = refresh.dashed(parent_id)
    props = db_props or {"title": {"id": "title", "type": "title", "title": rt(title)}}
    return {"object": "page", "id": refresh.dashed(pid), "properties": props, "parent": par,
            "created_time": "2026-01-01T00:00:00.000Z", "last_edited_time": le,
            "created_by": {"id": "uu" * 16, "name": "Someone"},
            "last_edited_by": {"id": "uu" * 16, "name": "Someone"},
            "archived": False, "in_trash": False, "url": "", "public_url": None}


class FakeUsers:
    def name(self, ref):
        if not ref:
            return ""
        return (ref.get("name") if isinstance(ref, dict) else None) or "Someone"

    def save(self):
        pass


class FakeNotion:
    CHILDREN = re.compile(r"^/blocks/([0-9a-f-]{32,36})/children$")

    def __init__(self, search=(), pages=None, children=None, comments=None, dbs=None,
                 comment_parents=None, gone_blocks=()):
        self.search = list(search)
        # comment id32 -> (parent type, parent id32), what GET /comments/{id} answers
        self.comment_parents = {refresh.undash(k): v for k, v in (comment_parents or {}).items()}
        self.gone_blocks = {refresh.undash(b) for b in gone_blocks}
        # db id32 -> (GET /databases/{id} response, [row page objects])
        self.dbs = {refresh.undash(k): v for k, v in (dbs or {}).items()}
        self.pages = {refresh.undash(k): v for k, v in (pages or {}).items()}
        self.children = {refresh.undash(k): v for k, v in (children or {}).items()}
        self.comments = {refresh.undash(k): v for k, v in (comments or {}).items()}
        self.n = 0
        self.r429 = 0
        self.by_endpoint = collections.Counter()
        self.calls = []

    def _count(self, method, path, params=None):
        self.n += 1
        from notion_core.api import endpoint_class
        self.by_endpoint[endpoint_class(method, path)] += 1
        key = path if path != "/comments" else f"/comments?{refresh.undash(params['block_id'])}"
        self.calls.append(key)

    def get(self, path, params=None, ver=None):
        self._count("GET", path, params)
        if path.startswith("/pages/"):
            pid = refresh.undash(path.split("/pages/", 1)[1])
            if pid in self.pages:
                return self.pages[pid]
            raise refresh.ApiError(404, "not found")
        if path.startswith("/comments/"):
            cid = refresh.undash(path.split("/comments/", 1)[1])
            if cid not in self.comment_parents:
                raise refresh.ApiError(404, "object_not_found")
            kind, pid = self.comment_parents[cid]
            return {"object": "comment", "id": refresh.dashed(cid),
                    "parent": {"type": kind, kind: refresh.dashed(pid)}}
        if path.startswith("/databases/"):
            # 2026-03-11: a database lists its data sources (one, same id, here)
            did = refresh.undash(path.split("/databases/", 1)[1])
            if did in self.dbs:
                d = {k: v for k, v in self.dbs[did][0].items() if k != "properties"}
                d.setdefault("data_sources", [{"id": refresh.dashed(did), "name": "src"}])
                return d
            raise refresh.ApiError(404, "not found")
        if path.startswith("/data_sources/"):
            did = refresh.undash(path.split("/data_sources/", 1)[1])
            if did in self.dbs:
                return {"object": "data_source",
                        "properties": dict(self.dbs[did][0].get("properties") or {})}
            raise refresh.ApiError(404, "not found")
        raise AssertionError(f"unexpected GET {path}")

    def query_rows(self, path, body=None, ver=None):
        self._count("POST", path)
        did = refresh.undash(path.split("/", 2)[2].split("/", 1)[0])
        if did not in self.dbs:
            raise refresh.ApiError(404, "not found")
        return list(self.dbs[did][1])

    def post(self, path, body=None, ver=None):
        raise AssertionError(f"unexpected POST {path}")

    def paginate(self, method, path, body=None, params=None, ver=None):
        self._count(method, path, params)
        if method == "POST" and path == "/search":
            if ((body or {}).get("filter") or {}).get("value") == "page":
                yield from self.search
            return
        if path == "/comments":
            if refresh.undash(params["block_id"]) in self.gone_blocks:
                raise refresh.ApiError(404, "object_not_found")
            yield from self.comments.get(refresh.undash(params["block_id"]), [])
            return
        m = self.CHILDREN.match(path)
        if m and method == "GET":
            yield from self.children.get(refresh.undash(m.group(1)), [])
            return
        raise AssertionError(f"unexpected {method} {path}")

    def rate(self, rps):
        return contextlib.nullcontext()

    def comment_calls(self):
        return [c.split("?", 1)[1] for c in self.calls if c.startswith("/comments?")]

    def walked(self):
        return [refresh.undash(m.group(1)) for c in self.calls
                for m in [self.CHILDREN.match(c)] if m]


class MirrorSandbox(unittest.TestCase):
    """A temp mirror with the engine's roots pointed at it."""

    AUTOMATION = ("Hub/Auto",)

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="nm-sandbox-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.ws = os.path.join(self.root, "workspace")
        self.dbs = os.path.join(self.ws, "_databases")
        self.meta = os.path.join(self.root, "_meta")
        self.state_dir = os.path.join(self.meta, "state")
        for d in (self.dbs, self.state_dir):
            os.makedirs(d)
        for name, value in (("NOTION", self.root), ("WS", self.ws), ("DBS", self.dbs),
                            ("META", self.meta), ("STATE", self.state_dir),
                            ("AUTOMATION_SUBTREES", self.AUTOMATION),
                            ("_WEBHOOK_CAPTURES", None)):
            p = mock.patch.object(refresh, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.users = FakeUsers()

    def write_page(self, rel, pid, title, body="body"):
        path = os.path.join(self.ws, rel, f"{title} {pid}.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(f"# {title}\n\n{body}\n")
        return path

    def write_meta(self, *pages):
        m = {}
        order = []
        for p in pages:
            mm = refresh.meta_of(p, self.users)
            m[mm["id"]] = mm
            order.append(mm["id"])
        refresh.save_meta_jsonl(m, order)

    def state(self, **extra):
        st = {"rows": {}, "db404": {}, "not_a_db": {}, "unshared": {}, "probe_policy": {},
              "content_since": "", "comment_scans": {}, "retry_pages": {}, "page_walks": {},
              "comment_parents": {},
              "comment_rows": {}}
        st.update(extra)
        return st

    def read(self, path):
        with open(path) as f:
            return f.read()
