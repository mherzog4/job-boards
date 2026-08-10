---
type: Testing Guide
title: Job Boards Testing Guide
description: Summarizes the offline self-check suite for job-boards, including parsing, matching, per-ATS normalization, discovery, grep, SQLite lifecycle, output encoding, and environment requirements.
tags: [testing, regression, python, ats]
---

# Testing guide

The test suite is `/test_job_boards.py`, a dependency-free script meant to be run with `uv run test_job_boards.py`. It validates the [job scrape workflow](workflows/job-scrape.md), [board discovery workflow](workflows/board-discovery.md), and [data model](architecture/data-model.md) without relying on live network calls.

## How to run

```bash
uv run test_job_boards.py
python3 test_job_boards.py  # fallback when uv is unavailable and Python is 3.9+
```

Both the application and test scripts declare `requires-python = ">=3.11"` in their `uv` script headers. `/job_boards.py` also uses `from __future__ import annotations` so the module imports under Python 3.9+ when `uv` is unavailable. Prefer `uv run test_job_boards.py`; if `uv` is missing, run `python3 test_job_boards.py` before reporting test health as unverified.

## Coverage map

| Test area | Representative tests | Protected behavior |
|---|---|---|
| Slug parsing | `test_slug_parsing`, `test_cdx_jsonl_parsing` | First path segment extraction, percent-decoding spaces, JSONL handling, case-insensitive dedupe. |
| Discovery validation and HTTP transport | `test_plausible_rejects_archive_noise_but_keeps_real_slugs`, `test_root_prefix_is_only_junk_for_ashby`, `test_board_exists_uses_head_and_maps_404_to_false`, `test_every_posting_api_host_is_pooled`, `test_etags_are_only_trusted_on_an_unfiltered_run`, `test_a_304_skips_the_board`, `test_conditional_request_sends_if_none_match`, `test_header_lookup_is_case_insensitive`, `test_pooled_request_retries_once_on_a_dead_connection`, `test_a_throttled_board_is_retried_not_dropped`, `test_retry_after_beats_the_exponential_delay_but_is_capped` | Shape filter, per-ATS junk-prefix behavior, real slug preservation, `HEAD` validation, 404-to-false mapping, pooled posting API hosts, safe ETag gating, 304 skip behavior, `If-None-Match` emission, case-insensitive header handling, stale pooled-connection retry behavior, throttled-board retry for `429`/`403`, immediate `404` handling, and capped `Retry-After` delay selection. |
| Per-ATS normalization | `test_normalize_ashby`, `test_normalize_greenhouse`, `test_normalize_lever`, `test_normalizers_survive_explicit_nulls`, `test_every_normalizer_fills_the_same_keys`, `test_greenhouse_content_param_only_when_grepping` | Platform payload mapping, null safety, shared row shape, Lever timestamp conversion, Greenhouse content opt-in. |
| Title matching | `test_fuzzy_matching`, `test_fuzzy_does_not_match_generic_one_word_titles`, `test_exact_matching`, `test_empty_query_matches_nothing` | Fuzzy containment in both directions, one-word false-positive guard, exact matching, empty-query safety. |
| Freshness filters | `test_parse_duration`, `test_published_within_boundaries`, `test_since_filters_by_publish_date`, `test_known_keys_reads_existing_and_legacy_databases` | Duration parsing, exclusion of stale or unparseable publish dates, `--since` scan filtering, and database-backed `(ats, id)` lookup for `--new-only`. |
| Board scanning | `test_scan_board_filters_and_flattens`, `test_no_filters_returns_every_listed_job`, `test_invalid_payload_raises_rather_than_returning_nothing`, `test_failed_boards_round_trip_through_boards_from`, `test_a_failed_board_is_written_to_the_failure_file_and_a_clean_run_clears_it`, `test_a_board_subset_still_may_not_widen_what_a_run_concludes` | Adapter dispatch, listed-only filtering, remote filtering, row shape, all-job mode, loud failure on API shape changes, failed-board retry files, stale failure cleanup, and board-subset lifecycle gates. |
| Grep matching | `test_plain_text_strips_markup_and_entities`, `test_fragments_give_context_and_dedupe`, `test_grep_filters_on_description_and_records_context`, `test_grep_matches_the_title_not_only_the_description`, `test_grep_does_not_match_across_the_title_description_seam` | HTML stripping, context windows, dedupe, word-boundary false-positive behavior, title matches, and no cross-field seam matches. |
| SQLite history | `test_db_upsert_preserves_history`, `test_db_keeps_grep_context_from_earlier_runs`, `test_migrates_a_single_ats_database`, `test_migrates_a_database_created_before_closed_at`, `test_etags_round_trip_through_the_database`, `test_unfiltered_run_closes_vanished_postings`, `test_filtered_run_never_closes_anything`, `test_closing_is_scoped_to_boards_actually_scanned`, `test_only_an_unfiltered_run_may_close_postings`, `test_db_skips_rows_without_an_id` | `first_seen` preservation, `last_seen` refresh, matched-context retention, pre-multi-ATS and pre-`closed_at` migrations, per-board ETag storage, closed-posting lifecycle, filter-aware closure eligibility, coverage scoping, id requirement. |
| Documentation drift | `test_every_cli_flag_is_documented_in_the_readme`, `test_agent_instructions_do_not_diverge`, `test_both_filter_gates_are_documented_where_agents_will_see_them` | CLI flag visibility in README, duplicated agent-instruction consistency below the OpenWiki marker, and `may_close_postings()` guidance where contributors will see it. |
| Output and headers | `test_sort_recent_puts_the_newest_first`, `test_sort_recent_is_deterministic_within_a_timestamp`, `test_user_agent_is_header_safe`, `test_csv_quoting` | Board-order default, newest-first recent sorting with undated rows last, deterministic ties, ASCII-safe user agent, CSV escaping, Unicode punctuation round trips. |

## Test style

Tests monkeypatch `job_boards.fetch` directly for scan and validation scenarios rather than introducing a mocking dependency. SQLite behavior uses temporary directories and real `sqlite3` connections. This keeps the repository aligned with its no-dependency architecture.

## Change guidance

- For CLI filter changes, add tests that call `matches()` or `scan_board()` directly and include negative cases. If the filter narrows observed coverage, add it to `may_close_postings()` and extend `test_only_an_unfiltered_run_may_close_postings`; if it also makes 304 skips unsafe, add it to `may_use_etags()` and extend `test_etags_are_only_trusted_on_an_unfiltered_run`. Every `argparse` `--flag` must also appear in `/README.md`, because `test_every_cli_flag_is_documented_in_the_readme` treats undocumented flags as docs drift.
- For duplicated agent guidance, keep `AGENTS.md` and `CLAUDE.md` identical below `<!-- OPENWIKI:END -->`; the generated OpenWiki-owned block above the marker may differ.
- For board discovery or HTTP transport changes, keep archive fixtures offline and test parsing, filtering, pooling, header casing, throttling backoff, `Retry-After` caps, and retry boundaries rather than live services.
- For new ATS platforms, add normalizer tests and assert every adapter fills the same public `FIELDS` used by the [data model](architecture/data-model.md).
- For database changes, test both first insert and subsequent update behavior. Schema changes should include migration from older on-disk databases; lifecycle changes should include filtered and unfiltered runs.
- For output changes, assert `FIELDS` and representative CSV/JSON behavior so downstream consumers are not surprised.

The [operations runbook](operations/runbook.md) should be updated whenever the required verification command or runtime environment changes.
