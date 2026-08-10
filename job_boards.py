#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Pull public job postings from every Ashby, Greenhouse and Lever job board.

No API key. Each ATS publishes an unauthenticated per-company posting API with no
global search, so this runs in two phases: discover board slugs from the Internet
Archive, then fetch and filter every board.

    uv run job_boards.py --all                              # every job, all platforms
    uv run job_boards.py --title "software engineer"
    uv run job_boards.py --title "software engineer" --match exact
    uv run job_boards.py --ats greenhouse --title "swe"     # one platform
    uv run job_boards.py --ats ashby,lever --all            # a subset
    uv run job_boards.py --grep '\\brust\\b|\\bgolang\\b'     # search descriptions
    uv run job_boards.py --refresh-boards

Results go to CSV and JSON, and accumulate into a SQLite database keyed on
(ats, posting id) so first_seen/last_seen/closed_at survive across scrapes.

boards.seed.json ships with the repo, so --refresh-boards is optional. See the README.
"""

# Keeps `X | None` annotations from being evaluated at import, so the file also
# imports under Python 3.9 — which is what a bare `python3` is on macOS, and what
# tooling gets when uv is not on its PATH. uv still provisions 3.11 per the
# metadata above; this only makes the no-uv fallback in AGENTS.md work.
from __future__ import annotations

import argparse
import csv
import gzip
import http.client
import json
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from pathlib import Path

HERE = Path(__file__).parent
# Two files on purpose. The seed is small, curated and committed, so a fresh clone
# works without touching any archive. The cache is whatever the last crawl produced
# — potentially three vendors' entire customer lists — and is gitignored, so a full
# crawl never turns this repo into published competitive intelligence.
BOARDS_SEED = HERE / "boards.seed.json"
BOARDS_CACHE = HERE / "boards.json"
COLLINFO = "https://index.commoncrawl.org/collinfo.json"
WAYBACK_CDX = (
    "http://web.archive.org/cdx/search/cdx?url={domain}"
    "&matchType=domain&fl=original&collapse=urlkey&output=json"
)
URLSCAN_SEARCH = "https://urlscan.io/api/v1/search/?q=page.domain%3A{domain}&size=10000"
_SLUG_SHAPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,60}$")
_SLUG_JUNK = {
    "_next", "api", "static", "assets", "meeting", "b", "favicon.ico",
    "robots.txt", "sitemap.xml", "embed", "css", "js", "images", "img",
}

# Archive operators ask that clients identify themselves. Set JOB_SCRAPER_CONTACT to
# your own email so a server operator can reach *you* about *your* traffic.
#
# Must be ASCII: http.client encodes headers as latin-1, so a stray em-dash or an
# accented character here makes every single request raise before it leaves the
# process. Non-ASCII is stripped rather than allowed to break the run.
_CONTACT = (
    os.environ.get("JOB_SCRAPER_CONTACT")
    or os.environ.get("ASHBY_SCRAPER_CONTACT")  # the name this had before Greenhouse
    or "set JOB_SCRAPER_CONTACT"
)
UA = f"job-boards-scraper/1.0 (public posting APIs; contact: {_CONTACT})".encode(
    "ascii", "ignore"
).decode()

# `ats` and `company` identify the board; the rest is normalised from whichever API
# it came from. `matched` holds --grep context and is empty without it.
FIELDS = [
    "ats", "company", "id", "title", "department", "team", "employmentType",
    "location", "isRemote", "workplaceType", "publishedAt", "jobUrl", "matched",
]

_HTML_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


class NotFound(Exception):
    """Board slug returned 404 — not a customer of that ATS (or never was)."""


class NotModified(Exception):
    """Server answered 304: the body is byte-identical to what we last fetched."""


class RateLimited(Exception):
    """Common Crawl returned 503. Per their docs this means the request rate was
    too high; a repeatedly-abusive IP can be blocked for 24 hours."""


# Hosts whose connections are pooled. Only the posting APIs: they take one request
# per board — over 13,000 in a full run — and a fresh TLS handshake for each was
# measured at 102ms against 64ms on a reused connection. Everything else (the
# archives, urlscan) is a handful of requests per run and may redirect, which
# urlopen handles and a raw connection would not, so those stay on urlopen.
_POOLED_HOSTS = {
    "api.ashbyhq.com",
    "boards-api.greenhouse.io",
    "api.lever.co",
}
# One connection per thread per host. Sharing across threads would need a lock and
# serialise the pool; a thread-local dict keeps the 8 workers independent, so a full
# run opens ~8 connections per host rather than one per board.
_CONNECTIONS = threading.local()


def _lower_headers(items) -> dict:
    """Lowercase header names.

    urlopen returns an email.message.Message, which looks keys up case-insensitively.
    A plain dict does not, so the pooled path has to normalise or a vendor changing
    `Content-Encoding` to `content-encoding` would silently skip gunzipping and hand
    back compressed bytes. Not hypothetical: these three APIs already disagree about
    the casing of `ETag`.
    """
    return {k.lower(): v for k, v in (items.items() if hasattr(items, "items") else items)}


def _pooled_request(url: str, method: str, timeout: int, headers: dict) -> tuple[int, dict, bytes]:
    """One request over a reused per-thread connection. Returns (status, headers, body).

    A pooled connection can be closed by the server between requests, which surfaces
    as an exception on the next use rather than at close time, so a dead connection is
    dropped and retried once before giving up.
    """
    parts = urllib.parse.urlsplit(url)
    pool = getattr(_CONNECTIONS, "pool", None)
    if pool is None:
        pool = _CONNECTIONS.pool = {}
    target = parts.path + (f"?{parts.query}" if parts.query else "")

    for attempt in (0, 1):
        conn = pool.get(parts.netloc)
        if conn is None:
            conn = pool[parts.netloc] = http.client.HTTPSConnection(
                parts.netloc, timeout=timeout
            )
        try:
            conn.request(method, target, headers=headers)
            resp = conn.getresponse()
            body = resp.read()  # must drain, or the connection cannot be reused
            return resp.status, _lower_headers(resp.getheaders()), body
        except (http.client.HTTPException, OSError):
            try:
                conn.close()
            except Exception:
                pass
            pool.pop(parts.netloc, None)
            if attempt:
                raise
    raise RuntimeError("unreachable")


def _single_request(
    url: str, method: str, timeout: int, etag: str | None = None
) -> tuple[int, dict, bytes]:
    """One request, pooled where that is safe and via urlopen everywhere else."""
    headers = {"User-Agent": UA, "Accept-Encoding": "gzip"}
    if etag:
        headers["If-None-Match"] = etag
    if urllib.parse.urlsplit(url).netloc in _POOLED_HOSTS:
        return _pooled_request(url, method, timeout, headers)
    req = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _lower_headers(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, _lower_headers(e.headers or {}), e.read()


# ponytail: seconds-form Retry-After only. The HTTP-date form is legal but none of
# these APIs send it, and falling back to the exponential delay is already correct.
def _retry_delay(retry_after: str | None, attempt: int, cap: float = 30.0) -> float:
    """How long to wait before retrying a throttled request.

    Capped: a server asking for an hour would stall a 13,000-board run behind one
    slug, and at that point giving up and logging the board is the better trade.
    """
    try:
        return min(float(retry_after), cap)
    except (TypeError, ValueError):
        return float(2**attempt)


def fetch(
    url: str,
    timeout: int = 30,
    retries: int = 4,
    method: str = "GET",
    etag: str | None = None,
    meta: dict | None = None,
) -> bytes:
    """GET a URL, transparently gunzipping. Raises NotFound on 404.

    Common Crawl's CDX index 502/504s under load often enough that a single
    attempt fails maybe half the time, so 5xx gets exponential backoff.

    429 and 403 get the same backoff. They are 4xx, so without this they took the
    raise-immediately path and a throttled board was dropped for the whole run: a
    real Greenhouse scrape lost 8 consecutive slugs that way. `Retry-After` wins
    over the exponential delay when the server sends it, since that is the server
    telling us exactly how long it wants.
    """
    for attempt in range(retries):
        try:
            status, headers, body = _single_request(url, method, timeout, etag)
            if meta is not None:
                meta["etag"] = headers.get("etag")
            if status == 304:
                raise NotModified(url)
            if status == 404:
                raise NotFound(url)
            if status == 503:
                raise RateLimited(url)
            if status >= 500:
                if attempt == retries - 1:
                    raise urllib.error.HTTPError(url, status, "server error", None, None)
                time.sleep(2**attempt)
                continue
            if status in (429, 403):
                if attempt == retries - 1:
                    raise urllib.error.HTTPError(url, status, "throttled", None, None)
                time.sleep(_retry_delay(headers.get("retry-after"), attempt))
                continue
            if status >= 400:
                raise urllib.error.HTTPError(url, status, "client error", None, None)
            if headers.get("content-encoding") == "gzip":
                body = gzip.decompress(body)
            return body
        except (urllib.error.URLError, TimeoutError, http.client.HTTPException, OSError):
            if attempt == retries - 1:
                raise
            time.sleep(2**attempt)
    raise RuntimeError("unreachable")


def plain_text(value: str) -> str:
    """Strip HTML tags, decode entities, collapse whitespace."""
    return _SPACE.sub(" ", unescape(_HTML_TAG.sub(" ", value))).strip()


# --------------------------------------------------------------------------- #
# Per-ATS adapters
#
# Each API returns a different shape, so a normaliser maps it onto FIELDS and
# everything downstream — filters, CSV, SQLite — stays platform-agnostic. A
# normaliser returns None for a posting that should not be listed at all.
#
# `_description` is stripped off before the row is emitted; only --grep reads it.
# --------------------------------------------------------------------------- #


def normalize_ashby(job: dict) -> dict | None:
    if not job.get("isListed"):
        return None
    return {
        "id": str(job.get("id", "")),
        "title": job.get("title") or "",
        "department": job.get("department") or "",
        "team": job.get("team") or "",
        "employmentType": job.get("employmentType") or "",
        "location": job.get("location") or "",
        "isRemote": bool(job.get("isRemote")),
        "workplaceType": job.get("workplaceType") or "",
        "publishedAt": job.get("publishedAt") or "",
        "jobUrl": job.get("jobUrl") or "",
        "_description": job.get("descriptionPlain") or job.get("descriptionHtml") or "",
    }


def normalize_greenhouse(job: dict) -> dict | None:
    # location is a nested object, not a string. Greenhouse exposes no remote flag
    # and no department on this endpoint, so remoteness is inferred from the label.
    loc = job.get("location") or {}
    # `or ""` rather than a get() default: Greenhouse sends {"name": null}, where
    # the key exists so the default never applies and the value stays None.
    name = (loc.get("name") or "") if isinstance(loc, dict) else str(loc)
    return {
        "id": str(job.get("id", "")),
        "title": job.get("title") or "",
        "department": "",
        "team": "",
        "employmentType": "",
        "location": name,
        "isRemote": "remote" in name.lower(),
        "workplaceType": "",
        "publishedAt": job.get("first_published") or job.get("updated_at") or "",
        "jobUrl": job.get("absolute_url") or "",
        # Absent unless the request asked for ?content=true — see SOURCES.
        "_description": job.get("content") or "",
    }


def normalize_lever(job: dict) -> dict | None:
    # Two traps here. The title field is `text`, not `title` — reading `title` gives
    # a silently empty column. And createdAt is epoch milliseconds, which has to
    # become ISO or it sorts and compares wrongly against the other platforms.
    cat = job.get("categories") or {}
    created = job.get("createdAt")
    published = ""
    if isinstance(created, (int, float)):
        published = datetime.fromtimestamp(
            created / 1000, timezone.utc
        ).isoformat(timespec="seconds")
    workplace = job.get("workplaceType") or ""
    return {
        "id": str(job.get("id", "")),
        "title": job.get("text") or "",
        "department": cat.get("department") or "",
        "team": cat.get("team") or "",
        "employmentType": cat.get("commitment") or "",
        "location": cat.get("location") or "",
        "isRemote": workplace.lower() == "remote",
        "workplaceType": workplace,
        "publishedAt": published,
        "jobUrl": job.get("hostedUrl") or "",
        "_description": " ".join(
            filter(None, (job.get("descriptionPlain"), job.get("descriptionBodyPlain")))
        ),
    }


SOURCES = {
    "ashby": {
        "domains": ["jobs.ashbyhq.com"],
        "api": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
        "jobs": lambda payload: payload.get("jobs"),
        "normalize": normalize_ashby,
        # Ashby returns descriptions whether or not we want them.
        "content_param": None,
        "junk_prefixes": ("root.",),
    },
    "greenhouse": {
        "domains": ["boards.greenhouse.io", "job-boards.greenhouse.io"],
        "api": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
        "jobs": lambda payload: payload.get("jobs"),
        "normalize": normalize_greenhouse,
        # Descriptions are opt-in and cost ~26x the bytes (25KB -> 653KB gzipped
        # per board, measured), so they are requested only when --grep needs them.
        "content_param": "content=true",
        "junk_prefixes": (),
    },
    "lever": {
        "domains": ["jobs.lever.co"],
        "api": "https://api.lever.co/v0/postings/{slug}?mode=json",
        # Lever's payload IS the list; there is no wrapper object.
        "jobs": lambda payload: payload if isinstance(payload, list) else None,
        "normalize": normalize_lever,
        "content_param": None,
        "junk_prefixes": (),
    },
}


def _clean(row: dict) -> dict:
    """Trim stray whitespace. Real payloads carry tabs and newlines inside titles,
    which otherwise corrupt sort order and leak into the CSV."""
    return {
        k: (_SPACE.sub(" ", v).strip() if isinstance(v, str) and k != "_description" else v)
        for k, v in row.items()
    }


def board_url(ats: str, slug: str, want_content: bool = False) -> str:
    url = SOURCES[ats]["api"].format(slug=urllib.parse.quote(slug))
    param = SOURCES[ats]["content_param"]
    if want_content and param:
        url += ("&" if "?" in url else "?") + param
    return url


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def slug_from_url(url: str) -> str | None:
    """First path segment of a board URL, percent-decoded.

    Slugs may contain spaces, e.g. .../A1%20Garage%20Door%20Service/... -> that name.
    """
    path = urllib.parse.urlparse(url).path
    first = path.strip("/").split("/")[0]
    return urllib.parse.unquote(first) or None


def _add(seen: dict[str, str], url: str) -> None:
    """Record the first path segment of a board URL, deduping case-insensitively."""
    slug = slug_from_url(url)
    if slug:
        seen.setdefault(slug.lower(), slug)


def candidates_from_wayback(domains: list[str], since_days: int | None = None) -> dict[str, str]:
    """The Internet Archive's CDX index. Broader than Common Crawl and far more
    reliable — it is the default for that reason.

    `since_days` adds CDX's `from=` filter, which is what makes a daily refresh
    affordable: the last 30 days of Ashby captures is ~6,200 URLs against 191,117
    for the full crawl.
    """
    window = ""
    if since_days is not None:
        start = datetime.now(timezone.utc) - timedelta(days=since_days)
        window = f"&from={start:%Y%m%d}"
    seen: dict[str, str] = {}
    for domain in domains:
        scope = f"last {since_days}d of " if since_days else ""
        print(f"  querying the Wayback Machine for {scope}{domain}...", file=sys.stderr)
        rows = json.loads(
            fetch(WAYBACK_CDX.format(domain=domain) + window, timeout=300, retries=3)
        )
        for row in rows[1:]:  # first row is the header
            _add(seen, row[0])
        print(f"    {len(rows) - 1} archived URLs -> {len(seen)} candidates so far",
              file=sys.stderr)
    return seen


def candidates_from_urlscan(domains: list[str]) -> dict[str, str]:
    """urlscan.io's public scan corpus.

    The Internet Archive is thorough but slow to notice a new board — a median of
    48 days between a board's first posting and its first capture. urlscan indexes
    scans people ran today, so it surfaces boards the archive has not reached yet.
    Sampled once, it found 14 live boards a full Wayback crawl had missed.

    Anonymous use is capped at 30 searches/minute per IP; this makes one per domain.
    A failure here is not fatal — Wayback remains the primary source.
    """
    seen: dict[str, str] = {}
    for domain in domains:
        url = URLSCAN_SEARCH.format(domain=urllib.parse.quote(domain))
        try:
            results = json.loads(fetch(url, timeout=60, retries=2)).get("results", [])
        except Exception as e:
            print(f"  urlscan failed for {domain} ({e}); skipping", file=sys.stderr)
            continue
        for row in results:
            _add(seen, row.get("page", {}).get("url", ""))
        print(f"  urlscan {domain}: {len(results)} scans -> {len(seen)} candidates so far",
              file=sys.stderr)
    return seen


def candidates_from_commoncrawl(domains: list[str], max_pages: int = 20) -> dict[str, str]:
    """Common Crawl's CDX index. Kept as a fallback: narrower coverage, and it
    sheds requests under load often enough to fail for hours at a time."""
    collections = json.loads(fetch(COLLINFO))
    cdx = collections[0]["cdx-api"]
    print(f"  querying Common Crawl index {collections[0]['id']}...", file=sys.stderr)
    seen: dict[str, str] = {}
    for domain in domains:
        query = f"{cdx}?url={urllib.parse.quote(domain)}%2F*&output=json&fl=url"
        # ponytail: walk pages until one comes back empty rather than asking
        # showNumPages first — that query is the most expensive one CDX offers and
        # times out far more often than the pages themselves.
        for page in range(max_pages):
            if page:
                time.sleep(1)  # Common Crawl asks for max 1 CDX request/second.
            try:
                body = fetch(f"{query}&page={page}", timeout=120, retries=6).decode()
            except NotFound:
                break
            if not body.strip():
                break
            for line in body.splitlines():  # JSONL, not a JSON array
                if line.strip():
                    _add(seen, json.loads(line)["url"])
    return seen


def plausible(slug: str, ats: str = "ashby") -> bool:
    """Cheap shape filter, so validation probes thousands of URLs and not millions.

    Archived URLs include tracking blobs, compensation strings and JS fragments as
    "path segments". Every live slug observed is alphanumeric plus space, dot,
    underscore or hyphen; `root.<uuid>` is Ashby's internal embed path, never a board.
    """
    lower = slug.lower()
    return (
        bool(_SLUG_SHAPE.match(slug))
        and lower not in _SLUG_JUNK
        and not lower.startswith(SOURCES.get(ats, {}).get("junk_prefixes", ()))
        and not re.fullmatch(r"[0-9a-f-]{30,}", lower)
    )


def board_exists(ats: str, slug: str) -> bool:
    """HEAD the posting API: 200 for a real board, 404 otherwise.

    HEAD returns the status with a zero-length body, so validating thousands of
    candidates costs nothing. A GET would download hundreds of kilobytes per live
    board — gigabytes just to learn which slugs are real.
    """
    try:
        fetch(board_url(ats, slug), timeout=25, retries=2, method="HEAD")
        return True
    except NotFound:
        return False
    except Exception:
        return False  # transient failure: drop it, the next refresh can find it


def discover_boards(ats: str, concurrency: int = 8, recent_days: int | None = None) -> list[str]:
    """Find board slugs for one ATS: harvest candidates, then validate each.

    `recent_days` switches to the cheap mode: only archive captures from that window,
    plus urlscan.io, which indexes scans run today rather than waiting on the
    archive's ~48-day median capture lag. Measured at ~4 minutes against ~26 for the
    full crawl, and purely additive: one run added 14 boards and lost none.
    """
    domains = SOURCES[ats]["domains"]
    print(f"{ats}: discovering boards", file=sys.stderr)
    try:
        seen = candidates_from_wayback(domains, since_days=recent_days)
    except Exception as e:
        print(f"  Wayback failed ({e}); falling back to Common Crawl", file=sys.stderr)
        seen = candidates_from_commoncrawl(domains)
    if recent_days is not None:
        # Additive: urlscan finds boards the archive has not reached, and a failure
        # there must not lose the Wayback results already gathered.
        for key, value in candidates_from_urlscan(domains).items():
            seen.setdefault(key, value)

    candidates = sorted((s for s in seen.values() if plausible(s, ats)), key=str.lower)
    print(f"  validating {len(candidates)} plausible slugs...", file=sys.stderr)
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        live = [
            s for s, ok in zip(candidates, pool.map(lambda x: board_exists(ats, x), candidates))
            if ok
        ]
    print(f"  {len(live)} live boards ({len(candidates) - len(live)} dead)", file=sys.stderr)

    # Discovery only sees what the archive captured, so a real board that was never
    # crawled is invisible to it. Union in every slug already known-good rather than
    # letting a refresh lose boards an earlier run had.
    known = {s.lower(): s for s in live}
    for path in (BOARDS_SEED, BOARDS_CACHE):
        for slug in _read_boards(path).get(ats, []):
            known.setdefault(slug.lower(), slug)
    if len(known) > len(live):
        print(f"  +{len(known) - len(live)} from seed/previous runs", file=sys.stderr)
    return sorted(known.values(), key=str.lower)


def _read_boards(path: Path) -> dict[str, list[str]]:
    """Read a board file, accepting the pre-multi-ATS flat list as Ashby."""
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return {"ashby": data} if isinstance(data, list) else data


RECENT_WINDOW_DAYS = 30


def load_boards(
    refresh: bool,
    ats_list: list[str],
    concurrency: int = 8,
    recent: bool = False,
) -> dict[str, list[str]]:
    if not refresh and not recent:
        # Merge per platform rather than taking the first file that has anything.
        # The cache may cover only the platforms the last refresh ran, and picking
        # it wholesale would silently return zero boards for all the others.
        merged: dict[str, list[str]] = {}
        for path in (BOARDS_SEED, BOARDS_CACHE):  # cache wins where it has entries
            for ats, slugs in _read_boards(path).items():
                if slugs:
                    merged[ats] = slugs
        got = {a: merged.get(a, []) for a in ats_list}
        if any(got.values()):
            summary = ", ".join(f"{a} {len(v)}" for a, v in got.items())
            print(f"{sum(len(v) for v in got.values())} boards ({summary})",
                  file=sys.stderr)
            for ats, slugs in got.items():
                if not slugs:
                    print(f"  note: no {ats} boards cached; run --refresh-boards",
                          file=sys.stderr)
            return got
    boards = _read_boards(BOARDS_CACHE)
    for ats in ats_list:
        try:
            boards[ats] = discover_boards(
                ats, concurrency, recent_days=RECENT_WINDOW_DAYS if recent else None
            )
        except RateLimited:
            sys.exit(
                "Common Crawl returned 503: request rate too high. Their docs say to "
                "slow down, and that a repeatedly-abusive IP can be blocked for 24 "
                "hours. Wait before retrying."
            )
        except (urllib.error.URLError, TimeoutError) as e:
            sys.exit(
                f"board discovery failed for {ats}: {e}\n"
                "Both the Wayback Machine and Common Crawl were unreachable. Retry "
                "later; the bundled boards.seed.json means this phase is optional."
            )
    BOARDS_CACHE.write_text(json.dumps(boards, indent=2))
    total = sum(len(boards.get(a, [])) for a in ats_list)
    print(f"cached {total} slugs -> {BOARDS_CACHE.name}", file=sys.stderr)
    return {a: boards.get(a, []) for a in ats_list}


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #


_DURATION = re.compile(r"^(\d+)\s*([dwmy]?)$", re.IGNORECASE)
_DURATION_DAYS = {"d": 1, "w": 7, "m": 30, "y": 365, "": 1}


def parse_duration(text: str) -> int:
    """'7d' / '2w' / '3m' / '1y' / '7' -> days. Raises ValueError on anything else."""
    m = _DURATION.match(text.strip())
    if not m:
        raise ValueError(f"expected something like 7d, 2w, 3m or 90; got {text!r}")
    return int(m.group(1)) * _DURATION_DAYS[m.group(2).lower()]


def published_within(published_at: str, cutoff: datetime) -> bool:
    """Is this posting newer than the cutoff?

    An unparseable or missing date counts as too old. Every one of 308,100 rows
    measured had a usable date, so this only guards against a future API change —
    and excluding is the safe direction, since --since exists to promise freshness.
    """
    if not published_at:
        return False
    try:
        return datetime.fromisoformat(published_at.replace("Z", "+00:00")) >= cutoff
    except ValueError:
        return False


def matches(job_title: str, wanted: str, mode: str = "fuzzy") -> bool:
    """Does a posting's title match what the user asked for? Case-insensitive.

    exact  — the whole title equals the query.
             "software engineer" matches "Software Engineer" only.
    fuzzy  — either string contains the other, so it works in both directions:
             a short query finds longer titles ("software engineer" ->
             "Senior Software Engineer, Backend") and a long query still finds
             the short title it contains ("senior software engineer, backend"
             -> "Software Engineer").

             The reverse direction requires the title to be at least two words.
             Without that, querying "senior software engineer" also matches jobs
             titled just "Engineer", "Software", or "Senior" — every one-word
             title that happens to appear in the query.
    """
    title, want = job_title.lower().strip(), wanted.lower().strip()
    if not title or not want:
        return False  # an empty query would otherwise match every job
    if mode == "exact":
        return title == want
    return want in title or (len(title.split()) >= 2 and title in want)


def fragments(text: str, pattern: re.Pattern[str], limit: int = 2) -> list[str]:
    """Windows of surrounding text for each match, so a hit can be judged in context."""
    found: list[str] = []
    for match in pattern.finditer(text):
        window = text[max(0, match.start() - 90) : match.end() + 150].strip()
        if window not in found:
            found.append(window)
        if len(found) == limit:
            break
    return found


def scan_board(
    ats: str,
    slug: str,
    wanted: str | None,
    remote_only: bool,
    mode: str,
    pattern: re.Pattern[str] | None = None,
    cutoff: datetime | None = None,
    etag: str | None = None,
    meta: dict | None = None,
) -> list[dict]:
    """Fetch one board, return flat rows for matching listed jobs.

    Descriptions are read only when --grep needs them, and even then only the
    matched fragments survive — the full text dominates every payload, and holding
    thousands of boards' worth would be gigabytes. --grep matches against the title
    as well as the description; see the loop below for why they are searched apart.
    """
    source = SOURCES[ats]
    payload = json.loads(
        fetch(board_url(ats, slug, want_content=pattern is not None), etag=etag, meta=meta)
    )
    jobs = source["jobs"](payload)
    if not isinstance(jobs, list):
        # Fail loudly on a shape change rather than silently reporting no results.
        raise ValueError(f"{ats}/{slug}: response has no jobs array")

    rows = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        norm = source["normalize"](job)
        if norm is None:
            continue
        norm = _clean(norm)
        if cutoff is not None and not published_within(norm["publishedAt"], cutoff):
            continue
        if wanted and not matches(norm["title"], wanted, mode):
            continue
        if remote_only and not norm["isRemote"]:
            continue

        hits: list[str] = []
        if pattern is not None:
            # The title is searched too. Searching only the description dropped
            # postings whose subject is *in the title* — "SSO Integrations Lead",
            # "Identity Platform Engineer" — whenever the body happened to phrase
            # it differently. Searched separately rather than concatenated, or a
            # regex could match across the seam and report a hit in neither field.
            hits = fragments(plain_text(norm["title"]), pattern, limit=1)
            for window in fragments(plain_text(norm["_description"]), pattern):
                if window not in hits:
                    hits.append(window)
            if not hits:
                continue

        norm.pop("_description", None)
        rows.append({"ats": ats, "company": slug, **norm, "matched": " … ".join(hits)})
    return rows


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

_INDEXES = """
CREATE INDEX IF NOT EXISTS jobs_company ON jobs(ats, company);
CREATE INDEX IF NOT EXISTS jobs_last_seen ON jobs(last_seen);
CREATE INDEX IF NOT EXISTS jobs_closed_at ON jobs(closed_at);
"""


def _create_table(con: sqlite3.Connection, name: str = "jobs") -> None:
    # (ats, id) rather than id alone: Greenhouse ids are integers while Ashby and
    # Lever use UUIDs, so a bare id risks a collision that would silently overwrite
    # one platform's posting with another's.
    body = "".join(f"{f} TEXT," for f in FIELDS if f not in ("ats", "id"))
    con.executescript(f"""
        CREATE TABLE IF NOT EXISTS {name} (
            ats         TEXT NOT NULL,
            id          TEXT NOT NULL,
            {body}
            first_seen  TEXT NOT NULL,
            last_seen   TEXT NOT NULL,
            closed_at   TEXT,
            PRIMARY KEY (ats, id)
        );
    """)


def _prepare(con: sqlite3.Connection) -> None:
    """Create the table, migrate an older one, then index — in that order.

    Indexes come last because an index on a column the migration has not added yet
    cannot be created; putting them in the same script as CREATE TABLE is what broke
    the previous migration.
    """
    cols = {c[1] for c in con.execute("PRAGMA table_info(jobs)")}
    if cols and "ats" not in cols:
        # Pre-multi-ATS database. The primary key is changing, which ALTER TABLE
        # cannot do, so rebuild and label every existing row as Ashby.
        if "closed_at" not in cols:
            con.execute("ALTER TABLE jobs ADD COLUMN closed_at TEXT")
            cols.add("closed_at")
        carried = [
            c for c in (*FIELDS, "first_seen", "last_seen", "closed_at")
            if c in cols and c != "ats"
        ]
        _create_table(con, "jobs_new")
        con.execute(
            f"INSERT INTO jobs_new (ats, {','.join(carried)}) "
            f"SELECT 'ashby', {','.join(carried)} FROM jobs"
        )
        con.execute("DROP TABLE jobs")
        con.execute("ALTER TABLE jobs_new RENAME TO jobs")
    else:
        _create_table(con)
        if cols and "closed_at" not in cols:
            con.execute("ALTER TABLE jobs ADD COLUMN closed_at TEXT")
    con.executescript(_INDEXES)


def sort_rows(rows: list[dict], mode: str) -> None:
    """Order rows in place. `recent` puts the newest posting first.

    Comparing the ISO strings is correct without parsing, because every adapter
    normalises to ISO — including Lever's epoch milliseconds. Two passes rather than
    one compound key: Python's sort is stable, so sorting by board first and then by
    date gives newest-first with a deterministic order inside each timestamp. An
    empty date is the smallest string, so reversing puts undated rows last.
    """
    rows.sort(key=lambda r: (r["ats"], str(r["company"]).lower(), str(r["title"]).lower()))
    if mode == "recent":
        rows.sort(key=lambda r: str(r["publishedAt"]), reverse=True)


def may_close_postings(
    title: str | None,
    pattern: re.Pattern[str] | None,
    cutoff: datetime | None,
    new_only: bool,
) -> bool:
    """Did this run see every posting on the boards it scanned?

    Only such a run may stamp closed_at. Every filter has to be listed here: a run
    that skipped old postings, or ones it had seen before, did not observe them and
    cannot conclude they are gone. Miss one and, for example, `--all --since 7d`
    would mark every posting older than a week as closed.
    """
    return not (title or pattern or cutoff or new_only)


_ETAG_SCHEMA = """
CREATE TABLE IF NOT EXISTS board_etag (
    ats     TEXT NOT NULL,
    company TEXT NOT NULL,
    etag    TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (ats, company)
);
"""


def may_use_etags(
    title: str | None,
    pattern: re.Pattern[str] | None,
    cutoff: datetime | None,
    remote_only: bool,
    new_only: bool,
) -> bool:
    """Is a 304 safe to treat as "nothing new on this board"?

    Only for a run that is unfiltered apart from --new-only. A 304 says the body is
    unchanged since the stored etag; concluding "no new postings" from that also
    requires that the fetch which stored the etag actually persisted every posting.
    A --title run stores rows for matching postings only, so trusting its etag later
    would skip a board whose non-matching postings were never recorded.

    Storing and using etags are gated on the same predicate, so an etag in the
    database always came from a full, persisted fetch.
    """
    return new_only and not (title or pattern or cutoff or remote_only)


def load_etags(db_path: Path) -> dict[tuple[str, str], str]:
    """Stored etags, keyed by board. Empty if the table does not exist yet."""
    if not db_path.exists():
        return {}
    with sqlite3.connect(db_path) as con:
        con.executescript(_ETAG_SCHEMA)
        return {(a, c): e for a, c, e in con.execute(
            "SELECT ats, company, etag FROM board_etag")}


def save_etags(db_path: Path, etags: dict[tuple[str, str], str], seen_at: str) -> None:
    if not etags:
        return
    with sqlite3.connect(db_path) as con:
        con.executescript(_ETAG_SCHEMA)
        con.executemany(
            "INSERT INTO board_etag (ats, company, etag, seen_at) VALUES (?,?,?,?) "
            "ON CONFLICT(ats, company) DO UPDATE SET etag=excluded.etag, "
            "seen_at=excluded.seen_at",
            [(a, c, e, seen_at) for (a, c), e in etags.items()],
        )


def known_keys(db_path: Path) -> set[tuple[str, str]]:
    """The (ats, id) pairs already recorded. Empty set if the database is new."""
    if not db_path.exists():
        return set()
    with sqlite3.connect(db_path) as con:
        if not con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone():
            return set()
        cols = {c[1] for c in con.execute("PRAGMA table_info(jobs)")}
        if "ats" not in cols:  # pre-multi-ATS database; everything in it is Ashby
            return {("ashby", r[0]) for r in con.execute("SELECT id FROM jobs")}
        return {tuple(r) for r in con.execute("SELECT ats, id FROM jobs")}


def save(
    rows: list[dict],
    db_path: Path,
    seen_at: str,
    covered: list[tuple[str, str]] | None = None,
) -> tuple[int, int, int]:
    """Upsert rows keyed on (ats, posting id). Returns (new, updated, closed).

    first_seen is preserved across runs and last_seen is refreshed, which is the
    whole reason to keep a database rather than just the CSV: it answers "when did
    this posting appear" and "is it still up" across scrapes.

    `covered` is the list of (ats, board) pairs this run scanned exhaustively, and is
    only passed for an unfiltered run. On a filtered run a missing job is ambiguous —
    it may be gone, or it may simply not have matched --title — so only an unfiltered
    run has the standing to close a posting. Anything on a covered board that this run
    did not see is stamped closed_at; anything that reappears has it cleared.
    """
    keyed = {(r["ats"], r["id"]): r for r in rows if r.get("id")}
    cols = ["ats", "id", *[f for f in FIELDS if f not in ("ats", "id")]]
    with sqlite3.connect(db_path) as con:
        _prepare(con)
        known = {tuple(r) for r in con.execute("SELECT ats, id FROM jobs")}
        con.executemany(
            f"INSERT INTO jobs ({','.join(cols)}, first_seen, last_seen) "
            f"VALUES ({','.join('?' * len(cols))}, ?, ?) "
            "ON CONFLICT(ats, id) DO UPDATE SET "
            # Everything except first_seen is refreshed; titles and locations do
            # get edited in place on live postings. `matched` is the exception: it
            # belongs to whichever --grep produced it, so a later title-only run
            # must not blank out context an earlier search found.
            + ",".join(
                f"{c}=excluded.{c}" for c in cols if c not in ("ats", "id", "matched")
            )
            + ", matched=CASE WHEN excluded.matched != '' "
            "THEN excluded.matched ELSE jobs.matched END"
            ", last_seen=excluded.last_seen",
            [
                [str(r.get(c, "")) for c in cols] + [seen_at, seen_at]
                for r in keyed.values()
            ],
        )
        closed = 0
        if covered is not None:
            con.execute(
                "CREATE TEMP TABLE scanned (ats TEXT, company TEXT, "
                "PRIMARY KEY (ats, company))"
            )
            con.executemany("INSERT OR IGNORE INTO scanned VALUES (?, ?)", covered)
            cur = con.execute(
                "UPDATE jobs SET closed_at = ? "
                "WHERE closed_at IS NULL AND last_seen < ? "
                "AND (ats, company) IN (SELECT ats, company FROM scanned)",
                (seen_at, seen_at),
            )
            closed = cur.rowcount
            # A posting that came back is open again.
            con.execute(
                "UPDATE jobs SET closed_at = NULL "
                "WHERE closed_at IS NOT NULL AND last_seen = ?",
                (seen_at,),
            )

    new = len(keyed.keys() - known)
    return new, len(keyed) - new, closed


# --------------------------------------------------------------------------- #


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--ats",
        default="all",
        help="comma-separated platforms: " + ", ".join(SOURCES) + " (default: all)",
    )
    p.add_argument(
        "--title",
        help="title to match (default: 'software engineer', unless --grep is given)",
    )
    p.add_argument(
        "--match",
        choices=("fuzzy", "exact"),
        default="fuzzy",
        help="fuzzy: either string contains the other (default). exact: titles must be equal",
    )
    p.add_argument(
        "--grep",
        metavar="REGEX",
        help="case-insensitive regex searched against the job title and description; "
        "matching context lands in the 'matched' column. On Greenhouse this "
        "requests full content, which is ~26x the bytes",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="every listed job on every board, no title or description filter",
    )
    p.add_argument(
        "--since",
        metavar="AGE",
        help="only postings published within this window: 7d, 2w, 3m, 1y, or a bare "
        "number of days. Across all platforms the median posting is 62 days old; "
        "--since 7d returns ~11%% of them at a median age of 4 days",
    )
    p.add_argument(
        "--new-only",
        action="store_true",
        help="only postings the database has never seen. Catches an old requisition "
        "that appeared today, which --since cannot. Requires the database",
    )
    p.add_argument(
        "--sort",
        choices=("board", "recent"),
        default="board",
        help="board: grouped by platform and company (default). recent: newest "
        "posting first, which is what you want with --since",
    )
    p.add_argument("--limit", type=int, help="max boards per platform (default: all)")
    p.add_argument("--remote", action="store_true", help="only remote postings")
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--refresh-boards", action="store_true", help="re-crawl slug lists")
    p.add_argument(
        "--refresh-recent",
        action="store_true",
        help="cheap daily discovery: urlscan.io plus the last 30 days of archive "
        "captures. ~4 minutes against the full crawl's ~26",
    )
    p.add_argument("--out", default="job-boards", help="output filename prefix")
    p.add_argument(
        "--boards-from",
        metavar="FILE",
        help="scan only the boards in this file instead of the discovered list. "
        "Takes the same shape as boards.json, which is what <out>.failed.json is "
        "written in, so retrying a run's failures is --boards-from <out>.failed.json",
    )
    p.add_argument(
        "--db",
        default="job-boards.db",
        help="SQLite file accumulating every scrape (default: job-boards.db)",
    )
    p.add_argument("--no-db", action="store_true", help="skip the database write")
    args = p.parse_args()

    ats_list = list(SOURCES) if args.ats == "all" else [
        a.strip() for a in args.ats.split(",") if a.strip()
    ]
    unknown = [a for a in ats_list if a not in SOURCES]
    if unknown:
        sys.exit(f"unknown --ats {', '.join(unknown)}; choose from {', '.join(SOURCES)}")

    if args.all and (args.title or args.grep):
        sys.exit("--all takes no filters; drop --title/--grep or drop --all")
    if args.new_only and args.no_db:
        sys.exit("--new-only compares against the database; it cannot be used with --no-db")
    cutoff = None
    if args.since:
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=parse_duration(args.since))
        except ValueError as e:
            sys.exit(f"--since: {e}")
    # The title default only applies when nothing else narrows the search. Applying
    # it to a --grep run would silently AND an unrequested title filter onto it.
    title = None if args.all else (
        args.title or (None if args.grep else "software engineer")
    )
    try:
        pattern = re.compile(args.grep, re.IGNORECASE) if args.grep else None
    except re.error as e:
        sys.exit(f"--grep is not a valid regex: {e}")
    if args.grep and r"\b" not in args.grep:
        # Silent and severe: `rust` matches "trust", which appears in almost every
        # description's boilerplate. Measured 1350 hits vs 72 word-bounded.
        print(
            rf"note: --grep {args.grep!r} has no \b word boundary, so it matches "
            rf"inside longer words. Consider '\b{args.grep}\b'.",
            file=sys.stderr,
        )
    if pattern is not None and "greenhouse" in ats_list:
        print(
            "note: --grep requests full descriptions from Greenhouse, roughly 26x "
            "the bytes of a normal run.",
            file=sys.stderr,
        )

    if args.boards_from:
        source = Path(args.boards_from)
        if not source.is_absolute():
            source = HERE / source
        boards = _read_boards(source)
        if not boards:
            sys.exit(f"--boards-from {args.boards_from}: no boards in that file")
    else:
        boards = load_boards(
            args.refresh_boards, ats_list, args.concurrency, recent=args.refresh_recent
        )
    scanned = [
        (ats, slug)
        for ats in ats_list
        for slug in (boards.get(ats, [])[: args.limit] if args.limit else boards.get(ats, []))
    ]
    criteria = [f"title {title!r} ({args.match})" if title else "",
                f"title or description /{args.grep}/" if args.grep else "",
                f"published within {args.since}" if args.since else "",
                "unseen postings only" if args.new_only else ""]
    what = " + ".join(c for c in criteria if c) or "every listed job"
    print(f"scanning {len(scanned)} boards across {len(ats_list)} platforms "
          f"for {what}...", file=sys.stderr)

    rows: list[dict] = []
    dead: set[tuple[str, str]] = set()
    # Which boards failed, not just how many. A count on stderr left no way to
    # re-scan the survivors of a throttle without repeating all 13,000 boards.
    # list.append is atomic under the GIL, so this needs no lock — same as `dead`.
    failed: list[tuple[str, str]] = []

    # Conditional requests, but only when a 304 genuinely means "nothing new here".
    db_path = HERE / args.db if not Path(args.db).is_absolute() else Path(args.db)
    conditional = not args.no_db and may_use_etags(
        title, pattern, cutoff, args.remote, args.new_only
    )
    etags = load_etags(db_path) if conditional else {}
    fresh_etags: dict[tuple[str, str], str] = {}
    unchanged = 0
    etag_lock = threading.Lock()
    if conditional and etags:
        print(f"  {len(etags)} boards have a stored etag; unchanged ones will be skipped",
              file=sys.stderr)

    def work(item: tuple[str, str]) -> list[dict]:
        nonlocal unchanged
        ats, slug = item
        meta: dict = {}
        for attempt in range(2):
            try:
                found = scan_board(
                    ats, slug, title, args.remote, args.match, pattern, cutoff,
                    etag=etags.get(item) if conditional else None,
                    meta=meta if conditional else None,
                )
                if conditional and meta.get("etag"):
                    with etag_lock:
                        fresh_etags[item] = meta["etag"]
                return found
            except NotModified:
                with etag_lock:
                    unchanged += 1
                return []
            except NotFound:
                dead.add(item)
                return []
            except Exception as e:
                if attempt:
                    failed.append(item)
                    print(f"  ! {ats}/{slug}: {e}", file=sys.stderr)
        return []

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for i, found in enumerate(pool.map(work, scanned), 1):
            rows.extend(found)
            if i % 500 == 0 or i == len(scanned):
                print(
                    f"  {i}/{len(scanned)} boards | {len(dead)} 404 | "
                    f"{len(failed)} err | {unchanged} unchanged | {len(rows)} matches",
                    file=sys.stderr,
                )

    if args.new_only:
        # Drop anything the database has already recorded. Done here rather than in
        # scan_board so the board fetch stays independent of storage.
        before = len(rows)
        seen_before = known_keys(db_path)
        rows = [r for r in rows if (r["ats"], r["id"]) not in seen_before]
        print(f"  --new-only: {before - len(rows)} already known, {len(rows)} new",
              file=sys.stderr)

    sort_rows(rows, args.sort)

    # BOM so Excel renders the en-dashes and bullets in location strings.
    csv_path = HERE / f"{args.out}.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    json_path = HERE / f"{args.out}.json"
    json_path.write_text(json.dumps(rows, indent=2))

    # Boards that errored, in the same shape as boards.json, so the retry is just
    # `--boards-from <out>.failed.json`. Written only when something failed, and
    # removed otherwise so a stale file from an earlier run cannot be re-read as
    # if it described this one. 404s are excluded: a dead slug is an expected
    # answer, not a failure, and it is already self-pruned below.
    failed_path = HERE / f"{args.out}.failed.json"
    if failed:
        by_platform: dict[str, list[str]] = {}
        for ats, slug in failed:
            by_platform.setdefault(ats, []).append(slug)
        failed_path.write_text(json.dumps(by_platform, indent=2))
        print(f"  {len(failed)} board{'s' if len(failed) != 1 else ''} failed -> "
              f"{failed_path.name} (retry with --boards-from {failed_path.name})",
              file=sys.stderr)
    elif failed_path.exists():
        failed_path.unlink()

    # Self-prune: drop slugs that 404'd so later runs skip them. Skipped for
    # --boards-from as well as --limit: `boards` is then a caller-supplied subset,
    # and with no cache on disk to fall back to it would be written out as though
    # it were the whole discovered board list.
    if dead and not args.limit and not args.boards_from:
        cached = _read_boards(BOARDS_CACHE) or boards
        for ats in ats_list:
            cached[ats] = [s for s in cached.get(ats, []) if (ats, s) not in dead]
        BOARDS_CACHE.write_text(json.dumps(cached, indent=2))

    written = f"{csv_path.name}, {json_path.name}"
    if not args.no_db:
        seen_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if conditional:
            save_etags(db_path, fresh_etags, seen_at)
        # Only an unfiltered run saw everything, so only it may close postings.
        covered = scanned if may_close_postings(title, pattern, cutoff, args.new_only) else None
        new, updated, closed = save(rows, db_path, seen_at, covered)
        written += f", {db_path.name} ({new} new, {updated} already seen"
        written += f", {closed} closed)" if covered is not None else ")"

    by_ats = {a: sum(1 for r in rows if r["ats"] == a) for a in ats_list}
    print(f"\n{len(rows)} jobs ({', '.join(f'{a} {n}' for a, n in by_ats.items())}) "
          f"-> {written}", file=sys.stderr)


if __name__ == "__main__":
    main()
