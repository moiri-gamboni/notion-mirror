"""The mirror engine's data roots — one derivation, imported everywhere it was repeated.

Every module here used to climb from its own `__file__` to the mirror, which was correct
while the engine lived beside the data and is wrong now that it lives in its own clone.
Those climbs are replaced by an import of this module; `TOOLS` stays a dirname climb in
the modules that use it, because there it means *code* and a file does know where its own
siblings are.

Import order is the contract: importing this module (and so any engine module) resolves
and asserts the mirror, at import time, and raises `mirror_root.MirrorError` if the
configured root does not carry the mirror fingerprint. Nothing here is lazy, on purpose —
a module that resolved on first use would let a wrong-root process get as far as writing.
The rule this places on tests: a test that sandboxes the mirror must set `$NOTION_MIRROR`
*before* importing the module under test (a `setUpModule` fixture, not `setUp`).

The filename constants are bare names, not paths, and they are defined beside the resolver
rather than here. The dead-man reader joins them onto a root it resolves lazily, so that an
unmounted mirror volume degrades to "mirror alarms unavailable" instead of a traceback —
and it cannot import this module to get them, because importing this module is what
asserts the mirror. Re-exported below so that every other consumer reads them from the one
module it already imports for the roots.
"""
import os

import mirror_root
from mirror_root import CAPTURE, ROWS_STATUS, USERS, WEBHOOK_SECRET  # noqa: F401 — re-exported

#: The nightly's own run marker under `_meta/state`; nothing outside the engine reads it.
LAST_RUN = "last-run.json"

NOTION = mirror_root.mirror_dir()
WS = os.path.join(NOTION, "workspace")
DBS = os.path.join(WS, "_databases")
META = os.path.join(NOTION, "_meta")
STATE = os.path.join(META, "state")
