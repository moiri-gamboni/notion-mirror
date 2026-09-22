"""The Notion HTTP client: paced, budgeted, and Retry-After-honouring.

One client for every caller — the mirror engine, the one-shot scripts and tasksync —
so the ~3 req/s shared integration cap is respected by construction rather than by each
caller remembering to sleep. `Budget` and `ApiError` are part of the contract: every
caller of a paginating helper has to handle both, and a helper that swallowed either
would turn a truncated run into a silently short one.
"""
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .util import log


API = "https://api.notion.com/v1"
VER = "2022-06-28"
VER_DS = "2025-09-03"


class Budget(Exception):
    pass


class ApiError(Exception):
    def __init__(self, code, body=""):
        self.code, self.body = code, body
        super().__init__(f"HTTP {code}: {body[:200]}")


# A single data-source/database query returns at most this many results, however
# far pagination is pushed (developers.notion.com/guides/data-apis/query-large-data-sources).
QUERY_CAP = 10_000


class Truncated(ApiError):
    """A row sweep could not produce the database's whole row set: a query hit
    Notion's per-query result cap and created_time windowing could not recover
    the remainder, or (in `refresh.query_db_rows`) a multi-source database
    listed no data source to query. Raised instead of returning a silently short
    set because callers diff missing rows as deletions. Subclasses ApiError so
    every existing report-and-skip handler covers it."""

    def __init__(self, msg):
        super().__init__(0, msg)
        self.msg = msg

    def __str__(self):
        return self.msg


class Api:
    def __init__(self, token, rps, budget):
        self.token = token
        self.interval = 1.0 / rps
        self.budget = budget
        self.n = 0
        self.r429 = 0
        self._next_slot = 0.0  # earliest monotonic time the next request may fire

    def check_budget(self):
        # With a message: callers print `{type}: {exc}` and a bare `Budget` reaches
        # a cron log as a type name and an empty string, which reads as a crash
        # rather than as the one condition an operator fixes by raising a number.
        if self.n >= self.budget:
            raise Budget(f"request budget of {self.budget:g} spent after {self.n} "
                         "request(s); the run stops here and the next one resumes")

    def _pace(self):
        """Rate LIMITER (not a fixed post-sleep): pace requests to <= 1/interval,
        sleeping only when we're actually ahead of schedule. When request latency
        already exceeds the interval (the common case: Notion round-trips ~0.4s
        vs a ~0.33s interval), this adds ZERO wait — the old fixed 0.4s post-sleep
        wasted ~0.4s/request, halving throughput below Notion's ~3 rps cap."""
        now = time.monotonic()
        if now < self._next_slot:
            time.sleep(self._next_slot - now)
            now = self._next_slot
        self._next_slot = now + self.interval

    def call(self, method, path, body=None, params=None, ver=VER):
        self.check_budget()
        url = API + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(9):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": ver,
                **({"Content-Type": "application/json"} if data else {}),
            })
            try:
                self._pace()
                with urllib.request.urlopen(req, timeout=90) as r:
                    out = json.loads(r.read().decode("utf-8"))
                self.n += 1
                if self.n % 200 == 0:
                    log(f"api requests: {self.n}")
                return out
            except urllib.error.HTTPError as e:
                eb = e.read().decode("utf-8", "replace")
                if e.code in (429, 529):
                    self.r429 += 1
                    ra = e.headers.get("Retry-After")
                    try:
                        wait = float(ra) if ra else min(2 ** attempt, 30)
                    except ValueError:  # RFC-7231 date or garbage: back off, don't crash
                        wait = min(2 ** attempt, 30)
                    log(f"rate {e.code}, waiting {wait}s ({path})")
                    time.sleep(wait + 0.5)
                    self._next_slot = time.monotonic() + self.interval  # ease back in
                    continue
                if 500 <= e.code < 600:
                    time.sleep(min(2 ** attempt, 30))
                    continue
                self.n += 1
                raise ApiError(e.code, eb)
            # A body that does not arrive whole belongs here and not with the
            # caller: a page cut mid-string surfaces as a JSON (or UTF-8) decode
            # error, or as IncompleteRead when the length was known. Letting one
            # escape killed the 2026-09-17 nightly after 8,042 requests and left
            # a half-written tree that wedged every run for three days. Retried
            # on the same terms as a dropped connection — which, as there, means
            # a write whose response was lost is sent a second time.
            except (urllib.error.URLError, TimeoutError, ConnectionError,
                    http.client.IncompleteRead, json.JSONDecodeError,
                    UnicodeDecodeError) as e:
                log(f"transport error {type(e).__name__}: {e} on {path}, retrying")
                time.sleep(min(2 ** attempt, 30))
        raise ApiError(0, "retries exhausted")

    def get(self, path, params=None, ver=VER):
        return self.call("GET", path, params=params, ver=ver)

    def post(self, path, body=None, ver=VER):
        return self.call("POST", path, body=body or {}, ver=ver)

    def query_rows(self, path, body=None, ver=VER):
        """Every row of a data-source/database query, complete past QUERY_CAP.

        Notion caps a single query at QUERY_CAP results: pagination just stops
        with has_more=false, and only a request_status marker (absent from the
        2022-06-28 endpoint's responses) says the result is incomplete. Sorting
        by created_time ascending makes the capped prefix a stable window, so
        the sweep continues from `on_or_after` the last created_time seen and
        de-duplicates the boundary tie by row id (the pattern from Notion's
        "query large data sources" guide). Any caller-supplied sort is replaced
        by that created_time sort; callers here order rows themselves.

        A query that ends exactly at the cap *without* the marker (the 2022
        endpoint) is treated as suspect and windowed too — on a database holding
        exactly QUERY_CAP rows that costs one boundary re-query and still
        terminates. A window that caps without yielding a single new row means
        more than a cap's worth of rows share one created_time, which windowing
        cannot split: Truncated.
        """
        base = dict(body or {})
        base_filter = base.get("filter")
        rows, seen = [], set()
        floor = None
        windows = 0
        while True:
            windows += 1
            b = dict(base)
            b["sorts"] = [{"timestamp": "created_time", "direction": "ascending"}]
            if floor is not None:
                w = {"timestamp": "created_time", "created_time": {"on_or_after": floor}}
                b["filter"] = {"and": [base_filter, w]} if base_filter else w
            b["page_size"] = b.get("page_size", 100)
            got_new = capped = marker_seen = False
            count = 0
            cursor = None
            last_ts = None
            while True:
                if cursor:
                    b["start_cursor"] = cursor
                else:
                    b.pop("start_cursor", None)
                d = self.post(path, b, ver=ver)
                got = d.get("results", [])
                count += len(got)
                for r in got:
                    if r.get("created_time"):
                        last_ts = r["created_time"]
                    rid = r.get("id")
                    if rid in seen:
                        continue
                    seen.add(rid)
                    rows.append(r)
                    got_new = True
                rs = d.get("request_status")
                if rs is not None:
                    marker_seen = True
                    if rs.get("type") == "incomplete":
                        capped = True
                if not d.get("has_more"):
                    break
                cursor = d.get("next_cursor")
            if not capped and (marker_seen or count < QUERY_CAP):
                break
            if not got_new:
                raise Truncated(
                    f"{path}: query capped at {QUERY_CAP} results and the "
                    f"created_time window at {floor!r} yielded nothing new — "
                    "more rows than the cap share one created_time")
            if not last_ts:
                raise Truncated(
                    f"{path}: query capped at {QUERY_CAP} results and rows "
                    "carry no created_time to window on")
            floor = last_ts
        if windows > 1:
            log(f"windowed row query: {path} -> {len(rows)} rows in {windows} windows")
        return rows

    def paginate(self, method, path, body=None, params=None, ver=VER):
        cursor = None
        while True:
            if method == "POST":
                b = dict(body or {})
                b["page_size"] = b.get("page_size", 100)
                if cursor:
                    b["start_cursor"] = cursor
                d = self.post(path, b, ver=ver)
            else:
                p = dict(params or {})
                p["page_size"] = p.get("page_size", 100)
                if cursor:
                    p["start_cursor"] = cursor
                d = self.get(path, p, ver=ver)
            yield from d.get("results", [])
            if not d.get("has_more"):
                return
            cursor = d.get("next_cursor")
