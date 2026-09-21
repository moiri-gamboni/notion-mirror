"""Offline scaffolding for the golden-fixture renderer tests.

A fixture is a captured Notion block payload plus the child payloads the walker
would otherwise fetch over the wire, so `Walker.render` runs with zero requests.
Fixture JSON:

    {"name": "to_do",
     "provenance": "live | synthetic — where it came from",
     "indent": 0,                  # optional, default 0
     "covers": ["to_do"],          # optional, extra type names this fixture proves
     "blocks": [<block payload>, ...],
     "children": {"<block id>": [<block payload>, ...]}}

Expected output lives beside it in `fixtures/expected/<name>.md`.
"""
import json
import os
import re
import sys

TESTS = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(TESTS)
FIXTURES = os.path.join(TESTS, "fixtures")
BLOCKS = os.path.join(FIXTURES, "blocks")
EXPECTED = os.path.join(FIXTURES, "expected")

# _tools is not a package; unittest discovery puts it on the path, running a
# single test file directly does not
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import refresh  # noqa: E402


class FakeApi:
    """Serves /blocks/{id}/children from the fixture. Any other call is a bug in
    the test: fixture mode must never reach the network."""

    CHILDREN = re.compile(r"^/blocks/([0-9a-f-]{32,36})/children$")

    def __init__(self, children=None):
        self.children = {refresh.undash(k): v for k, v in (children or {}).items()}
        self.n = 0
        self.r429 = 0

    def paginate(self, method, path, body=None, params=None, ver=None):
        m = self.CHILDREN.match(path)
        if not m or method != "GET":
            raise AssertionError(f"fixture mode attempted a live call: {method} {path}")
        self.n += 1
        yield from self.children.get(refresh.undash(m.group(1)), [])


class FakeUsers:
    """Walker only resolves users for comments, which fixtures don't exercise."""

    def name(self, ref):
        return "Someone"


def fixture_names():
    return sorted(f[:-5] for f in os.listdir(BLOCKS) if f.endswith(".json"))


def load_fixture(name):
    with open(os.path.join(BLOCKS, name + ".json")) as f:
        return json.load(f)


def render_fixture(fx):
    """-> (rendered text, walker, api). Text ends in a newline so the expected
    files are ordinary text files."""
    api = FakeApi(fx.get("children"))
    w = refresh.Walker(api, FakeUsers())
    lines = []
    for b in fx["blocks"]:
        w.render(b, lines, fx.get("indent", 0))
    return "\n".join(lines) + "\n", w, api


def _as_read_back(items):
    """A rich_text list as the API hands it back: `plain_text` and `href`, which
    every renderer reads and no writer sends."""
    out = []
    for item in items or []:
        item = dict(item)
        text = item.get("text") or {}
        item.setdefault("plain_text", text.get("content", ""))
        if (text.get("link") or {}).get("url"):
            item.setdefault("href", text["link"]["url"])
        item.setdefault("annotations", {})
        out.append(item)
    return out


def as_payloads(blocks, children, counter=None):
    """Converter output as Notion returns it: ids, `has_children`, children hoisted
    into the map a `FakeApi` serves. The one transformation Notion itself performs
    between a write and the next read."""
    counter = counter if counter is not None else [0]
    out = []
    for block in blocks:
        counter[0] += 1
        bid = f"{counter[0]:032x}"
        btype = block["type"]
        data = dict(block[btype])
        kids = data.pop("children", None)
        for field in ("rich_text", "caption"):
            if field in data:
                data[field] = _as_read_back(data[field])
        if btype == "table":
            children[bid] = [{"id": f"{counter[0]:031x}r", "type": "table_row",
                              "has_children": False,
                              "table_row": {"cells": [_as_read_back(c)
                                                      for c in row["table_row"]["cells"]]}}
                             for row in kids or []]
            out.append({"id": bid, "type": btype, btype: data, "has_children": True})
            continue
        out.append({"id": bid, "type": btype, btype: data, "has_children": bool(kids)})
        if kids:
            children[bid] = as_payloads(kids, children, counter)
    return out


def render_blocks(blocks):
    """Converter output rendered back to markdown, through the real renderer."""
    children = {}
    return render_fixture({"blocks": as_payloads(blocks, children),
                           "children": children})[0].rstrip("\n")


def expected_path(name):
    return os.path.join(EXPECTED, name + ".md")


def read_expected(name):
    with open(expected_path(name)) as f:
        return f.read()


def types_in(payloads):
    """Every block type appearing anywhere in a list of payloads."""
    out = set()
    for b in payloads or []:
        t = b.get("type")
        if t:
            out.add(t)
        data = b.get(t, {}) or {}
        if isinstance(data, dict):
            out |= types_in(data.get("children"))
    return out


def fixture_covers(fx):
    covered = types_in(fx.get("blocks"))
    for kids in (fx.get("children") or {}).values():
        covered |= types_in(kids)
    return covered | set(fx.get("covers") or [])


def dispatched_types():
    """Block types `Walker.render` branches on, read out of its own source, so a
    new branch without a fixture fails the suite."""
    import inspect
    src = inspect.getsource(refresh.Walker.render)
    out = set(re.findall(r't == "([a-z_0-9]+)"', src))
    for group in re.findall(r"t in \(([^)]*)\)", src):
        out |= set(re.findall(r'"([a-z_0-9]+)"', group))
    return out
