---
type: Guide
title: Job Boards OpenWiki Quickstart
description: Entry point for the job-boards code wiki, covering the multi-ATS scraper's purpose, repository layout, primary workflows, operations, testing, and where engineers should start.
tags: [openwiki, quickstart, job-boards]
---

# Job Boards OpenWiki quickstart

This repository is a dependency-free Python CLI for pulling public job postings from Ashby, Greenhouse, and Lever boards. Each ATS exposes an unauthenticated per-company posting API with no global search endpoint, so the tool maintains a per-platform board-slug list and then fans out across those boards to produce CSV, JSON, and optional SQLite outputs. The README is the user-facing product narrative; this wiki is the engineer-facing map for changing and operating the code.

Start with the [architecture overview](architecture/overview.md) for the end-to-end runtime, then use the workflow pages when changing board discovery or scrape behavior.

## What the tool does

The main script, `/job_boards.py`, supports these core use cases:

- Discover Ashby, Greenhouse, and Lever board slugs via Internet Archive CDX, with Common Crawl as fallback, then validate candidates by `HEAD`ing each platform's posting API.
- Reuse `boards.json` when present, or the curated per-platform `/boards.seed.json` seed in a fresh clone.
- Scan boards concurrently, keeping only postings that pass `--all`, `--ats`, `--title`, `--grep`, `--remote`, and match-mode filters.
- Normalize platform-specific payloads into one row shape with an `ats` column, then write `job-boards.csv`, `job-boards.json`, and, unless `--no-db` is set, an accumulating SQLite database.
- Track posting appearance and disappearance across runs using `first_seen`, `last_seen`, and `closed_at`.

## First commands

The repository is designed to run through `uv` using script metadata in `/job_boards.py` and `/test_job_boards.py`; both declare Python `>=3.11` and no dependencies. The current source also imports cleanly on Python 3.9+ when `uv` is unavailable, so the offline test suite has a bare-`python3` fallback.

```bash
export JOB_SCRAPER_CONTACT="you@example.com"
uv run job_boards.py --refresh-boards --all
uv run job_boards.py --ats greenhouse --title "swe"
uv run job_boards.py --ats ashby,lever --all
uv run job_boards.py --title "software engineer" --match exact
uv run job_boards.py --grep '\brust\b|\bgolang\b'
uv run test_job_boards.py
python3 test_job_boards.py  # fallback when uv is unavailable and Python is 3.9+
```

Set `JOB_SCRAPER_CONTACT` before real network runs so archive operators and API owners can identify the traffic source. `/job_boards.py` still accepts the old `ASHBY_SCRAPER_CONTACT` environment variable as a compatibility fallback, then strips non-ASCII characters because HTTP headers must be latin-1 safe.

## Documentation map

- [Architecture overview](architecture/overview.md) explains how `main()`, source adapters, board loading, scraping, outputs, and persistence fit together.
- [Board discovery workflow](workflows/board-discovery.md) explains Wayback/Common Crawl candidate harvesting, platform-specific slug filtering, `HEAD` validation, cache semantics, and why `boards.json` is ignored.
- [Job scrape workflow](workflows/job-scrape.md) explains CLI filters, title matching, title-or-description grep, remote filtering, per-ATS normalization, concurrency, output writing, and failure handling.
- [Data model](architecture/data-model.md) documents output row fields, SQLite schema, composite upsert key, and posting lifecycle semantics.
- [Operations runbook](operations/runbook.md) covers scheduled usage, freshness expectations, privacy/git hygiene, rate-limit guidance, and the OpenWiki drift-check workflow.
- [Testing guide](testing.md) summarizes the offline regression suite and verification caveats.
- [Source map](source-map.md) maps repository files to the concepts above.

## Recent evolution from git history

Recent commits show a progression from a simple public Ashby board scraper into a multi-platform data collection tool:

- Initial implementation added the dependency-free Ashby CLI, seed boards, README, and offline tests.
- The committed board list was split into `/boards.seed.json` while generated `boards.json` became ignored output, preventing full crawls from publishing customer lists.
- `--grep` was added to search descriptions without retaining full descriptions in output rows; it now also searches titles separately so matches cannot span the title/description seam.
- SQLite accumulation was added so repeated scrapes preserve `first_seen` and `last_seen`.
- Discovery moved to Wayback-first with `HEAD` validation because Common Crawl was less reliable and narrower.
- `--all` was added and board refreshes were changed to union archive results with seed and previous cache.
- Data-model work added disappearance tracking through `closed_at`, then fixed migration ordering so databases created before that column can be upgraded safely.
- The latest source rename generalized the scraper to Ashby, Greenhouse, and Lever through `SOURCES`, per-ATS normalizers, `--ats`, an `ats` output column, and a `(ats, id)` SQLite key.

## Change checklist for future agents

1. Read the page matching the area you are changing, then inspect the referenced source functions in `/job_boards.py` and tests in `/test_job_boards.py`.
2. Keep generated outputs (`*.csv`, `*.json`, `*.db`, `boards.json`) out of documentation examples except as outputs; `.gitignore` intentionally denies them.
3. Preserve the operational contract that public scraping is bounded, identifiable, and low-impact.
4. Run `uv run test_job_boards.py` first. If `uv` is unavailable, run `python3 test_job_boards.py`; the source keeps a no-uv fallback working on Python 3.9+.
5. When changing data retention or lifecycle semantics, update the [data model](architecture/data-model.md), [operations runbook](operations/runbook.md), and tests together.
6. When adding another ATS, update the [job scrape workflow](workflows/job-scrape.md), [board discovery workflow](workflows/board-discovery.md), [data model](architecture/data-model.md), and [testing guide](testing.md) so adapter behavior, seed/cache shape, and output compatibility stay explicit.

## Backlog

- Packaging and distribution: source anchor `/job_boards.py` script metadata. Deferred because the repository currently presents itself as a single `uv run` script, not an installable package.
- Live endpoint measurements: source anchor `/README.md` facts table. Deferred because the wiki captures the current documented measurements but should not refresh external metrics during this update run.
