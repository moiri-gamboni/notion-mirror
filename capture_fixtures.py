#!/usr/bin/env python3
"""One-shot fixture recorder for the renderer tests (tests/fixtures/).

Three verbs, all safe to re-run:

  capture    walk live pages read-only at ~1 rps, recording one payload per
             block type that has no fixture yet (never overwrites a fixture)
  synthetic  write the hand-authored fixtures for types the workspace has no
             instance of, plus the nesting/annotation cases a live walk rarely
             produces in isolation
  expected   re-render every fixture and rewrite tests/fixtures/expected/*.md

`expected` is the migration step for a renderer change: rerun it and review the
diff. It needs no token and makes no requests.

Signed-URL credentials (X-Amz-*) are stripped from captured file blocks before
anything is written — they expire in an hour and must not be committed.

Env: NOTION_TOKEN required for `capture` only. Stdlib only.
"""
import argparse
import json
import os
import sys
import urllib.parse

# Code, not data: this script reads and writes only the fixtures beside it, so it needs
# no workspace of its own — `refresh` brings the data roots with it when it is imported.
TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
sys.path.insert(0, os.path.join(TOOLS, "tests"))

import refresh  # noqa: E402
from fixture_support import BLOCKS, EXPECTED, fixture_names, load_fixture, render_fixture  # noqa: E402

# types worth hunting for live; the rest are hand-authored below
WANTED = [
    "paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item",
    "numbered_list_item", "to_do", "toggle", "quote", "callout", "code",
    "divider", "table", "image", "child_page", "child_database", "bookmark",
    "equation", "link_to_page", "table_of_contents", "column_list",
    "synced_block", "template", "breadcrumb", "embed", "video", "file", "pdf",
]

# hand-authored payloads, shaped per the Notion block-object docs. Used for
# types the workspace has no reachable instance of, and for the nesting and
# annotation paths a live walk almost never hands you in isolation.
SYNTHETIC = {
    "unknown": {
        "provenance": "synthetic — no such block type exists; pins the fallback branch",
        "blocks": [
            {"id": "0" * 32, "type": "ai_block", "has_children": False,
             "ai_block": {"rich_text": []}},
            {"id": "1" * 32, "type": "unsupported_with_text", "has_children": False,
             "unsupported_with_text": {"rich_text": [
                 {"type": "text", "plain_text": "text the fallback still renders",
                  "annotations": {}}]}},
        ],
        "covers": ["ai_block"],
    },
    "paragraph_rich": {
        "provenance": "synthetic — the rich_md annotation matrix in one block",
        "blocks": [
            {"id": "2" * 32, "type": "paragraph", "has_children": False, "paragraph": {
                "rich_text": [
                    {"type": "text", "plain_text": "plain ", "annotations": {}},
                    {"type": "text", "plain_text": "bold", "annotations": {"bold": True}},
                    {"type": "text", "plain_text": " ital ", "annotations": {"italic": True}},
                    {"type": "text", "plain_text": "code", "annotations": {"code": True}},
                    {"type": "text", "plain_text": " struck ", "annotations": {"strikethrough": True}},
                    {"type": "text", "plain_text": "under", "annotations": {"underline": True}},
                    {"type": "text", "plain_text": " link", "annotations": {},
                     "href": "https://example.org/x"},
                    {"type": "equation", "plain_text": "e^{i\\pi}",
                     "equation": {"expression": "e^{i\\pi}"}, "annotations": {}},
                    {"type": "text", "plain_text": " **bold+ital** ",
                     "annotations": {"bold": True, "italic": True}},
                ]}},
            {"id": "3" * 32, "type": "paragraph", "has_children": False, "paragraph": {
                "rich_text": [{"type": "text", "plain_text": "first line\nsecond line\nthird",
                               "annotations": {}}]}},
            {"id": "4" * 32, "type": "paragraph", "has_children": False,
             "paragraph": {"rich_text": []}},
        ],
    },
    "nesting": {
        "provenance": "synthetic — list/toggle children indent one level; quote children stay quoted",
        "blocks": [
            {"id": "a" * 32, "type": "bulleted_list_item", "has_children": True,
             "bulleted_list_item": {"rich_text": [
                 {"type": "text", "plain_text": "outer", "annotations": {}}]}},
            {"id": "b" * 32, "type": "quote", "has_children": True, "quote": {
                "rich_text": [{"type": "text", "plain_text": "quoted", "annotations": {}}]}},
        ],
        "children": {
            "a" * 32: [
                {"id": "c" * 32, "type": "numbered_list_item", "has_children": True,
                 "numbered_list_item": {"rich_text": [
                     {"type": "text", "plain_text": "inner", "annotations": {}}]}},
                {"id": "d" * 32, "type": "to_do", "has_children": False,
                 "to_do": {"checked": False, "rich_text": [
                     {"type": "text", "plain_text": "inner todo", "annotations": {}}]}},
            ],
            "c" * 32: [
                {"id": "e" * 32, "type": "paragraph", "has_children": False, "paragraph": {
                    "rich_text": [{"type": "text", "plain_text": "deepest", "annotations": {}}]}},
            ],
            "b" * 32: [
                {"id": "f" * 32, "type": "paragraph", "has_children": False, "paragraph": {
                    "rich_text": [{"type": "text", "plain_text": "inside the quote",
                                   "annotations": {}}]}},
                {"id": "0a" + "0" * 30, "type": "bulleted_list_item", "has_children": False,
                 "bulleted_list_item": {"rich_text": [
                     {"type": "text", "plain_text": "quoted bullet", "annotations": {}}]}},
            ],
        },
    },
    "row_mode_headings": {
        "provenance": "synthetic — a heading sits at its own depth and its children go one "
                      "level deeper. Kept under its original name: it was a `row_mode` "
                      "fixture until 2026-08-27, when the flat content-page spelling it "
                      "contrasted with turned out to be a bug and both modes became one.",
        "indent": 0,
        "blocks": [
            {"id": "1a" + "0" * 30, "type": "heading_2", "has_children": True, "heading_2": {
                "rich_text": [{"type": "text", "plain_text": "Section", "annotations": {}}]}},
        ],
        "children": {
            "1a" + "0" * 30: [
                {"id": "1b" + "0" * 30, "type": "paragraph", "has_children": False, "paragraph": {
                    "rich_text": [{"type": "text", "plain_text": "under the heading",
                                   "annotations": {}}]}},
            ],
        },
    },
    "synced_block": {
        "provenance": "synthetic — a reference synced_block walks the ORIGINAL block's children",
        "blocks": [
            {"id": "2a" + "0" * 30, "type": "synced_block", "has_children": True,
             "synced_block": {"synced_from": {"type": "block_id", "block_id": "2b" + "0" * 30}}},
            {"id": "2c" + "0" * 30, "type": "synced_block", "has_children": True,
             "synced_block": {"synced_from": None}},
        ],
        "children": {
            "2b" + "0" * 30: [
                {"id": "2d" + "0" * 30, "type": "paragraph", "has_children": False, "paragraph": {
                    "rich_text": [{"type": "text", "plain_text": "content of the original",
                                   "annotations": {}}]}},
            ],
            "2c" + "0" * 30: [
                {"id": "2e" + "0" * 30, "type": "paragraph", "has_children": False, "paragraph": {
                    "rich_text": [{"type": "text", "plain_text": "content of an original block",
                                   "annotations": {}}]}},
            ],
        },
    },
    "column_list": {
        "provenance": "synthetic — column_list/column emit nothing themselves; children walk flat",
        "blocks": [
            {"id": "3a" + "0" * 30, "type": "column_list", "has_children": True,
             "column_list": {}},
        ],
        "children": {
            "3a" + "0" * 30: [
                {"id": "3b" + "0" * 30, "type": "column", "has_children": True, "column": {}},
                {"id": "3c" + "0" * 30, "type": "column", "has_children": True, "column": {}},
            ],
            "3b" + "0" * 30: [
                {"id": "3d" + "0" * 30, "type": "paragraph", "has_children": False, "paragraph": {
                    "rich_text": [{"type": "text", "plain_text": "left", "annotations": {}}]}},
            ],
            "3c" + "0" * 30: [
                {"id": "3e" + "0" * 30, "type": "paragraph", "has_children": False, "paragraph": {
                    "rich_text": [{"type": "text", "plain_text": "right", "annotations": {}}]}},
            ],
        },
    },
    "toggle_callout": {
        "provenance": "synthetic — toggle and callout, both with children (callout children stay quoted)",
        "blocks": [
            {"id": "b1" + "0" * 30, "type": "toggle", "has_children": True, "toggle": {
                "rich_text": [{"type": "text", "plain_text": "Details", "annotations": {}}]}},
            {"id": "b2" + "0" * 30, "type": "callout", "has_children": True, "callout": {
                "icon": {"type": "emoji", "emoji": "💡"},
                "rich_text": [{"type": "text", "plain_text": "Worth knowing", "annotations": {}}]}},
            {"id": "b3" + "0" * 30, "type": "callout", "has_children": False, "callout": {
                "icon": {"type": "external", "external": {"url": "https://example.org/i.png"}},
                "rich_text": [{"type": "text", "plain_text": "no emoji icon", "annotations": {}}]}},
        ],
        "children": {
            "b1" + "0" * 30: [
                {"id": "b4" + "0" * 30, "type": "paragraph", "has_children": False, "paragraph": {
                    "rich_text": [{"type": "text", "plain_text": "hidden until opened",
                                   "annotations": {}}]}},
            ],
            "b2" + "0" * 30: [
                {"id": "b5" + "0" * 30, "type": "bulleted_list_item", "has_children": False,
                 "bulleted_list_item": {"rich_text": [
                     {"type": "text", "plain_text": "inside the callout", "annotations": {}}]}},
            ],
        },
    },
    "child_database": {
        "provenance": "synthetic — inline database stand-in (rows live under _databases/)",
        "blocks": [
            {"id": "c1" + "0" * 30, "type": "child_database", "has_children": False,
             "child_database": {"title": "Tasks"}},
            {"id": "c2" + "0" * 30, "type": "child_page", "has_children": True,
             "child_page": {"title": "A sub-page"}},
        ],
        "children": {
            # never walked: child_page returns before the has_children tail
            "c2" + "0" * 30: [
                {"id": "c3" + "0" * 30, "type": "paragraph", "has_children": False, "paragraph": {
                    "rich_text": [{"type": "text", "plain_text": "own file, not inlined",
                                   "annotations": {}}]}},
            ],
        },
    },
    "equation": {
        "provenance": "synthetic — block equation, single- and multi-line expressions",
        "blocks": [
            {"id": "4a" + "0" * 30, "type": "equation", "has_children": False,
             "equation": {"expression": "a^2 + b^2 = c^2"}},
            {"id": "4b" + "0" * 30, "type": "equation", "has_children": False,
             "equation": {"expression": "\\begin{aligned}\nx &= 1\\\\\ny &= 2\n\\end{aligned}"}},
        ],
    },
    "link_to_page": {
        "provenance": "synthetic — all three link_to_page shapes",
        "blocks": [
            {"id": "5a" + "0" * 30, "type": "link_to_page", "has_children": False,
             "link_to_page": {"type": "page_id", "page_id": "5d000000-0000-0000-0000-000000000000"}},
            {"id": "5b" + "0" * 30, "type": "link_to_page", "has_children": False,
             "link_to_page": {"type": "database_id",
                              "database_id": "5e000000-0000-0000-0000-000000000000"}},
            {"id": "5c" + "0" * 30, "type": "link_to_page", "has_children": False,
             "link_to_page": {"type": "comment_id",
                              "comment_id": "5f000000-0000-0000-0000-000000000000"}},
        ],
    },
    # The eleven everyday types below used to be live captures. Each keeps the shape
    # its capture had — the same nesting, annotations and children — with invented
    # prose and placeholder ids, so the renderer paths they exercised stay exercised.
    "paragraph": {
        "provenance": "synthetic — an empty paragraph (a blank line) and a plain one",
        "blocks": [
            {"id": "d1" + "0" * 30, "type": "paragraph", "has_children": False,
             "paragraph": {"rich_text": [], "color": "default"}},
            {"id": "d2" + "0" * 30, "type": "paragraph", "has_children": False, "paragraph": {
                "rich_text": [{"type": "text", "plain_text": "One plain sentence.",
                               "annotations": {}}]}},
        ],
    },
    "heading_1": {
        "provenance": "synthetic — a toggleable heading whose children sit one level deeper",
        "blocks": [
            {"id": "d3" + "0" * 30, "type": "heading_1", "has_children": True, "heading_1": {
                "rich_text": [{"type": "text", "plain_text": "Update: 2024-10-23",
                               "annotations": {}}],
                "is_toggleable": True}},
        ],
        "children": {
            "d3" + "0" * 30: [
                {"id": "d4" + "0" * 30, "type": "heading_2", "has_children": False, "heading_2": {
                    "rich_text": [{"type": "text", "plain_text": "Changes:", "annotations": {}}],
                    "is_toggleable": False}},
                {"id": "d5" + "0" * 30, "type": "code", "has_children": False, "code": {
                    "caption": [], "language": "markdown",
                    "rich_text": [{"type": "text", "plain_text": "No changes", "annotations": {}}]}},
                {"id": "d6" + "0" * 30, "type": "paragraph", "has_children": False, "paragraph": {
                    "rich_text": [{"type": "text", "plain_text": "No changes detected",
                                   "annotations": {}}]}},
            ],
        },
    },
    "heading_2": {
        "provenance": "synthetic — a plain second-level heading",
        "blocks": [
            {"id": "d7" + "0" * 30, "type": "heading_2", "has_children": False, "heading_2": {
                "rich_text": [{"type": "text", "plain_text": "Changes (Part 1):",
                               "annotations": {}}],
                "is_toggleable": False}},
        ],
    },
    "heading_3": {
        "provenance": "synthetic — a third-level heading opening with a code span",
        "blocks": [
            {"id": "d8" + "0" * 30, "type": "heading_3", "has_children": False, "heading_3": {
                "rich_text": [
                    {"type": "text", "plain_text": "has_errors: true", "annotations": {"code": True}},
                    {"type": "text", "plain_text": " with nothing attributable", "annotations": {}},
                ],
                "is_toggleable": False}},
        ],
    },
    "bulleted_list_item": {
        "provenance": "synthetic — one bullet, no children",
        "blocks": [
            {"id": "d9" + "0" * 30, "type": "bulleted_list_item", "has_children": False,
             "bulleted_list_item": {"rich_text": [
                 {"type": "text", "plain_text": "the output file could not be written (I/O issue)",
                  "annotations": {}}]}},
        ],
    },
    "numbered_list_item": {
        "provenance": "synthetic — a numbered item with two numbered children",
        "blocks": [
            {"id": "e1" + "0" * 30, "type": "numbered_list_item", "has_children": True,
             "numbered_list_item": {"rich_text": [
                 {"type": "text", "plain_text": "First, translate the source to an intermediate form",
                  "annotations": {}}]}},
        ],
        "children": {
            "e1" + "0" * 30: [
                {"id": "e2" + "0" * 30, "type": "numbered_list_item", "has_children": False,
                 "numbered_list_item": {"rich_text": [
                     {"type": "text", "plain_text": "parse and compile it with the toolchain",
                      "annotations": {}}]}},
                {"id": "e3" + "0" * 30, "type": "numbered_list_item", "has_children": False,
                 "numbered_list_item": {"rich_text": [
                     {"type": "text", "plain_text": "then lower it to the target language",
                      "annotations": {}}]}},
            ],
        },
    },
    "to_do": {
        "provenance": "synthetic — an unchecked item with a bold run, and a checked one",
        "blocks": [
            {"id": "e4" + "0" * 30, "type": "to_do", "has_children": False, "to_do": {
                "checked": False,
                "rich_text": [
                    {"type": "text", "plain_text": "Publish the draft page to ", "annotations": {}},
                    {"type": "text", "plain_text": "staging ", "annotations": {"bold": True}},
                    {"type": "text", "plain_text": "(NOT PRODUCTION)", "annotations": {}},
                ]}},
            {"id": "e5" + "0" * 30, "type": "to_do", "has_children": False, "to_do": {
                "checked": True,
                "rich_text": [{"type": "text", "plain_text": "a checked one", "annotations": {}}]}},
        ],
    },
    "divider": {
        "provenance": "synthetic — a divider",
        "blocks": [
            {"id": "e6" + "0" * 30, "type": "divider", "has_children": False, "divider": {}},
        ],
    },
    "code": {
        "provenance": "synthetic — a fenced block with a language and multi-line content",
        "blocks": [
            {"id": "e7" + "0" * 30, "type": "code", "has_children": False, "code": {
                "caption": [], "language": "diff",
                "rich_text": [{"type": "text",
                               "plain_text": "--- Old\n+++ New\n@@ -316 +316 @@\n-[2024-06-07 09:26]",
                               "annotations": {}}]}},
        ],
    },
    "table": {
        "provenance": "synthetic — a two-column table with a header row and annotated cells",
        "blocks": [
            {"id": "e8" + "0" * 30, "type": "table", "has_children": True, "table": {
                "table_width": 2, "has_column_header": True, "has_row_header": False}},
        ],
        "children": {
            "e8" + "0" * 30: [
                {"id": "e9" + "0" * 30, "type": "table_row", "has_children": False, "table_row": {
                    "cells": [[{"type": "text", "plain_text": "Verdict", "annotations": {}}],
                              [{"type": "text", "plain_text": "Meaning", "annotations": {}}]]}},
                {"id": "f1" + "0" * 30, "type": "table_row", "has_children": False, "table_row": {
                    "cells": [[{"type": "text", "plain_text": "ADMIT", "annotations": {"bold": True}}],
                              [{"type": "text", "plain_text": "Usable. Goes into the dataset.",
                                "annotations": {}}]]}},
                {"id": "f2" + "0" * 30, "type": "table_row", "has_children": False, "table_row": {
                    "cells": [[{"type": "text", "plain_text": "FLAG", "annotations": {"bold": True}}],
                              [{"type": "text", "plain_text": "Usable, but the loss plausibly invalidates the row's ",
                                "annotations": {}},
                               {"type": "text", "plain_text": "purpose", "annotations": {"italic": True}},
                               {"type": "text", "plain_text": ". Review before use.", "annotations": {}}]]}},
                {"id": "f3" + "0" * 30, "type": "table_row", "has_children": False, "table_row": {
                    "cells": [[{"type": "text", "plain_text": "DISCARD", "annotations": {"bold": True}}],
                              [{"type": "text", "plain_text": "Dropped, ", "annotations": {}},
                               {"type": "text", "plain_text": "but an artifact exists",
                                "annotations": {"bold": True}},
                               {"type": "text", "plain_text": " and could be salvaged later.",
                                "annotations": {}}]]}},
            ],
        },
    },
    "child_page": {
        "provenance": "synthetic — a sub-page stand-in; its children are never walked",
        "blocks": [
            {"id": "f4" + "0" * 30, "type": "child_page", "has_children": True,
             "child_page": {"title": "Diagnosis Flow"}},
        ],
        "children": {
            "f4" + "0" * 30: [
                {"id": "f5" + "0" * 30, "type": "heading_1", "has_children": False, "heading_1": {
                    "rich_text": [{"type": "text", "plain_text": "Diagnosing a row",
                                   "annotations": {}}],
                    "is_toggleable": False}},
            ],
        },
    },
    "template": {
        "provenance": "synthetic — template blocks are legacy and rare; per the block-object docs",
        "blocks": [
            {"id": "6a" + "0" * 30, "type": "template", "has_children": True, "template": {
                "rich_text": [{"type": "text", "plain_text": "Add a meeting note",
                               "annotations": {}}]}},
        ],
        "children": {
            "6a" + "0" * 30: [
                {"id": "6b" + "0" * 30, "type": "paragraph", "has_children": False, "paragraph": {
                    "rich_text": [{"type": "text", "plain_text": "templated body",
                                   "annotations": {}}]}},
            ],
        },
    },
    "breadcrumb": {
        "provenance": "synthetic — breadcrumb renders nothing at all",
        "blocks": [
            {"id": "7a" + "0" * 30, "type": "breadcrumb", "has_children": False, "breadcrumb": {}},
            {"id": "7b" + "0" * 30, "type": "table_of_contents", "has_children": False,
             "table_of_contents": {"color": "default"}},
        ],
        "covers": ["table_of_contents"],
    },
    "media": {
        "provenance": "synthetic — Notion-hosted files become ATTACH: placeholders, external keep their URL",
        "blocks": [
            {"id": "8a" + "0" * 30, "type": "image", "has_children": False, "image": {
                "type": "file", "file": {"url": "https://prod-files-secure.s3.us-west-2.amazonaws.com/x/y/diagram.png"},
                "caption": [{"type": "text", "plain_text": "architecture", "annotations": {}}]}},
            {"id": "8b" + "0" * 30, "type": "image", "has_children": False, "image": {
                "type": "external", "external": {"url": "https://example.org/logo.png"},
                "caption": []}},
            {"id": "8c" + "0" * 30, "type": "pdf", "has_children": False, "pdf": {
                "type": "file", "file": {"url": "https://prod-files-secure.s3.us-west-2.amazonaws.com/x/y/paper.pdf"},
                "caption": []}},
            {"id": "8d" + "0" * 30, "type": "video", "has_children": False, "video": {
                "type": "external", "external": {"url": "https://youtu.be/abc"}, "caption": []}},
            {"id": "8e" + "0" * 30, "type": "audio", "has_children": False, "audio": {
                "type": "external", "external": {"url": "https://example.org/a.mp3"}, "caption": []}},
            {"id": "8f" + "0" * 30, "type": "file", "has_children": False, "file": {
                "type": "external", "external": {"url": "https://example.org/sheet.csv"},
                "caption": [{"type": "text", "plain_text": "the export", "annotations": {}}]}},
            # captioned on purpose: the caption is content the renderer used to drop
            {"id": "9a" + "0" * 30, "type": "bookmark", "has_children": False,
             "bookmark": {"url": "https://example.org/post",
                          "caption": [{"type": "text", "plain_text": "the write-up",
                                       "annotations": {}}]}},
            {"id": "9b" + "0" * 30, "type": "embed", "has_children": False,
             "embed": {"url": "https://example.org/embed"}},
            {"id": "9c" + "0" * 30, "type": "link_preview", "has_children": False,
             "link_preview": {"url": "https://github.com/org/repo/pull/1"}},
        ],
        "covers": ["audio", "link_preview"],
    },
}

SIGNED = ("signature", "sig", "policy", "key-pair-id", "token", "file_token")


def strip_credentials(obj):
    """Signed S3 URLs carry short-lived credentials; keep the path, drop them."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "url" and isinstance(v, str) and v.startswith("http"):
                pu = urllib.parse.urlsplit(v)
                keep = [(a, b) for a, b in urllib.parse.parse_qsl(pu.query)
                        if not a.lower().startswith("x-amz") and a.lower() not in SIGNED]
                out[k] = urllib.parse.urlunsplit(
                    (pu.scheme, pu.netloc, pu.path, urllib.parse.urlencode(keep), ""))
            else:
                out[k] = strip_credentials(v)
        return out
    if isinstance(obj, list):
        return [strip_credentials(x) for x in obj]
    return obj


def write_fixture(name, fx):
    path = os.path.join(BLOCKS, name + ".json")
    fx = dict(fx, name=name)
    fx = strip_credentials(fx)
    with open(path, "w") as f:
        json.dump(fx, f, indent=1, ensure_ascii=False)
        f.write("\n")
    print(f"[fixture] {name}")


def cmd_synthetic(args):
    have = set(fixture_names())
    for name, fx in SYNTHETIC.items():
        if name in have and not args.force:
            print(f"[keep] {name} (exists)")
            continue
        write_fixture(name, fx)


def trim_payload(obj, text_cap, list_cap):
    """Shrink a captured payload to fixture size: cap every text run and every
    child list. A fixture proves the renderer's shape handling, so a 286 KB
    capture of somebody's whole sub-page is cost with no coverage — and the
    less real prose sits in git, the less there is to review for PII."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            # "content" is the raw twin of plain_text: unused by the renderer,
            # and the one the first trim left carrying real prose
            if k in ("plain_text", "expression", "content") and isinstance(v, str) and len(v) > text_cap:
                out[k] = v[:text_cap] + "…"
            elif k in ("rich_text", "caption", "results") and isinstance(v, list):
                out[k] = [trim_payload(x, text_cap, list_cap) for x in v[:list_cap]]
            elif k == "cells" and isinstance(v, list):
                out[k] = [[trim_payload(y, text_cap, list_cap) for y in c] for c in v[:list_cap]]
            else:
                out[k] = trim_payload(v, text_cap, list_cap)
        return out
    if isinstance(obj, list):
        return [trim_payload(x, text_cap, list_cap) for x in obj]
    return obj


def cmd_trim(args):
    for name in fixture_names():
        fx = load_fixture(name)
        if fx.get("provenance", "").startswith("synthetic"):
            continue
        blocks = trim_payload(fx["blocks"], args.text_cap, args.list_cap)
        children = {k: trim_payload(v[:args.list_cap], args.text_cap, args.list_cap)
                    for k, v in (fx.get("children") or {}).items()}
        # drop child entries nothing kept still points at
        keep, changed = set(), True
        ids = {refresh.undash(b["id"]) for b in blocks}
        while changed:
            changed = False
            for bid in list(ids - keep):
                keep.add(bid)
                changed = True
                for kid in children.get(bid, []):
                    ids.add(refresh.undash(kid["id"]))
        fx["blocks"], fx["children"] = blocks, {k: v for k, v in children.items() if k in keep}
        if "truncated" not in fx["provenance"]:
            fx["provenance"] += f" (text truncated to {args.text_cap} chars, lists to {args.list_cap})"
        write_fixture(name, fx)


def cmd_expected(args):
    os.makedirs(EXPECTED, exist_ok=True)
    for name in fixture_names():
        text, _w, _api = render_fixture(load_fixture(name))
        with open(os.path.join(EXPECTED, name + ".md"), "w") as f:
            f.write(text)
        print(f"[expected] {name} ({len(text.splitlines())} lines)")


def first_children(api, block_id, size=100):
    """One page of children, one request. The scan must never paginate: the most
    recently edited pages in this workspace are machine-generated transcripts of
    thousands of paragraph blocks, and a paginating scan spends its whole budget
    on ~three of them (measured: 505 requests, 0 new block types)."""
    d = api.get(f"/blocks/{refresh.undash(block_id)}/children", {"page_size": size})
    return d.get("results", [])


def children_of(api, block_id, depth, out):
    """Record the children of block_id (and, to `depth`, theirs) into out."""
    kids = first_children(api, block_id)
    out[refresh.undash(block_id)] = kids
    if depth <= 0:
        return
    for k in kids:
        if k.get("has_children"):
            children_of(api, k["id"], depth - 1, out)


def cmd_capture(args):
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        print("NOTION_TOKEN not set", file=sys.stderr)
        return 2
    api = refresh.Api(token, args.rps, args.budget)
    wanted = [t for t in WANTED if t not in
              {c for n in fixture_names() for c in _covers(n)}] if not args.force else list(WANTED)
    print(f"looking for: {', '.join(wanted) or '(nothing missing)'}")
    if not wanted:
        return 0

    # content pages first, DB rows after: the most recently edited pages are
    # feed rows (click events, signups) with empty bodies. Filtering them out at
    # search time is the expensive way round — it paginates through thousands of
    # rows to find 90 pages (measured: 195 requests) — so take whatever search
    # returns and just order it.
    hits = []
    try:
        for r in api.paginate("POST", "/search", body={
                "filter": {"value": "page", "property": "object"},
                "sort": {"direction": "descending", "timestamp": "last_edited_time"}}):
            hits.append(r)
            if len(hits) >= args.pages:
                break
    except refresh.Budget:
        pass
    hits.sort(key=lambda r: (r.get("parent") or {}).get("type") == "database_id")
    pages = [r["id"] for r in hits]
    print(f"scanning {len(pages)} pages ({api.n} requests so far)")

    found = {}
    queue = list(pages)
    seen = set()
    try:
        while queue and set(wanted) - set(found):
            bid = queue.pop(0)
            if bid in seen:
                continue
            seen.add(bid)
            try:
                kids = first_children(api, bid)
            except refresh.ApiError as e:
                print(f"  [skip] {bid[:8]}: {e.code}")
                continue
            for b in kids:
                t = b.get("type")
                if t in wanted and t not in found:
                    ch = {}
                    if b.get("has_children") or t == "table":
                        children_of(api, b["id"], 1, ch)
                    found[t] = {
                        "provenance": f"live: block {refresh.undash(b['id'])} "
                                      f"(page {refresh.undash(bid)}), captured "
                                      f"{refresh.now_iso()[:10]}",
                        "blocks": [b],
                        "children": ch,
                    }
                    print(f"  [found] {t} on {refresh.undash(bid)[:8]} (req={api.n})")
                if b.get("has_children") and len(seen) + len(queue) < args.max_blocks:
                    queue.append(b["id"])
    except refresh.Budget:
        print("request budget hit")
    except KeyboardInterrupt:
        print("interrupted")

    for t, fx in found.items():
        write_fixture(t, fx)
    missing = [t for t in wanted if t not in found]
    print(f"\ncaptured {len(found)}, requests={api.n}, 429s={api.r429}")
    if missing:
        print(f"NOT FOUND (hand-author these): {', '.join(missing)}")
    return 0


def _covers(name):
    from fixture_support import fixture_covers
    return fixture_covers(load_fixture(name))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture", help="record live payloads for missing types")
    c.add_argument("--rps", type=float, default=1.0)
    c.add_argument("--budget", type=int, default=400)
    c.add_argument("--pages", type=int, default=120, help="pages to seed the walk with")
    c.add_argument("--max-blocks", type=int, default=4000)
    c.add_argument("--force", action="store_true", help="re-capture types that already have fixtures")
    c.set_defaults(fn=cmd_capture)
    s = sub.add_parser("synthetic", help="write the hand-authored fixtures")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_synthetic)
    t = sub.add_parser("trim", help="shrink captured fixtures (text runs, child lists)")
    t.add_argument("--text-cap", type=int, default=90)
    t.add_argument("--list-cap", type=int, default=5)
    t.set_defaults(fn=cmd_trim)
    e = sub.add_parser("expected", help="re-render every fixture into expected/")
    e.set_defaults(fn=cmd_expected)
    args = ap.parse_args()
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
