"""The primitives the rest of `notion_core` shares: time, id spelling, the run log.

Small on purpose. These are here because `api`, `flatten`, `walker` and `runcfg` all
need one or two of them and none of them owns the others — not because a `util` module
is a good idea in general. `refresh.py` re-imports all four, so a mirror script still
reads them off `refresh.`.
"""
import datetime as dt
import sys


UTC = dt.timezone.utc


def undash(i):
    return (i or "").replace("-", "")


def dashed(i):
    i = undash(i)
    return f"{i[0:8]}-{i[8:12]}-{i[12:16]}-{i[16:20]}-{i[20:32]}" if len(i) == 32 else i


def log(msg):
    sys.stderr.write(f"[{dt.datetime.now(UTC).strftime('%H:%M:%S')}] {msg}\n")
    sys.stderr.flush()
