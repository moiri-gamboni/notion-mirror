"""How a mirror run is configured: the row-id parser, and a retired budget table.

`parse_row_ids` is read rather than copied by anything that drives a run from outside —
`tasks refresh-mirror` takes its id validation from here, so a caller cannot disagree
with the run it is about to start.
"""
import re

from .util import undash


# The engine enforces no request budget any more; nothing in this repository reads
# this. It stays because tasksync's `refresh-mirror` still reads MODE_BUDGETS["rows"]
# to pass as NOTION_REFRESH_BUDGET, which refresh.sh now ignores — remove it together
# with that read.
MODE_BUDGETS = {"daily": 15000, "full-comments": 90000, "place": 2000,
                "validate": 3000, "rows": 300}


def parse_row_ids(s):
    """Comma- or space-separated page ids -> undashed ids. Raises on anything else.

    A typo'd id must stop the run rather than quietly refresh a shorter list:
    the caller (the hourly job, or a person) believes those rows are now fresh."""
    given = [x for x in re.split(r"[,\s]+", (s or "").strip()) if x]
    bad = [x for x in given if not re.fullmatch(r"[0-9a-f]{32}", undash(x))]
    if bad:
        raise ValueError(f"not page ids: {', '.join(bad[:5])}")
    if not given:
        raise ValueError("no row ids given")
    return [undash(x) for x in given]
