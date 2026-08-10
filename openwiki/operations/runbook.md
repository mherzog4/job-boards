---
type: Runbook
title: Job Boards Operations Runbook
description: Practical operating notes for running the multi-ATS jobs scraper safely, managing generated outputs, handling archive/API failures, and maintaining OpenWiki automation.
tags: [operations, runbook, scraping, automation]
---

# Operations runbook

This runbook connects safe operation of the scraper to the [architecture overview](../architecture/overview.md), [board discovery workflow](../workflows/board-discovery.md), [job scrape workflow](../workflows/job-scrape.md), [data model](../architecture/data-model.md), and [testing guide](../testing.md).

## Good-citizen scraping

Before network runs, set `JOB_SCRAPER_CONTACT`:

```bash
export JOB_SCRAPER_CONTACT="you@example.com"
```

`/job_boards.py` includes the value in the `User-Agent` for both archive discovery and posting API calls. The old `ASHBY_SCRAPER_CONTACT` name is still accepted as a fallback for compatibility. The code defaults to a safe ASCII fallback and strips non-ASCII characters so a missing or unusual contact value does not break all requests.

Keep concurrency modest. The script defaults to 8 workers for API calls and board validation across Ashby, Greenhouse, and Lever. Common Crawl paging is sequentially throttled to one request per second in the fallback path. When a posting API returns `429` or `403`, the shared HTTP client used by the [job scrape workflow](../workflows/job-scrape.md) backs off instead of dropping the board immediately; it honours seconds-form `Retry-After` values when present and caps them at 30 seconds.

## Routine runs

Use these patterns from the README and source:

```bash
# Fresh full scrape and board refresh across all supported platforms.
uv run job_boards.py --refresh-boards --all

# Later full scrape using cached boards.
uv run job_boards.py --all

# Daily freshness-oriented run: recent board discovery plus recent postings.
uv run job_boards.py --refresh-recent --all --since 7d
uv run job_boards.py --all --new-only

# Platform-specific or targeted searches.
uv run job_boards.py --ats greenhouse --title "swe"
uv run job_boards.py --ats ashby,lever --all
uv run job_boards.py --title "software engineer"
uv run job_boards.py --grep '\brust\b|\bgolang\b'

# Offline regression suite.
uv run test_job_boards.py
python3 test_job_boards.py  # fallback when uv is unavailable and Python is 3.9+
```

The README recommends monthly full `--refresh-boards` runs because board discovery depends on archive coverage and has measured lag. For fresher daily discovery, `--refresh-recent` reads the last 30 days of Wayback captures and urlscan.io public scans; it is additive to the seed and cache, not a replacement for periodic full refresh. Jobs themselves are live on every scrape because the scan phase reads each platform's posting API directly, while `--since` and `--new-only` narrow which live postings are written.

## Persistence and scheduling

Schedule unfiltered `--all` runs if you want the SQLite database to become a fill-rate or disappearance signal. The [data model](../architecture/data-model.md) only stamps `closed_at` after an unfiltered run has covered a board and omitted a previously seen posting. Filtered searches, including `--since` and `--new-only`, update matching postings but cannot prove that non-matching postings disappeared.

For low-transfer freshness checks, repeat `uv run job_boards.py --all --new-only` with the database enabled. The [job scrape workflow](../workflows/job-scrape.md) stores per-board ETags during safe full persisted fetches and sends them back on later `--all --new-only` runs, so unchanged boards return 304 and are counted as `unchanged` without downloading posting bodies. Do not combine this mode with `--no-db`; it needs both known posting keys and stored board ETags.

Useful database questions from the README include new postings in the last day, platform comparison through the `ats` column, currently open roles (`closed_at IS NULL`), recently closed roles, and companies filling roles quickly.

## Generated output hygiene

`.gitignore` intentionally ignores:

- `*.csv`
- `*.json`, except `/boards.seed.json`
- `*.db` and `*.db-journal`
- Python caches

This is more than cleanup. A full generated `boards.json` is effectively three vendors' discovered customer lists, so it should stay local. Generated scrape outputs in the working tree are operational artifacts, not source evidence for code changes.

## Failure modes and responses

| Symptom | Likely cause | Response |
|---|---|---|
| `Common Crawl returned 503` | Common Crawl is rate-limiting or protecting itself from client pressure. | Wait before retrying; do not increase request rate. |
| Common Crawl 502 or 504 | Common Crawl index backend overloaded. | Retry later; Wayback is the preferred default. |
| Board discovery fails entirely | Wayback and Common Crawl unreachable. | Use the committed seed or existing cache; discovery is optional for a fresh clone. |
| Many board scan errors | A platform API shape or network behavior may have changed. | Inspect stderr, run tests, and verify `scan_board()` still receives the jobs list shape expected by the platform adapter. `429` or `403` should already have backed off inside `fetch()` before surfacing as persistent failures. |
| Greenhouse `--grep` is unexpectedly slow or large | Greenhouse descriptions require `?content=true`, which is much larger than normal list payloads. | Scope with `--ats`, `--limit`, or a smaller board set unless a full Greenhouse description sweep is intended. |
| `--new-only` exits before scanning | It needs SQLite history and was combined with `--no-db`. | Keep the database enabled or drop `--new-only`. |
| `--since` returns fewer rows than expected | Missing, malformed, or older `publishedAt` values are excluded because freshness must be provable. | Use a wider duration or an unfiltered `--all` run when completeness matters. |
| `uv` is missing from `PATH` | Minimal local or agent environment. | Run `python3 test_job_boards.py`; the source keeps the offline suite working on Python 3.9+. |

## OpenWiki automation

The repository includes two OpenWiki-related workflows:

| Workflow | Trigger | Behavior |
|---|---|---|
| `/.github/workflows/openwiki-drift-check.yml` | Pull requests or `main` pushes touching `job_boards.py` or `test_job_boards.py` | Fails the PR, or marks the pushed commit red, if tracked source changed but `openwiki/` did not. It does not run OpenWiki or require provider credentials. |
| `/.github/workflows/openwiki-update.yml` | Manual `workflow_dispatch` | Runs `openwiki code --update --print` in CI and opens a pull request, but needs an `OPENROUTER_API_KEY` secret. |

The drift check exists because provider credentials for regeneration should not be required on every PR. The README explains the intended loop: a human runs `openwiki --update`, commits `openwiki`, `AGENTS.md`, and `CLAUDE.md`, and only uses `[skip-wiki]` in the PR title for source changes that genuinely do not affect generated docs. Direct pushes to `main` have no PR title escape hatch, so the workflow compares `before..after` and can only mark the commit red. The check runs with `contents: read`, checks out the PR head SHA rather than a branch name, and keeps the PR title escape hatch out of the shell.

The manual update workflow checks out the repository, sets up Node.js, installs `uv`, installs `openwiki@0.2.3` plus Mermaid validation packages, runs OpenWiki with OpenRouter settings, and opens a pull request containing `openwiki`, `AGENTS.md`, `CLAUDE.md`, and the workflow file. Actions in both workflows are pinned to commit SHAs. Root `AGENTS.md` and `CLAUDE.md` direct future agents to start with `openwiki/quickstart.md` and document the same no-uv test fallback as the [testing guide](../testing.md). Treat `/openwiki/INSTRUCTIONS.md` as user-authored scope metadata and do not rewrite it during routine updates.

## Change guidance

If you change external-service behavior, update [board discovery](../workflows/board-discovery.md) or [job scraping](../workflows/job-scrape.md) with rate-limit and error-handling implications. If you change persistence or scheduling semantics, update the [data model](../architecture/data-model.md) and tests together. If you change workflow triggers, credential assumptions, or OpenWiki CI behavior, update this runbook and the README together.
