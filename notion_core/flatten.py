"""Property values -> the flat cell strings the mirror's CSVs and row tables carry.

The conventions here replicate the Jul-08..10 build exactly (raw `str()` on formula and
rollup values, `' | '` between rollup array items, and so on). They are load-bearing for
diff size rather than for correctness: a changed convention rewrites every CSV in the
mirror on the next run.

`expand_truncated_props` belongs beside them because the 25-item cap is what makes a
cell wrong in the first place — a `relation` rendered from an unexpanded payload is a
quietly shortened list, not an error.
"""
import json

from .api import ApiError, Budget
from .richtext import plain
from .util import undash


def fmt_num(v):
    # replicate the build's str() semantics exactly (5.0 -> '5.0', 3 -> '3')
    return "" if v is None else str(v)


def fmt_date(v):
    if not v:
        return ""
    s = v.get("start") or ""
    if v.get("end"):
        s = f"{s} → {v['end']}"
    return s


def cell(p, users, in_rollup=False):
    """Render one property value to the mirror's cell string. Defensive against
    legacy/malformed payloads (e.g. an old hand-built page's off-schema properties)."""
    if p is None:
        return ""
    try:
        return _cell(p, users, in_rollup)
    except (AttributeError, TypeError, KeyError):
        v = p.get(p.get("type"))
        return "" if v is None else (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))


def _cell(p, users, in_rollup):
    t = p.get("type")
    v = p.get(t)
    if t in ("title", "rich_text"):
        return plain(v) if isinstance(v, list) else ""
    if t == "number":
        return fmt_num(v)
    if t in ("select", "status"):
        return v.get("name", "") if isinstance(v, dict) else ""
    if t == "multi_select":
        if not isinstance(v, list):
            return ""
        return ", ".join(o.get("name", "") if isinstance(o, dict) else str(o) for o in v)
    if t == "date":
        return fmt_date(v) if v is None or isinstance(v, dict) else str(v)
    if t == "people":
        if not isinstance(v, list):
            return ""
        # build convention: the item's own embedded name, else its raw uuid —
        # cells never resolve users via the API (users.name is for metadata/comments)
        return ", ".join((x.get("name") or x.get("id", "")) if isinstance(x, dict) else str(x)
                         for x in v)
    if t == "files":
        if not isinstance(v, list):
            return ""
        return ", ".join((f.get("name") or (f.get("external") or f.get("file") or {}).get("url", ""))
                         if isinstance(f, dict) else str(f) for f in v)
    if t == "checkbox":
        return "true" if v else "false"
    if t in ("url", "email", "phone_number"):
        return v or ""
    if t == "relation":
        if not isinstance(v, list):
            return ""
        return ", ".join(x.get("id", "") if isinstance(x, dict) else str(x) for x in v)
    if t == "formula":
        # build used raw str() on the parsed value: null -> 'None', floats keep
        # full repr, date objects keep their python dict repr
        ft = (v or {}).get("type")
        fv = (v or {}).get(ft) if ft else None
        return str(fv)
    if t == "rollup":
        rt = (v or {}).get("type")
        if rt == "number":
            return str(v.get("number"))
        if rt == "date":
            return str(v.get("date"))  # build: raw dict repr (incl. None fields)
        if rt == "array":
            arr = v.get("array")
            if not isinstance(arr, list):
                return ""
            # build convention: rollup arrays join with ' | '
            return " | ".join(x for x in (cell(i, users, in_rollup=True) for i in arr) if x)
        return ""
    if t in ("created_time", "last_edited_time"):
        return v or ""
    if t in ("created_by", "last_edited_by"):
        if not isinstance(v, dict):
            return "" if v is None else str(v)
        return v.get("name") or v.get("id", "")
    if t == "unique_id":
        if not v or v.get("number") is None:
            return ""
        pre = v.get("prefix")
        return f"{pre}-{v['number']}" if pre else str(v["number"])
    if t == "verification":
        return (v or {}).get("state", "") if v else ""
    if t == "button":
        return json.dumps(v) if v is not None else ""
    if v is None:
        return ""
    if isinstance(v, (str, int, float)):
        return str(v)
    return json.dumps(v, ensure_ascii=False)


def md_cell(s):
    """Table-cell form of a rendered value: newlines -> single space, pipes escaped."""
    return s.replace("\r", "").replace("\n", " ").replace("|", "\\|")


# Property types whose values query/page payloads paginate (i.e. truncate at 25
# items). Rollup arrays are also capped but not expanded here: their elements
# come back from the property endpoint in a different shape than the page
# payload, and the underlying rows live in the target DB's own CSV anyway.
PAGINATED_PROP_TYPES = ("title", "rich_text", "relation", "people")


def expand_truncated_props(api, page, db_title, report):
    """Query/page payloads cap list-valued properties at 25 items (relation sets
    has_more; title/rich_text/people just truncate silently, hence the len==25
    probe — an exactly-25 value costs one confirming request). Re-fetch capped
    values in full via the per-property endpoint, mutating the page payload in
    place so every downstream render (CSV cell, row md) sees the complete list.
    On API error the truncated value is kept and the error reported."""
    for p in (page.get("properties") or {}).values():
        t = p.get("type")
        if t not in PAGINATED_PROP_TYPES:
            continue
        v = p.get(t)
        if not (p.get("has_more") or (isinstance(v, list) and len(v) == 25)):
            continue
        try:
            # property ids arrive already percent-encoded ("%3FTiN"), so pass them
            # through verbatim. Re-quoting to "%253FTiN" is tolerated by the API for
            # every id tested here except one rollup, which 404s — and rollups are
            # excluded above, so this is hardening, not a live-bug fix. Verbatim is
            # the form that worked in 45/45 probes; keep it that way if rollup
            # expansion is ever added.
            items = [r.get(r.get("type")) for r in api.paginate(
                "GET", f"/pages/{page['id']}/properties/{p['id']}")]
        except Budget:
            raise
        except ApiError as e:
            report["dbs"]["errors"].append({"db": db_title, "op": f"expand {undash(page['id'])[:8]}",
                                            "error": str(e)[:160]})
            continue
        p[t] = [x for x in items if x is not None]
        p.pop("has_more", None)
