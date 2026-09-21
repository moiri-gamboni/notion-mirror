#!/usr/bin/env python3
"""The hourly rows job's status marker — `_meta/state/rows-refresh-status.json`.

The hourly job never ntfys. It cannot: losing the race with the nightly is its
designed outcome and happens 2-4 times every night by construction, so a job
that alerted on contention would send up to five high-priority notifications a
night for working correctly. Everything it could say through a notification it
writes here instead, and a **different cron** (a health check, which has to
survive this whole pipeline being broken) ages the last success and alerts.

That split is the point, and it is also the trap: "write a marker and exit
non-zero" is indistinguishable from a clean run unless something is genuinely
reading the marker. The dead-man is not a nice-to-have here, it is the only
listener — see `README.md` § "Rows mode and its status marker" for the check itself.

**Every path here is resolved on use, not at import** — the one module in the engine that
is, and for the reason the engine asserts at import everywhere else. A mirror on a
`nofail` bind mount is, unmounted, an empty directory. The other engine modules must
refuse that outright (they write, and an empty root is materialised rather than noticed),
but this one is what a health check runs *because* something is broken. So the mirror
root is resolved inside `check()` and its failure becomes one alarm line, which a caller
composing further alarms on top of this one can carry along.

Three rules live here rather than in the shell caller, so no caller can forget
one:

  * **An all-refused run is a failure**, not a success with zero rows. A run
    that names 44 rows and refreshes none exits 0 from `refresh.py` and leaves a
    clean tree, which reads exactly like a night with no edits.
  * **A budget-exhausted run is a failure.** It refreshed a prefix of the scope
    and left the tail stale; if that recurs the tail is stale forever and the
    success clock is the only thing that would ever say so. A one-off costs
    nothing (the clock is an hour old, the threshold is six).
  * **Only an `ok` advances the success clock.** Skips and failures are recorded
    and counted, but the clock the dead-man reads moves only when rows were
    actually photographed.

The writer never guesses where the marker lives: `record()` takes its state directory
from the caller, which has already resolved the mirror. Only the reader resolves, and
only on use.
"""
import argparse
import datetime
import json
import os
import sys

import mirror_root

# The dead-man's threshold. Six hours clears the measured nightly blackout: the
# nightly holds the mirror lock ~2-2.5h typically and up to ~4.5h including the
# in-lock changelog call, so `flock -n` legitimately refuses 2-4 consecutive
# ticks every night and up to 5 on a bad one. Anything tighter alerts on the
# system working as designed, which is how a dead-man gets muted.
MAX_AGE_H = 6

# Outcomes. `skipped` is a designed refusal (someone else holds the lock, the
# tree is mid-edit); `failed` is something wrong. Both exit non-zero and both
# leave the success clock alone — the distinction is for whoever reads the file.
OK, SKIPPED, FAILED = "ok", "skipped", "failed"


def _state_path(name):
    """`<mirror>/_meta/state/<name>`, resolved on use; raises `MirrorError`."""
    return os.path.join(mirror_root.state_dir(), name)


def path(state_dir=None):
    return os.path.join(state_dir, mirror_root.ROWS_STATUS) if state_dir \
        else _state_path(mirror_root.ROWS_STATUS)


def _now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def _read_json(p):
    """A dict from `p`, or `{}`. Unreadable state must never stop the caller:
    for the writer that means the next tick still records, and for the dead-man
    a corrupt marker reads as "no success on record", which alarms."""
    try:
        with open(p) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load(state_dir=None):
    return _read_json(path(state_dir))


def _save(data, state_dir):
    p = path(state_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, p)


def verdict_from_report(report):
    """`(outcome, reason, detail, stats)` for a finished `--mode rows` run.

    The two downgrades are here so the shell caller cannot ship a run that
    looks clean because nothing happened. Everything else about the run is
    carried through as counts for the operator.
    """
    rows = report.get("rows") or {}
    props = report.get("props_probe") or {}
    stats = {
        "requested": rows.get("requested", 0),
        "refreshed": len(rows.get("refreshed") or []),
        "skipped": len(rows.get("skipped") or []),
        "errors": len(rows.get("errors") or []) + len(props.get("errors") or []),
        "props_drained": props.get("drained", 0),
        "props_deferred": props.get("deferred", 0),
        "requests": report.get("requests", 0),
        "budget_exhausted": bool(report.get("budget_exhausted")),
    }
    detail = (f"{stats['refreshed']}/{stats['requested']} rows, "
              f"{stats['props_drained']} props probes, "
              f"{stats['errors']} errors, {stats['requests']} req")
    if stats["budget_exhausted"]:
        return FAILED, "budget_exhausted", detail, stats
    if stats["requested"] and not stats["refreshed"]:
        return FAILED, "all_rows_refused", detail, stats
    return OK, "", detail, stats


def record(outcome, reason="", detail="", *, state_dir):
    """Write one attempt into the marker under `state_dir`. Returns the new marker dict."""
    data = load(state_dir)
    attempt = {"ts": _now(), "outcome": outcome}
    if reason:
        attempt["reason"] = reason
    if detail:
        attempt["detail"] = detail

    data["last_attempt"] = attempt
    if outcome == OK:
        data["last_success_ts"] = attempt["ts"]
        data["consecutive_not_ok"] = 0
    else:
        data["consecutive_not_ok"] = int(data.get("consecutive_not_ok") or 0) + 1
    _save(data, state_dir)
    return data


def _age_hours(ts):
    """Hours since an ISO timestamp, or `None` if it cannot be read at all."""
    try:
        then = datetime.datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:  # tolerate a hand-edited marker
        then = then.replace(tzinfo=datetime.timezone.utc)
    return (datetime.datetime.now(datetime.timezone.utc) - then).total_seconds() / 3600.0


def check(max_age_h=MAX_AGE_H, state_dir=None):
    """The dead-man's read side: one line per alarm, empty when healthy.

    Called from a health check in a **different cron**, so it still runs when this
    pipeline is wedged. It keys on the age of the last *success* and never on the last
    exit status: there is no wall-clock bound on `refresh.sh`, so a hung nightly holds
    the mirror lock indefinitely and every hourly tick behind it exits non-zero from
    `flock -n`. That is not the job failing, so an alert-on-failure would stay quiet for
    exactly as long as the outage lasted.
    """
    try:
        data = load(state_dir)
    except mirror_root.MirrorError as e:
        # An unmounted mirror volume, or nothing configured. One alarm line rather than a
        # traceback: a caller composing this with other checks keeps its own answers.
        return ["mirror alarms unavailable: " + str(e).split("\n")[0]]
    age = _age_hours(data.get("last_success_ts"))
    if age is None:
        n = data.get("consecutive_not_ok") or 0
        return ["notion mirror: no successful hourly rows refresh on record"
                + (f", {n} failed or skipped attempt(s) since" if n
                   else " — the job has never run")]
    if age >= max_age_h:
        last = data.get("last_attempt") or {}
        why = last.get("reason") or last.get("outcome") or "unknown"
        return [f"notion mirror: no successful hourly rows refresh in {age:.0f}h "
                f"(threshold {max_age_h}h, last attempt: {why})"]
    return []


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--outcome", choices=[OK, SKIPPED, FAILED],
                    help="record this outcome directly (pre-flight refusals)")
    ap.add_argument("--reason", default="", help="machine-readable reason tag")
    ap.add_argument("--detail", default="", help="one line for a human")
    ap.add_argument("--from-report", metavar="PATH",
                    help="derive the outcome from a finished rows run's JSON report")
    ap.add_argument("--check", action="store_true",
                    help="the dead-man: print every alarm and exit 1 if there are any. "
                         "Run from a health check in a different cron.")
    ap.add_argument("--max-age-hours", type=float, default=MAX_AGE_H)
    ap.add_argument("--state-dir", default=None,
                    help="the marker's directory (default: the configured mirror's "
                         "_meta/state; a writer must pass it)")
    args = ap.parse_args(argv)

    if sum(map(bool, (args.outcome, args.from_report, args.check))) != 1:
        ap.error("exactly one of --outcome / --from-report / --check")

    if args.check:
        alarms = check(max_age_h=args.max_age_hours, state_dir=args.state_dir)
        for line in alarms:
            print(line)
        return 1 if alarms else 0

    try:
        state_dir = args.state_dir or mirror_root.state_dir()
    except mirror_root.MirrorError as e:
        # A hand-run writer with no mirror configured: the refusal, not a traceback.
        print(e, file=sys.stderr)
        return 2
    if args.from_report:
        try:
            with open(args.from_report) as f:
                report = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            # The run finished but left no readable report: that is a failure of
            # the run, not of this script, and it must not read as a success.
            record(FAILED, "no_report", f"{type(e).__name__}: {e}"[:160],
                   state_dir=state_dir)
            print(f"rows-status: unreadable report {args.from_report}", file=sys.stderr)
            return 1
        outcome, reason, detail, _stats = verdict_from_report(report)
    else:
        outcome, reason, detail = args.outcome, args.reason, args.detail
    record(outcome, reason, detail, state_dir=state_dir)

    print(f"rows-status: {outcome}" + (f" ({reason})" if reason else "") +
          (f" — {detail}" if detail else ""))
    return 0 if outcome == OK else 1


if __name__ == "__main__":
    sys.exit(main())
