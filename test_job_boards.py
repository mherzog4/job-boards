#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Offline self-check. Run: uv run test_job_boards.py  (or python3 test_job_boards.py)"""

import csv
import io
from datetime import datetime, timezone
import json
import re
import sqlite3
import tempfile
from pathlib import Path

from job_boards import (
    FIELDS,
    board_url,
    fragments,
    matches,
    normalize_ashby,
    normalize_greenhouse,
    normalize_lever,
    plain_text,
    plausible,
    save,
    scan_board,
    slug_from_url,
)

BASE = {f: "" for f in FIELDS}


def _with_fetch(payload, fn):
    """Run fn with job_boards.fetch stubbed to return payload."""
    import job_boards
    original = job_boards.fetch
    job_boards.fetch = (
        payload if callable(payload) else (lambda *a, **k: json.dumps(payload).encode())
    )
    try:
        return fn()
    finally:
        job_boards.fetch = original


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def test_slug_parsing():
    assert slug_from_url("https://jobs.ashbyhq.com/0g/4fc6ba8a-1111?ref=x") == "0g"
    assert (
        slug_from_url("https://jobs.ashbyhq.com/A1%20Garage%20Door%20Service/x")
        == "A1 Garage Door Service"
    )
    assert slug_from_url("https://jobs.ashbyhq.com/") is None


def test_cdx_jsonl_parsing():
    """Common Crawl returns JSONL, not a JSON array, and mixes in junk paths."""
    payload = "\n".join([
        '{"url": "https://jobs.ashbyhq.com/ramp/abc-123?ref=x"}',
        '{"url": "https://jobs.ashbyhq.com/Ramp/def-456"}',        # dupe, other casing
        '{"url": "https://jobs.ashbyhq.com/A1%20Garage%20Door/x"}',  # spaces in slug
        "",                                                         # blank line
        '{"url": "https://jobs.ashbyhq.com/_next/static/z.js"}',    # junk, 404s later
        '{"url": "https://jobs.ashbyhq.com/"}',                     # no slug at all
    ])
    seen = {}
    for line in payload.splitlines():
        if not line.strip():
            continue
        slug = slug_from_url(json.loads(line)["url"])
        if slug:
            seen.setdefault(slug.lower(), slug)
    assert sorted(seen.values()) == ["A1 Garage Door", "_next", "ramp"]
    assert seen["ramp"] == "ramp"  # first-seen casing wins over "Ramp"


def test_plausible_rejects_archive_noise_but_keeps_real_slugs():
    """The archives yield millions of URLs; the shape filter makes validation cheap."""
    for good in ("ramp", "keeling-labs", "A1 Garage Door Service", "ScribdInc", "0g"):
        assert plausible(good), good
    for junk in (
        "_next", "api", "favicon.ico", "robots.txt",          # site plumbing
        "root.6511f3ee_758c_4ed4_8fce_254a11715ed7",          # Ashby embed path
        "4fc6ba8a-532f-46a7-b1f3-d5490d78120e",               # a posting id, not a board
        "$10.2K", '"80e0bf43", "environment":"production"',   # comp strings, JS blobs
        "블록웍스", "", "-" * 80,
    ):
        assert not plausible(junk), junk


def test_root_prefix_is_only_junk_for_ashby():
    """`root.<uuid>` is an Ashby embed path; it carries no meaning elsewhere."""
    assert not plausible("root.abc", "ashby")
    assert plausible("root.abc", "greenhouse")


def test_every_posting_api_host_is_pooled():
    """A new ATS added without pooling silently loses the connection reuse.

    Pooling cut connections for a 300-board sample from 300 to 23 and wall clock by
    ~19-29%. That win is per-host, so an adapter whose host is missing from
    _POOLED_HOSTS quietly opts out of it.
    """
    import urllib.parse
    from job_boards import SOURCES, _POOLED_HOSTS, board_url
    for ats in SOURCES:
        host = urllib.parse.urlsplit(board_url(ats, "example")).netloc
        assert host in _POOLED_HOSTS, f"{ats} posting API host {host} is not pooled"


def test_etags_are_only_trusted_on_an_unfiltered_run():
    """A 304 only means "no new postings" if the fetch that stored the etag kept
    every posting.

    A --title run persists matching rows only. Trusting its etag later would skip a
    board whose non-matching postings were never recorded, so they would never appear
    even once they matched a later query. Storing and using etags share this gate, so
    an etag in the database always came from a full, persisted fetch.
    """
    from datetime import timedelta
    from job_boards import may_use_etags
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    assert may_use_etags(None, None, None, False, True), "--all --new-only is the one case"
    assert not may_use_etags(None, None, None, False, False), "without --new-only, rows are needed"
    assert not may_use_etags("engineer", None, None, False, True), "--title persists a subset"
    assert not may_use_etags(None, re.compile("x"), None, False, True), "--grep persists a subset"
    assert not may_use_etags(None, None, cutoff, False, True), "--since persists a subset"
    assert not may_use_etags(None, None, None, True, True), "--remote persists a subset"


def test_a_304_skips_the_board():
    """The whole point: an unchanged board costs no body at all."""
    import job_boards as jb
    original = jb._single_request
    jb._single_request = lambda u, m, t, e=None: (304, {"etag": e}, b"")
    try:
        raised = False
        try:
            scan_board("ashby", "acme", None, False, "fuzzy", etag='W/"abc"')
        except jb.NotModified:
            raised = True
        assert raised, "a 304 must surface as NotModified so the caller can skip"
    finally:
        jb._single_request = original


def test_etags_round_trip_through_the_database():
    from job_boards import load_etags, save_etags
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.db"
        assert load_etags(db) == {}, "a missing database has no etags"
        save_etags(db, {("ashby", "acme"): 'W/"v1"'}, "2026-01-01T00:00:00+00:00")
        assert load_etags(db) == {("ashby", "acme"): 'W/"v1"'}
        save_etags(db, {("ashby", "acme"): 'W/"v2"'}, "2026-02-02T00:00:00+00:00")
        assert load_etags(db) == {("ashby", "acme"): 'W/"v2"'}, "an etag must be replaced"


def test_conditional_request_sends_if_none_match():
    """Without the header the server has nothing to compare and always returns 200."""
    import job_boards as jb
    seen = {}

    def fake_pooled(url, method, timeout, headers):
        seen.update(headers)
        return 200, {}, b"{}"

    original = jb._pooled_request
    jb._pooled_request = fake_pooled
    try:
        jb._single_request("https://api.lever.co/x", "GET", 5, 'W/"abc"')
        assert seen.get("If-None-Match") == 'W/"abc"'
        seen.clear()
        jb._single_request("https://api.lever.co/x", "GET", 5, None)
        assert "If-None-Match" not in seen, "no etag means no conditional header"
    finally:
        jb._pooled_request = original


def test_header_lookup_is_case_insensitive():
    """urlopen returned a case-insensitive Message; a plain dict is not.

    The pooled path builds its own header dict, so without normalising, a vendor
    sending `content-encoding` instead of `Content-Encoding` would skip gunzipping and
    return compressed bytes as if they were JSON. These three APIs already disagree
    about the casing of `ETag` — ashby and greenhouse send `etag`, lever sends `ETag` —
    so the disagreement is real, not hypothetical.
    """
    import gzip as gziplib
    import job_boards as jb

    payload = gziplib.compress(b'{"jobs": []}')
    for header_name in ("Content-Encoding", "content-encoding", "CONTENT-ENCODING"):
        original = jb._single_request
        jb._single_request = lambda u, m, t, e=None, _h=header_name: (
            200, jb._lower_headers([(_h, "gzip")]), payload
        )
        try:
            assert jb.fetch("https://api.lever.co/x") == b'{"jobs": []}', \
                f"gunzip failed when the server sent {_h!r}"
        finally:
            jb._single_request = original


def test_pooled_request_retries_once_on_a_dead_connection():
    """Servers close idle keep-alive connections; the next use raises, not the close.

    Without the retry a run would surface random failures on boards that happen to
    follow an idle gap.
    """
    import http.client
    import job_boards as jb

    attempts = []

    class FakeConn:
        def __init__(self, host, timeout=None):
            self.host = host
            attempts.append(host)
        def request(self, method, target, headers=None):
            if len(attempts) == 1:      # the first, stale connection fails
                raise http.client.RemoteDisconnected("closed by server")
        def getresponse(self):
            class R:
                status = 200
                def read(self): return b"ok"
                def getheaders(self): return []
            return R()
        def close(self): pass

    original = http.client.HTTPSConnection
    http.client.HTTPSConnection = FakeConn
    jb._CONNECTIONS.__dict__.pop("pool", None)
    try:
        status, _, body = jb._pooled_request("https://api.lever.co/x", "GET", 5, {"User-Agent": "t"})
    finally:
        http.client.HTTPSConnection = original
        jb._CONNECTIONS.__dict__.pop("pool", None)
    assert (status, body) == (200, b"ok")
    assert len(attempts) == 2, "a dead pooled connection must be replaced and retried once"


def test_board_exists_uses_head_and_maps_404_to_false():
    """Validation must not download board payloads — thousands of GETs would be GBs."""
    import job_boards

    calls = []

    def fake_fetch(url, timeout=30, retries=4, method="GET"):
        calls.append(method)
        if "nope" in url:
            raise job_boards.NotFound(url)
        return b""

    original = job_boards.fetch
    job_boards.fetch = fake_fetch
    try:
        assert job_boards.board_exists("ashby", "ramp") is True
        assert job_boards.board_exists("greenhouse", "nope12345") is False
    finally:
        job_boards.fetch = original
    assert calls == ["HEAD", "HEAD"], f"expected HEAD probes, got {calls}"


# --------------------------------------------------------------------------- #
# Per-ATS normalisation
# --------------------------------------------------------------------------- #


def test_normalize_ashby():
    row = normalize_ashby({
        "id": "2b401986", "title": "Senior Software Engineer, Data",
        "department": "Builder", "team": "Data", "employmentType": "FullTime",
        "location": "Remote - US", "isRemote": True, "workplaceType": "Remote",
        "publishedAt": "2026-06-30T19:02:11.162+00:00",
        "jobUrl": "https://jobs.ashbyhq.com/abridge/2b401986",
        "isListed": True, "descriptionPlain": "we use Rust",
    })
    assert row["title"] == "Senior Software Engineer, Data"
    assert row["isRemote"] is True
    assert row["publishedAt"].startswith("2026-06-30")
    assert row["_description"] == "we use Rust"
    # unlisted postings are dropped by the normaliser, not downstream
    assert normalize_ashby({"id": "x", "title": "Secret", "isListed": False}) is None


def test_normalize_greenhouse():
    """location is a nested object and there is no remote flag."""
    row = normalize_greenhouse({
        "id": 7954688, "title": "Account Executive",
        "location": {"name": "San Francisco, CA"},
        "absolute_url": "https://stripe.com/jobs/search?gh_jid=7954688",
        "first_published": "2026-06-02T08:58:57-04:00",
        "updated_at": "2026-07-27T11:17:30-04:00",
    })
    assert row["id"] == "7954688", "integer ids must become strings for the TEXT key"
    assert row["location"] == "San Francisco, CA"
    assert row["jobUrl"].endswith("gh_jid=7954688")
    assert row["publishedAt"] == "2026-06-02T08:58:57-04:00"
    assert row["isRemote"] is False
    assert normalize_greenhouse(
        {"id": 1, "title": "T", "location": {"name": "Remote - US"}}
    )["isRemote"] is True


def test_normalize_lever():
    """Two traps: the title is `text`, and createdAt is epoch milliseconds."""
    row = normalize_lever({
        "id": "f25a6c49", "text": "Compounding Pharmacy Technician",
        "categories": {"commitment": "Full-time", "location": "Romeoville, IL",
                       "team": "Pharmacy"},
        "createdAt": 1750119882479,
        "workplaceType": "onsite",
        "hostedUrl": "https://jobs.lever.co/ro/f25a6c49",
        "descriptionPlain": "compounding meds",
    })
    assert row["title"] == "Compounding Pharmacy Technician", "Lever's title key is `text`"
    assert row["employmentType"] == "Full-time"
    assert row["team"] == "Pharmacy"
    assert row["isRemote"] is False
    # epoch ms -> ISO, or it sorts wrongly against the other platforms
    assert row["publishedAt"].startswith("2025-06-17T"), row["publishedAt"]
    assert "T" in row["publishedAt"] and row["publishedAt"].endswith("+00:00")


def test_normalizers_survive_explicit_nulls():
    """Every ATS sends JSON null for fields it has no value for.

    Regression: Greenhouse sends {"location": {"name": null}}. `loc.get("name", "")`
    returns None there — the key exists, so the default never applies — and the
    subsequent .lower() raised, aborting the whole board. Seven Greenhouse boards
    were dropped entirely, xai's 220 jobs among them.
    """
    gh = normalize_greenhouse({"id": 1, "title": None, "location": {"name": None},
                               "absolute_url": None, "first_published": None})
    assert gh["location"] == "" and gh["isRemote"] is False and gh["title"] == ""

    ash = normalize_ashby({"id": "1", "isListed": True, "title": None,
                           "location": None, "descriptionPlain": None})
    assert ash["title"] == "" and ash["location"] == ""

    lev = normalize_lever({"id": "1", "text": None, "categories": None,
                           "createdAt": None, "workplaceType": None})
    assert lev["title"] == "" and lev["publishedAt"] == "" and lev["team"] == ""


def test_every_normalizer_fills_the_same_keys():
    """A missing key in one adapter becomes a silently empty column."""
    samples = [
        (normalize_ashby, {"id": "1", "isListed": True}),
        (normalize_greenhouse, {"id": 1}),
        (normalize_lever, {"id": "1"}),
    ]
    expected = {f for f in FIELDS if f not in ("ats", "company", "matched")}
    for fn, payload in samples:
        keys = set(fn(payload)) - {"_description"}
        assert keys == expected, f"{fn.__name__} produced {keys ^ expected}"


def test_greenhouse_content_param_only_when_grepping():
    """Descriptions cost ~26x the bytes, so they must be opt-in."""
    assert "content=true" not in board_url("greenhouse", "stripe")
    assert "content=true" in board_url("greenhouse", "stripe", want_content=True)
    # platforms that always return descriptions must not gain a stray parameter
    assert "content=true" not in board_url("ashby", "ramp", want_content=True)
    assert "mode=json" in board_url("lever", "ro", want_content=True)


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #


def test_fuzzy_matching():
    assert matches("Senior Software Engineer, Backend", "software engineer")
    assert matches("SOFTWARE ENGINEER II", "software engineer")
    assert matches("Software Engineer", "senior software engineer, backend")
    assert not matches("Engineering Manager", "software engineer")


def test_fuzzy_does_not_match_generic_one_word_titles():
    """The reverse direction must not drag in every one-word title in the query."""
    for junk in ("Engineer", "Software", "Senior"):
        assert not matches(junk, "senior software engineer"), junk
    assert matches("Software Engineer", "senior software engineer")


def test_empty_query_matches_nothing():
    assert not matches("Chef", "")
    assert not matches("", "software engineer")


def test_exact_matching():
    assert matches("Software Engineer", "software engineer", "exact")
    assert matches("  SOFTWARE ENGINEER  ", "software engineer", "exact")
    assert not matches("Senior Software Engineer, Backend", "software engineer", "exact")
    assert not matches("Software Engineer", "senior software engineer", "exact")


def test_plain_text_strips_markup_and_entities():
    assert plain_text("<p>Rust &amp; Go</p>\n\n  <b>here</b>") == "Rust & Go here"
    assert plain_text("") == ""


def test_fragments_give_context_and_dedupe():
    text = "we use kubernetes daily. " * 3 + "the end"
    hits = fragments(text, re.compile("kubernetes", re.IGNORECASE))
    assert len(hits) <= 2
    assert all("kubernetes" in h for h in hits)
    assert len(set(hits)) == len(hits)
    assert fragments("nothing here", re.compile("kubernetes")) == []


def test_scan_board_filters_and_flattens():
    """isListed/remote filtering, and that descriptions never reach the output."""
    board = {"jobs": [
        {"id": "1", "title": "Software Engineer", "isListed": True, "isRemote": True,
         "location": "Remote", "descriptionHtml": "<p>huge</p>",
         "descriptionPlain": "huge", "jobUrl": "u1"},
        {"id": "2", "title": "Software Engineer", "isListed": False, "isRemote": True},
        {"id": "3", "title": "Chef", "isListed": True, "isRemote": True},
        {"id": "4", "title": "Software Engineer II", "isListed": True, "isRemote": False},
    ]}
    rows = _with_fetch(board, lambda: scan_board("ashby", "acme", "software engineer", False, "fuzzy"))
    remote = _with_fetch(board, lambda: scan_board("ashby", "acme", "software engineer", True, "fuzzy"))

    assert [r["title"] for r in rows] == ["Software Engineer", "Software Engineer II"]
    assert [r["title"] for r in remote] == ["Software Engineer"]
    assert rows[0]["company"] == "acme" and rows[0]["ats"] == "ashby"
    assert set(rows[0]) == set(FIELDS), "row must be exactly the declared columns"
    assert not any("escription" in k for k in rows[0]), "descriptions must be dropped"


def test_scan_board_handles_levers_bare_list_payload():
    """Lever's payload is the list itself, not {'jobs': [...]}."""
    payload = [{"id": "a", "text": "Software Engineer", "categories": {},
                "createdAt": 1750119882479, "hostedUrl": "u"}]
    rows = _with_fetch(payload, lambda: scan_board("lever", "ro", "software engineer", False, "fuzzy"))
    assert len(rows) == 1
    assert rows[0]["ats"] == "lever" and rows[0]["title"] == "Software Engineer"


def test_grep_filters_on_description_and_records_context():
    board = {"jobs": [
        {"id": "1", "title": "Backend Engineer", "isListed": True,
         "descriptionPlain": "You will write <b>Rust</b> and Go all day."},
        {"id": "2", "title": "Backend Engineer", "isListed": True,
         "descriptionPlain": "We deeply value trust and integrity."},
    ]}
    loose = _with_fetch(board, lambda: scan_board(
        "ashby", "acme", None, False, "fuzzy", re.compile("rust", re.I)))
    strict = _with_fetch(board, lambda: scan_board(
        "ashby", "acme", None, False, "fuzzy", re.compile(r"\brust\b", re.I)))

    assert len(loose) == 2, "unbounded 'rust' also matches 'trust' — the documented footgun"
    assert len(strict) == 1, "word-bounded regex excludes 'trust'"
    assert "Rust" in strict[0]["matched"]
    assert "<b>" not in strict[0]["matched"], "markup must be stripped from fragments"


def test_invalid_payload_raises_rather_than_returning_nothing():
    raised = False
    try:
        _with_fetch({"error": "nope"}, lambda: scan_board("ashby", "acme", "engineer", False, "fuzzy"))
    except ValueError:
        raised = True
    assert raised, "a shape change must fail loudly, not read as 'no jobs found'"


def test_no_filters_returns_every_listed_job():
    board = {"jobs": [
        {"id": "1", "title": "Chef", "isListed": True},
        {"id": "2", "title": "Welder", "isListed": True},
        {"id": "3", "title": "Secret Role", "isListed": False},
    ]}
    rows = _with_fetch(board, lambda: scan_board("ashby", "acme", None, False, "fuzzy", None))
    assert [r["title"] for r in rows] == ["Chef", "Welder"]


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #


def test_db_upsert_preserves_history():
    """first_seen must survive re-scrapes; that is the point of the database."""
    row = BASE | {"ats": "ashby", "id": "job-1", "company": "acme", "title": "SWE"}
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.db"
        new, updated, _ = save([row], db, "2026-01-01T00:00:00+00:00")
        assert (new, updated) == (1, 0)
        new, updated, _ = save([row | {"title": "Senior SWE"}], db, "2026-02-02T00:00:00+00:00")
        assert (new, updated) == (0, 1)
        got = sqlite3.connect(db).execute(
            "SELECT title, first_seen, last_seen FROM jobs").fetchall()
        assert got == [("Senior SWE", "2026-01-01T00:00:00+00:00", "2026-02-02T00:00:00+00:00")]


def test_same_id_on_two_platforms_is_two_rows():
    """Greenhouse ids are integers; a bare id key would collide across platforms."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.db"
        save([
            BASE | {"ats": "ashby", "id": "123", "company": "acme", "title": "A"},
            BASE | {"ats": "greenhouse", "id": "123", "company": "other", "title": "G"},
        ], db, "2026-01-01T00:00:00+00:00")
        got = dict(sqlite3.connect(db).execute("SELECT ats, title FROM jobs"))
        assert got == {"ashby": "A", "greenhouse": "G"}


def test_db_keeps_grep_context_from_earlier_runs():
    row = BASE | {"ats": "ashby", "id": "job-1", "company": "acme"}
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.db"
        save([row | {"matched": "uses Rust daily"}], db, "2026-01-01T00:00:00+00:00")
        save([row], db, "2026-02-02T00:00:00+00:00")  # no --grep this time
        (kept,) = sqlite3.connect(db).execute("SELECT matched FROM jobs").fetchone()
        assert kept == "uses Rust daily"


def test_migrates_a_single_ats_database():
    """Upgrade path: an id-keyed, ats-less database from before Greenhouse existed.

    Fresh-schema tests never touch this branch, which is exactly how the previous
    migration shipped broken.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "old.db"
        legacy = [f for f in FIELDS if f not in ("ats", "id")]
        con = sqlite3.connect(db)
        con.executescript(f"""
            CREATE TABLE jobs (id TEXT PRIMARY KEY,
                               {','.join(f'{c} TEXT' for c in legacy)},
                               first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                               closed_at TEXT);
            CREATE INDEX jobs_company ON jobs(company);
        """)
        con.execute(
            "INSERT INTO jobs (id, company, title, first_seen, last_seen) VALUES "
            "('old-1','acme','Old Role','2026-01-01T00:00:00+00:00',"
            "'2026-01-01T00:00:00+00:00')"
        )
        con.commit()
        con.close()

        row = BASE | {"ats": "greenhouse", "id": "new-1", "company": "gh-co"}
        save([row], db, "2026-02-02T00:00:00+00:00")

        con = sqlite3.connect(db)
        cols = {c[1] for c in con.execute("PRAGMA table_info(jobs)")}
        assert "ats" in cols and "closed_at" in cols
        got = dict(con.execute("SELECT id, ats FROM jobs"))
        assert got == {"old-1": "ashby", "new-1": "greenhouse"}, \
            "existing rows must be labelled ashby, not dropped"
        # history survives the table rebuild
        (fs, title) = con.execute(
            "SELECT first_seen, title FROM jobs WHERE id='old-1'").fetchone()
        assert fs == "2026-01-01T00:00:00+00:00" and title == "Old Role"
        assert (("ats", "id") == tuple(
            c[1] for c in con.execute("PRAGMA table_info(jobs)") if c[5]
        )), "primary key must be (ats, id)"


def test_migrates_a_database_created_before_closed_at():
    """The older upgrade path still works, now via the rebuild."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "old.db"
        legacy = [f for f in FIELDS if f not in ("ats", "id")]
        con = sqlite3.connect(db)
        con.executescript(f"""
            CREATE TABLE jobs (id TEXT PRIMARY KEY,
                               {','.join(f'{c} TEXT' for c in legacy)},
                               first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
        """)
        con.execute(
            "INSERT INTO jobs (id, company, first_seen, last_seen) VALUES "
            "('old-1','acme','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
        )
        con.commit()
        con.close()

        row = BASE | {"ats": "ashby", "id": "new-1", "company": "acme"}
        new, _, closed = save([row], db, "2026-02-02T00:00:00+00:00",
                              covered=[("ashby", "acme")])
        con = sqlite3.connect(db)
        assert "closed_at" in {c[1] for c in con.execute("PRAGMA table_info(jobs)")}
        got = dict(con.execute("SELECT id, closed_at FROM jobs"))
        assert got["old-1"] == "2026-02-02T00:00:00+00:00"
        assert got["new-1"] is None
        assert (new, closed) == (1, 1)
        idx = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "jobs_closed_at" in idx


def test_unfiltered_run_closes_vanished_postings():
    a = BASE | {"ats": "ashby", "id": "a", "company": "acme"}
    b = BASE | {"ats": "ashby", "id": "b", "company": "acme"}
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.db"
        save([a, b], db, "2026-01-01T00:00:00+00:00", covered=[("ashby", "acme")])
        _, _, closed = save([a], db, "2026-02-02T00:00:00+00:00",
                            covered=[("ashby", "acme")])
        assert closed == 1
        got = dict(sqlite3.connect(db).execute("SELECT id, closed_at FROM jobs"))
        assert got["a"] is None and got["b"] == "2026-02-02T00:00:00+00:00"

        _, _, closed = save([a, b], db, "2026-03-03T00:00:00+00:00",
                            covered=[("ashby", "acme")])
        assert closed == 0
        got = dict(sqlite3.connect(db).execute("SELECT id, closed_at FROM jobs"))
        assert got["b"] is None, "a reappearing posting must reopen"


def test_filtered_run_never_closes_anything():
    """A --title run missing a job is ambiguous: gone, or just not matched."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.db"
        save([BASE | {"ats": "ashby", "id": "a", "company": "acme"},
              BASE | {"ats": "ashby", "id": "b", "company": "acme"}],
             db, "2026-01-01T00:00:00+00:00", covered=[("ashby", "acme")])
        _, _, closed = save([BASE | {"ats": "ashby", "id": "a", "company": "acme"}],
                            db, "2026-02-02T00:00:00+00:00", covered=None)
        assert closed == 0
        (open_rows,) = sqlite3.connect(db).execute(
            "SELECT COUNT(*) FROM jobs WHERE closed_at IS NULL").fetchone()
        assert open_rows == 2


def test_closing_is_scoped_to_boards_actually_scanned():
    """A --limit or --ats run must not close postings it never visited."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.db"
        save([BASE | {"ats": "ashby", "id": "a", "company": "acme"},
              BASE | {"ats": "greenhouse", "id": "z", "company": "acme"}],
             db, "2026-01-01T00:00:00+00:00",
             covered=[("ashby", "acme"), ("greenhouse", "acme")])
        # this run covered only ashby/acme, and saw nothing there
        _, _, closed = save([], db, "2026-02-02T00:00:00+00:00",
                            covered=[("ashby", "acme")])
        assert closed == 1
        got = dict(sqlite3.connect(db).execute("SELECT ats, closed_at FROM jobs"))
        assert got["ashby"] is not None
        assert got["greenhouse"] is None, \
            "same company name on another platform must be left alone"


def test_only_an_unfiltered_run_may_close_postings():
    """The highest-risk rule in the tool.

    closed_at means "this posting is gone". A run that filtered did not see the
    postings it filtered out, so it cannot make that claim. Miss one filter here and
    `--all --since 7d` silently marks everything older than a week as closed,
    destroying the fill-rate signal with no visible symptom.
    """
    from datetime import timedelta
    from job_boards import may_close_postings
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    assert may_close_postings(None, None, None, False), "a bare --all sees everything"

    assert not may_close_postings("engineer", None, None, False), "--title filters"
    assert not may_close_postings(None, re.compile("rust"), None, False), "--grep filters"
    assert not may_close_postings(None, None, cutoff, False), "--since filters"
    assert not may_close_postings(None, None, None, True), "--new-only filters"
    assert not may_close_postings("engineer", None, cutoff, True), "combinations filter"


def test_parse_duration():
    from job_boards import parse_duration
    assert parse_duration("7d") == 7
    assert parse_duration("2w") == 14
    assert parse_duration("3m") == 90
    assert parse_duration("1y") == 365
    assert parse_duration("90") == 90, "a bare number means days"
    assert parse_duration(" 7D ") == 7
    for junk in ("", "d", "-7", "7 days", "soon", "7x"):
        try:
            parse_duration(junk)
            raise AssertionError(f"{junk!r} should not parse")
        except ValueError:
            pass


def test_published_within_boundaries():
    from datetime import timedelta
    from job_boards import published_within
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)
    assert published_within((now - timedelta(days=1)).isoformat(), cutoff)
    assert published_within((now - timedelta(days=6, hours=23)).isoformat(), cutoff)
    assert not published_within((now - timedelta(days=8)).isoformat(), cutoff)
    # a Lever-style very old posting
    assert not published_within("2009-12-05T00:00:00+00:00", cutoff)
    # missing or malformed dates are excluded: --since promises freshness, so the
    # safe direction is to drop what cannot be shown to be fresh
    assert not published_within("", cutoff)
    assert not published_within("not a date", cutoff)


def test_since_filters_by_publish_date():
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    board = {"jobs": [
        {"id": "fresh", "title": "A", "isListed": True,
         "publishedAt": (now - timedelta(days=2)).isoformat()},
        {"id": "stale", "title": "B", "isListed": True,
         "publishedAt": (now - timedelta(days=400)).isoformat()},
    ]}
    cutoff = now - timedelta(days=7)
    rows = _with_fetch(board, lambda: scan_board(
        "ashby", "acme", None, False, "fuzzy", None, cutoff))
    assert [r["id"] for r in rows] == ["fresh"]
    # without a cutoff both survive
    both = _with_fetch(board, lambda: scan_board("ashby", "acme", None, False, "fuzzy"))
    assert len(both) == 2


def test_known_keys_reads_existing_and_legacy_databases():
    from job_boards import known_keys, save
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "t.db"
        assert known_keys(db) == set(), "a missing database has no known keys"
        save([BASE | {"ats": "lever", "id": "x", "company": "acme"}], db,
             "2026-01-01T00:00:00+00:00")
        assert known_keys(db) == {("lever", "x")}

        # a pre-multi-ATS database has no ats column; its rows are all Ashby
        legacy = Path(tmp) / "legacy.db"
        con = sqlite3.connect(legacy)
        con.executescript(
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, company TEXT,"
            " first_seen TEXT, last_seen TEXT);"
        )
        con.execute("INSERT INTO jobs VALUES ('old','acme','t','t')")
        con.commit(); con.close()
        assert known_keys(legacy) == {("ashby", "old")}


def test_db_skips_rows_without_an_id():
    with tempfile.TemporaryDirectory() as tmp:
        assert save([BASE | {"ats": "ashby"}], Path(tmp) / "t.db",
                    "2026-01-01T00:00:00+00:00") == (0, 0, 0)


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def test_every_cli_flag_is_documented_in_the_readme():
    """Docs drift silently; this makes it a test failure.

    --since, --new-only and --refresh-recent all shipped before the agent
    instructions mentioned them, because nothing checked. A flag that exists and
    is undocumented is a feature nobody can find.
    """
    src = (Path(__file__).parent / "job_boards.py").read_text()
    flags = sorted(set(re.findall(r'p\.add_argument\(\s*"(--[a-z-]+)"', src)))
    assert len(flags) > 10, f"flag extraction looks broken, found {flags}"
    readme = (Path(__file__).parent / "README.md").read_text()
    missing = [f for f in flags if f not in readme]
    assert not missing, f"undocumented in README.md: {missing}"


def test_sort_recent_puts_the_newest_first():
    """With --since, alphabetical order buries the thing you asked for.

    A real run returned 5,980 postings from the last 24 hours; sorted by board, the
    six-minute-old one sat somewhere in the middle.
    """
    from job_boards import sort_rows
    rows = [
        BASE | {"ats": "lever", "company": "zz", "id": "old",
                "publishedAt": "2020-01-01T00:00:00+00:00"},
        BASE | {"ats": "ashby", "company": "aa", "id": "new",
                "publishedAt": "2026-07-27T23:00:00+00:00"},
        BASE | {"ats": "greenhouse", "company": "mm", "id": "mid",
                "publishedAt": "2026-07-01T00:00:00+00:00"},
        BASE | {"ats": "ashby", "company": "bb", "id": "undated", "publishedAt": ""},
    ]
    recent = list(rows)
    sort_rows(recent, "recent")
    assert [r["id"] for r in recent] == ["new", "mid", "old", "undated"], \
        "newest first, undated last"

    board = list(rows)
    sort_rows(board, "board")
    assert [r["id"] for r in board] == ["new", "undated", "mid", "old"], \
        "board order groups by ats then company"


def test_sort_recent_is_deterministic_within_a_timestamp():
    """Two postings sharing a timestamp must not reorder between runs."""
    from job_boards import sort_rows
    same = "2026-07-27T12:00:00+00:00"
    rows = [
        BASE | {"ats": "lever", "company": "zz", "id": "3", "publishedAt": same},
        BASE | {"ats": "ashby", "company": "bb", "id": "2", "publishedAt": same},
        BASE | {"ats": "ashby", "company": "aa", "id": "1", "publishedAt": same},
    ]
    first = list(rows); sort_rows(first, "recent")
    shuffled = [rows[2], rows[0], rows[1]]; sort_rows(shuffled, "recent")
    assert [r["id"] for r in first] == [r["id"] for r in shuffled] == ["1", "2", "3"]


def test_wiki_does_not_cite_tests_that_do_not_exist():
    """Generated docs can be confidently wrong, not just incomplete.

    A regeneration once listed `test_migrates_a_pre_multi_ats_database`, which has
    never existed — the real name is `test_migrates_a_single_ats_database`. Checking
    that a doc *mentions* something cannot catch that; checking that what it mentions
    is *real* can, and it is the cheap half of keeping generated prose honest.
    """
    here = Path(__file__).parent
    suite = set(re.findall(r"^def (test_\w+)", (here / "test_job_boards.py").read_text(), re.M))
    cited = set()
    for page in (here / "openwiki").rglob("*.md"):
        cited |= set(re.findall(r"\btest_\w+", page.read_text()))
    cited.discard("test_job_boards")  # the module, not a test
    invented = sorted(cited - suite)
    assert not invented, f"the wiki cites tests that do not exist: {invented}"


def test_agent_instructions_do_not_diverge():
    """AGENTS.md and CLAUDE.md carry the same hand-written guidance.

    Everything below the OPENWIKI marker is maintained by hand and duplicated
    across both files, so it drifts the moment someone edits one and not the
    other. OpenWiki owns the block above the marker and may legitimately differ.
    """
    marker = "<!-- OPENWIKI:END -->"
    here = Path(__file__).parent
    agents = (here / "AGENTS.md").read_text()
    claude = (here / "CLAUDE.md").read_text()
    assert marker in agents and marker in claude, "the OpenWiki marker went missing"
    assert agents.split(marker, 1)[1] == claude.split(marker, 1)[1], (
        "AGENTS.md and CLAUDE.md have diverged below the OpenWiki marker; "
        "copy one over the other"
    )


def test_both_filter_gates_are_documented_where_agents_will_see_them():
    """The two mistakes that corrupt data silently, so both must be findable.

    A filter missing from may_close_postings() makes a run assert that live postings
    are gone. A filter missing from may_use_etags() makes a later run skip a board
    whose postings were never persisted. Agents add filters; they read AGENTS.md.
    """
    here = Path(__file__).parent
    agents = (here / "AGENTS.md").read_text()
    for gate in ("may_close_postings", "may_use_etags"):
        assert gate in agents, f"AGENTS.md must tell a contributor to update {gate}()"
    assert "may_close_postings" in (here / "README.md").read_text()


def test_user_agent_is_header_safe():
    """http.client encodes headers as latin-1; non-ASCII breaks every request."""
    import job_boards
    job_boards.UA.encode("latin-1")
    assert job_boards.UA.isascii()


def test_csv_quoting():
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS)
    w.writeheader()
    w.writerow(BASE | {
        "ats": "ashby", "company": "acme",
        "title": 'Senior Software Engineer, Backend "Core"',
        "location": "Remote – US • EU",
    })
    row = list(csv.DictReader(io.StringIO(buf.getvalue())))[0]
    assert row["title"] == 'Senior Software Engineer, Backend "Core"'
    assert row["location"] == "Remote – US • EU"


def test_a_throttled_board_is_retried_not_dropped():
    """429 and 403 are 4xx, so they used to take the raise-immediately path.

    A real Greenhouse run lost 8 consecutive slugs to that: the board was thrown
    away for the whole run on the first refusal, with no backoff and no second
    chance. 404 must keep raising at once — that one means "not a customer", and
    retrying it would quadruple the cost of every dead slug.
    """
    import job_boards as jb

    original_request, original_sleep = jb._single_request, jb.time.sleep
    jb.time.sleep = lambda seconds: None        # keep the suite offline and instant
    try:
        for status in (429, 403):
            calls = []

            def refuse_twice(u, m, t, e=None, _s=status, _calls=calls):
                _calls.append(1)
                if len(_calls) <= 2:
                    return _s, {}, b""
                return 200, {}, b'{"jobs": []}'

            jb._single_request = refuse_twice
            assert jb.fetch("https://api.lever.co/x") == b'{"jobs": []}', \
                f"a {status} that clears on retry must still return the body"
            assert len(calls) == 3, f"expected 2 refusals then a success, got {len(calls)}"

        calls = []

        def always_404(u, m, t, e=None, _calls=calls):
            _calls.append(1)
            return 404, {}, b""

        jb._single_request = always_404
        raised = False
        try:
            jb.fetch("https://api.lever.co/x")
        except jb.NotFound:
            raised = True
        assert raised and len(calls) == 1, "404 must raise on the first attempt"
    finally:
        jb._single_request, jb.time.sleep = original_request, original_sleep


def test_retry_after_beats_the_exponential_delay_but_is_capped():
    """The server saying "wait 5s" is better information than 2**attempt.

    Capped, because a server asking for an hour would stall a 13,000-board run
    behind one slug; giving up and logging that board is the better trade.
    """
    from job_boards import _retry_delay
    assert _retry_delay("5", 0) == 5.0, "an explicit Retry-After wins"
    assert _retry_delay(None, 3) == 8.0, "no header falls back to 2**attempt"
    assert _retry_delay("garbage", 2) == 4.0, "an unparseable header falls back too"
    assert _retry_delay("3600", 0) == 30.0, "an absurd Retry-After is capped"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"ok ({len(tests)} tests)")
