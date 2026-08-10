# job-boards

Pull every public job posting from every **Ashby, Greenhouse and Lever** job board.
No API key, no account, no dependencies.

All three publish an unauthenticated posting API that is per-company, keyed by a board
slug, with no global search endpoint. This finds the boards — **13,146** of them across
the three platforms — then fans out across all of them. **~308,000 live postings.**

> **Engineers and coding agents:** the [`openwiki/`](openwiki/quickstart.md) wiki is the
> map of the code — architecture, workflows, data model, runbook. Agents should start
> with [For coding agents](#for-coding-agents-llms) below.

## Quick start

No dataset is bundled; you generate your own. The only prerequisite is
[uv](https://docs.astral.sh/uv/) — no `pip install`, no venv, no dependencies, and it
fetches its own Python.

```bash
git clone https://github.com/mherzog4/job-boards.git
cd job-boards

# Identify your traffic. Archive operators ask clients to do this, and it puts
# your address on your requests rather than someone else's.
export JOB_SCRAPER_CONTACT="you@example.com"

# Find every board on every platform, then pull every posting from all of them.
uv run job_boards.py --refresh-boards --all
```

That is the whole thing. You end up with:

| file | what it is |
|---|---|
| `job-boards.csv` | every posting, UTF-8 BOM so Excel renders `–`/`•` correctly |
| `job-boards.json` | the same rows |
| `job-boards.db` | SQLite, accumulating across runs, keyed on `(ats, id)` |
| `boards.json` | the discovered slugs per platform, cached so later runs skip discovery |

Every row carries an `ats` column, so one CSV and one database cover all three platforms
and you can slice by platform or ignore it entirely.

Later runs reuse `boards.json`, so a re-scrape is just `uv run job_boards.py --all`.
Re-run `--refresh-boards` monthly, or `--refresh-recent` daily — see
[How recent is the data](#how-recent-is-the-data--and-how-to-make-it-fresher), which also
covers `--since` for a fresher dataset.

### Narrower searches

```bash
uv run job_boards.py --ats greenhouse --title "swe"  # one platform
uv run job_boards.py --ats ashby,lever --all         # a subset
uv run job_boards.py --title "software engineer"
uv run job_boards.py --title "software engineer" --match exact
uv run job_boards.py --title "product designer" --remote --limit 200
uv run job_boards.py --grep '\brust\b|\bgolang\b'    # search titles + descriptions
uv run job_boards.py --all --since 7d                # only the last week's postings
uv run job_boards.py --all --new-only                # only what the db has not seen
uv run test_job_boards.py                            # offline self-check
```

## How complete is this?

Measured by a real `--refresh-boards --all`, not estimated:

| platform | boards live | companies hiring | jobs |
|---|---|---|---|
| Ashby | 3,617 | 3,289 | 54,591 |
| Greenhouse | 6,797 | 5,660 | 180,915 |
| Lever | 2,718 | 2,113 | 72,594 |
| **total** | **13,132** | **11,062** | **308,100** |

That is the `--refresh-boards` figure; [`--refresh-recent`](#closing-the-discovery-gap---refresh-recent)
has since taken the cached board list to **13,146**, which is the count the runtimes below
are measured over.

A full `--refresh-boards --all` took **26 minutes** measured before connection pooling —
most of it discovery, which later runs skip. The scrape half of that is now 41% faster
(see [Performance](#performance)); the discovery half has not been re-timed since. Note the board/company gap: ~2,000 boards are real customers with nothing
currently listed, which is expected and not an error.

So it gets **every listed job on every board it knows about**. The honest limit is the
board list, not the scraping — and no vendor publishes a list of its customers to
check against, so completeness cannot be proven, only bounded.

Where boards can still be missed:

- **Discovery only sees what the Internet Archive captured.** A board that exists but was
  never crawled is invisible. This is real, not theoretical: `newtonx` was live but absent
  from 191k archived URLs. To stop that from costing you anything, `--refresh-boards`
  unions its results with `boards.seed.json` and the previous `boards.json`, so a refresh
  never loses a board an earlier run knew about.
- **New customers of any of these platforms** appear before the archive notices them — a median of 48 days
  before, measured above. Re-run `--refresh-boards` monthly, or add the slug by hand.
- **The shape filter** drops candidates that cannot be slugs. Sampling 150 of the 2,463 it
  rejected on Ashby turned up zero real boards, so this looks safe, but it is a sample.

If you find a board this misses, add it to `boards.seed.json` and it is permanent.

## How recent is the data — and how to make it fresher

Every run hits each platform's API directly, so the *data* is live. But the median
posting in a full pull is **62 days old**, because companies leave requisitions listed:

| ats | jobs | median age | >1yr | >3yr | oldest |
|---|---|---|---|---|---|
| ashby | 54,591 | 48d | 3.9% | 0.3% | 2,484d |
| greenhouse | 180,915 | 60d | 13.0% | 1.8% | 2,776d |
| lever | 72,594 | **97d** | **26.2%** | **14.1%** | **6,078d** |
| **all** | 308,100 | **62d** | 15.6% | 5.1% | — |

That tail is real upstream data, not a parsing bug. Palantir's Lever board carries a
"Forward Deployed Software Engineer" with `createdAt` **2009-12-05** — verified against
the raw API. Lever boards accumulate the most evergreen postings by a wide margin.

**You cannot lower the age of what exists, only choose what to collect.** Two flags do
that, and they catch different things:

```bash
uv run job_boards.py --all --since 1d --sort recent   # today's postings, newest first
uv run job_boards.py --all --since 7d     # published in the last week
uv run job_boards.py --all --new-only     # never seen by the database before
uv run job_boards.py --all --since 30d --new-only
```

**Today's jobs, today.** `--since 1d` over all 13,146 boards takes about **3m51s** and
returned **5,980 postings** on a real run — 133 of them published within the previous
hour, the freshest **6 minutes old**. Pair it with `--sort recent` so the newest are at
the top; the default `--sort board` groups by platform and company, which buries them.

| posted within | jobs |
|---|---|
| 1 hour | 133 |
| 3 hours | 686 |
| 6 hours | 1,909 |
| 12 hours | 4,441 |
| 24 hours | 5,980 |

None of the three APIs support server-side date filtering — `updated_after` and friends
are silently ignored, verified against all three — so every board is fetched and the
window is applied locally. ~4 minutes is therefore the floor for a full sweep, and the
only way to see a posting sooner is to run more often.

`--since` accepts `7d`, `2w`, `3m`, `1y`, or a bare number of days:

| `--since` | jobs | share | median age |
|---|---|---|---|
| 7d | 35,490 | 11.5% | **4.0d** |
| 14d | 58,294 | 18.9% | 6.0d |
| 30d | 95,780 | 31.1% | 12.0d |
| 90d | 183,228 | 59.5% | 27.0d |
| (none) | 308,100 | 100% | 62.0d |

`--new-only` catches what `--since` cannot: a 200-day-old requisition that only appeared
on a board today, or one on a board you only just discovered. It compares against the
`(ats, id)` keys already in the database, so it needs the database and errors with
`--no-db`.

**Neither ever marks a posting closed.** A run that filtered did not see what it filtered
out, so it cannot conclude those postings are gone — the same rule that already applies to
`--title` and `--grep`. Without it, one `--all --since 7d` would close everything older
than a week.

**They also differ in what they cost the platforms.** `--all --new-only` is the one
combination that may send `If-None-Match`, so an unchanged board transfers nothing at all
([conditional requests](#conditional-requests-on---all---new-only)). Adding `--since`
turns that off — `may_use_etags()` requires a run that is unfiltered apart from
`--new-only` — so every `--since` run re-downloads all 13,146 boards in full. If you are
scraping on a schedule and want the cheap pass, run `--all --new-only` and apply the date
window to the database afterwards:

```sql
SELECT company, title, jobUrl FROM jobs
WHERE first_seen > datetime('now', '-1 day')
ORDER BY publishedAt DESC;
```

**Board discovery lags by ~48 days.** Separately from posting age: a company that adopts
any of these platforms is invisible until the Internet Archive crawls its board. Comparing
each board's first archive capture against its oldest surviving posting:

| percentile | lag before the archive first saw the board |
|---|---|
| p25 | 20 days |
| **p50** | **48 days** |
| p75 | 110 days |
| p90 | 257 days |

The archive is actively crawling — 348 of the 3,617 Ashby boards were captured within
the last week, some the same day — but a brand-new customer typically waits about seven weeks to
become discoverable.

**Why the lag matters less than it looks.** A board only has to be discovered once; after
that every scrape reads live data from it. The lag is a one-time cost per company, not a
staleness tax on jobs, and it only applies to companies that adopted their ATS in
the last couple of months. For the other ~13,000 it is already paid.

### Closing the discovery gap: `--refresh-recent`

The archive is thorough but slow. urlscan.io indexes scans people ran *today*, so it
surfaces boards the archive has not reached yet — a measured run added 14 boards a full
Wayback crawl had missed, including `headway`, `lab37` and `eltropyinc`.

| | `--refresh-boards` | `--refresh-recent` |
|---|---|---|
| Wayback | full crawl, 2.9M URLs | last 30 days only, ~17k URLs |
| urlscan.io | — | recent public scans |
| runtime | ~26 min | **~4 min** | (both pre-pooling)
| cadence | monthly | daily |

```bash
uv run job_boards.py --refresh-recent --all --since 7d   # a daily fresh-jobs run
```

It is purely additive — a measured run went 13,132 → 13,146 boards and lost none, because
discovery unions with the seed and the previous cache. urlscan's anonymous API allows 30
searches/minute and this makes four, so no key is needed.

If you need one specific new company immediately, skip discovery entirely — add its slug
to `boards.seed.json` and it is permanent from the next run.

## Match modes

`--match fuzzy` (default) — either string contains the other, so it works in both
directions. A short query finds longer titles, and a long query still finds the short
title inside it:

| `--title` | matches |
|---|---|
| `software engineer` | Software Engineer, Senior Software Engineer, Backend, SOFTWARE ENGINEER II |
| `senior software engineer, backend` | Senior Software Engineer, Backend, **and** Software Engineer |

The reverse direction requires the title to be **at least two words**. Without that guard,
querying `senior software engineer` also matches every job titled just `Engineer`,
`Software`, or `Senior`. An empty `--title` matches nothing rather than everything.

`--match exact` — the whole title must equal the query (case- and whitespace-insensitive).
`software engineer` matches only `Software Engineer`.

## Searching descriptions with `--grep`

Titles are a weak filter — they miss "Software Development Engineer" and tell you nothing
about the stack. `--grep` runs a case-insensitive regex against the job title *and*
description and puts the surrounding context in a `matched` column, so a hit can be
judged without opening the posting.

```bash
uv run job_boards.py --grep '\brust\b|\bgolang\b'          # title and description
uv run job_boards.py --title engineer --grep '\bkubernetes\b'  # both must match
```

The two fields are searched separately, not concatenated, so a pattern cannot match
across the seam between them and report a hit that is in neither.

`--title` and `--grep` are ANDed. Giving `--grep` alone drops the title filter entirely
rather than silently ANDing the default `software engineer` onto it.

**Use `\b`.** Without word boundaries a pattern matches inside longer words, and job
descriptions are full of boilerplate that will catch you:

| pattern | jobs matched (26 Ashby boards) |
|---|---|
| `rust\|golang` | **1350** — `rust` matches "t**rust**", which is in nearly every description |
| `\brust\b\|\bgolang\b` | **72** |

That is an 18x false-positive rate with no visible symptom, so the script warns on stderr
when a `--grep` pattern contains no `\b`.

Only matched fragments are kept, never whole descriptions.

**Greenhouse costs ~26x more with `--grep`.** Ashby and Lever return descriptions whether
or not you want them, so searching them is free. Greenhouse omits descriptions from its
list endpoint and only returns them for `?content=true`, which takes one board from 25KB
to 653KB gzipped — measured on `stripe`. The script requests content only when `--grep`
is set, and warns when it does. A `--grep` sweep of every Greenhouse board is multiple
gigabytes; scope it with `--ats` or `--limit` unless you mean it.

Fuzzy is much wider than exact: on a 26-board Ashby sample, `software engineer` returned
**268** jobs fuzzy against **2** exact, because most companies prefix with Senior/Staff.

## Boards that fail, and retrying just those

A board can error rather than answer: a throttle, a timeout, a connection dropped
mid-run. The `N err` figure in the progress line counts those, and each one prints to
stderr — but a count is not a recovery plan, and re-running a 13,000-board sweep to
recover eight of them is absurd.

So the failures are written to `<out>.failed.json`, in the same shape as `boards.json`:

```json
{ "greenhouse": ["yotpo", "yieldmo", "yld"] }
```

`--boards-from` reads that shape, so the retry is the same command with one flag
swapped:

```bash
uv run job_boards.py --ats greenhouse --all         # ... 8 boards error
uv run job_boards.py --all --boards-from job-boards.failed.json   # just those 8
```

The file is written only when something failed, and deleted when nothing did, so a
stale one from an earlier run cannot be mistaken for this run's result. 404s are not
included — a dead slug is an expected answer rather than a failure, and it is already
pruned from `boards.json` automatically.

`--boards-from` restricts which *boards* are scanned, not which postings are kept, so
it does not touch either gate: closing is already scoped to the boards a run actually
visited, the same rule that keeps `--limit 10` from closing postings at companies it
never opened. It does disable the 404 self-prune, since a caller-supplied subset must
not be written back as though it were the whole discovered list.

## The database

Every run also upserts into `job-boards.db` (SQLite, stdlib, no setup). The CSV is a
snapshot of one query; the database accumulates across runs and is what lets you ask
questions a snapshot can't answer.

Rows are keyed on **`(ats, id)`**, with `first_seen` preserved and `last_seen` refreshed.
The composite key is deliberate: Greenhouse posting ids are integers while Ashby and Lever
use UUIDs, so a bare `id` risks a collision that would silently overwrite one platform's
posting with another's. Everything else is overwritten each run, since titles and
locations do get edited in place on live postings. The exception is `matched`: a later
title-only run won't blank out `--grep` context an earlier search found.

Upgrading an older single-platform database is automatic — the table is rebuilt with the
new key and every existing row is labelled `ats = 'ashby'`, preserving `first_seen` and
`closed_at`.

```bash
uv run job_boards.py --title "software engineer"     # writes job-boards.db
uv run job_boards.py --db ~/jobs.db                  # somewhere else
uv run job_boards.py --no-db                         # CSV/JSON only
uv run job_boards.py --out weekly                    # weekly.csv/.json instead
```

`--out` renames the CSV and JSON outputs; `--concurrency` (default 8) caps parallel
requests and is the one knob you should leave alone — see
[Being a good citizen](#being-a-good-citizen).

The run summary reports `N new, M already seen`, so a scheduled scrape tells you what
changed without diffing anything.

```sql
-- postings that showed up in the last day
SELECT ats, company, title, jobUrl FROM jobs
WHERE first_seen > datetime('now', '-1 day');

-- how the platforms compare
SELECT ats, COUNT(*) jobs, COUNT(DISTINCT company) companies FROM jobs GROUP BY ats;

-- postings that have since disappeared (see the section below)
SELECT company, title, first_seen, closed_at FROM jobs
WHERE closed_at IS NOT NULL ORDER BY closed_at DESC;

-- who is hiring hardest
SELECT company, COUNT(*) n FROM jobs GROUP BY company ORDER BY n DESC LIMIT 10;

-- roles whose description mentioned your --grep term, with the context
SELECT company, title, matched FROM jobs WHERE matched != '';
```

### Detecting when a posting disappears

`last_seen` alone can't tell you a job is gone, because it only advances when a run's
filters happen to match. On a `--title` run, "filled last week" and "didn't match this
time" look identical.

So closing a posting is reserved for **unfiltered `--all` runs**, which are the only ones
that saw everything. After such a run, any posting on a scanned board that wasn't seen
gets a `closed_at` stamp; anything reposted has it cleared. Filtered runs never touch it,
and the closing is scoped to boards actually scanned, so `--limit` can't close jobs at
companies it skipped.

```
54581 jobs -> ... (54581 new, 0 already seen, 0 closed)
54578 jobs -> ... (0 new, 54578 already seen, 3 closed)
```

Schedule `--all` (daily or weekly) and the database becomes a real fill-rate signal:

```sql
-- how long postings stay open
SELECT AVG(julianday(closed_at) - julianday(first_seen)) AS avg_days_open
FROM jobs WHERE closed_at IS NOT NULL;

-- currently open roles only
SELECT company, title, jobUrl FROM jobs WHERE closed_at IS NULL;

-- companies filling roles fastest
SELECT company, COUNT(*) filled,
       ROUND(AVG(julianday(closed_at) - julianday(first_seen))) avg_days
FROM jobs WHERE closed_at IS NOT NULL
GROUP BY company HAVING filled >= 5 ORDER BY avg_days LIMIT 20;
```

Until you've run `--all` at least twice, `closed_at` is null everywhere — one sweep
establishes the baseline, the next detects what left.

## How it works

Every supported ATS publishes a **per-company** API keyed by a board slug, with no global
search endpoint. So this is two phases, run per platform.

| platform | posting API | archive domain(s) |
|---|---|---|
| Ashby | `api.ashbyhq.com/posting-api/job-board/{slug}` | `jobs.ashbyhq.com` |
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{slug}/jobs` | `boards.greenhouse.io`, `job-boards.greenhouse.io` |
| Lever | `api.lever.co/v0/postings/{slug}?mode=json` | `jobs.lever.co` |

**Phase 1 — discover slugs.** Query the **Wayback Machine's** CDX index for everything
archived under each platform's domains, take the first path segment of each URL as a
candidate slug, drop the ones that can't be slugs, then validate the rest against that
platform's posting API. Cached to `boards.json` and skipped on later runs unless
`--refresh-boards`.

Measured funnels:

```
ashby       191,117 archived URLs  ->  7,463 candidates  ->  3,617 live boards
greenhouse  1,348,314              -> 14,430             ->  6,797 live boards
lever       1,302,426              ->  8,681             ->  2,718 live boards
```

Three details make that work:

- **The Wayback Machine, not Common Crawl.** Common Crawl was the obvious index and it is
  the wrong default: narrower coverage (~3,400 estimated) and it sheds requests under load
  for hours at a time. The Internet Archive returned 191k URLs in 34 seconds. Common Crawl
  is still there as an automatic fallback.
- **A shape filter before validating.** Archived "path segments" include tracking blobs,
  compensation strings like `$10.2K`, and JS fragments. On Ashby that cuts 7,463
  candidates to 5,000 probes; sampling 150 of the rejects found zero real boards.
- **HEAD, not GET.** A live board returns 200 with a zero-length body under HEAD, so
  validating 5,000 candidates costs nothing. GET would have downloaded ~220KB per live
  board — most of a gigabyte purely to learn which slugs are real.

**Phase 2 — fetch + filter.** Thread pool of 8 over every `(platform, slug)` pair. Each
platform's response is run through a normaliser that maps it onto one row shape, so
filters, CSV and SQLite stay platform-agnostic. Because Phase 1 already validated, a
healthy run sees zero 404s; any that do appear get pruned from `boards.json`.

### Normalisation, and two traps

| row field | Ashby | Greenhouse | Lever |
|---|---|---|---|
| `title` | `title` | `title` | **`text`** |
| `location` | `location` | `location.name` | `categories.location` |
| `employmentType` | `employmentType` | — | `categories.commitment` |
| `isRemote` | `isRemote` | inferred from location | `workplaceType == "remote"` |
| `publishedAt` | `publishedAt` ISO | `first_published` ISO | **`createdAt` epoch-ms** |
| `jobUrl` | `jobUrl` | `absolute_url` | `hostedUrl` |
| description | always present | opt-in, 26x bytes | always present |

The two bolded cells are the ones that fail quietly. Reading `title` on Lever yields an
empty column rather than an error, and treating its `createdAt` as ISO makes every Lever
posting sort wrongly against the other two. Both are pinned by tests.

Greenhouse exposes no remote flag on this endpoint, so `isRemote` is inferred from the
location label containing "remote" — weaker than the other two, and worth knowing before
you trust `--remote` there.

## Facts verified against live endpoints (2026-07-27)

| Fact | Note |
|---|---|
| All three posting APIs | 200, no auth, no account |
| Invalid slug, all three | 404 — this is the validator |
| `HEAD`, all three | 200 with a 0-byte body — free validation |
| Ashby slugs may contain spaces | `A1%20Garage%20Door%20Service` → 200 |
| gzip, Ashby | 1.73MB → 220KB, **8x** |
| gzip, Greenhouse | 317KB → 25KB, **12x** |
| Greenhouse `?content=true` | 25KB → 653KB, **26x** — descriptions are opt-in |
| Lever payload | a bare JSON array, not `{"jobs": [...]}` |
| Wayback CDX | 191k (ashby) + 1.35M (greenhouse) + 1.30M (lever) URLs |
| Posting data | live, uncached — 946 jobs published the same day |
| Archive lag for a new board | median 48 days (p90 257) — distinct from posting age |
| urlscan.io | newest scan 1 day old; 30 searches/min anonymous |
| Median posting age | 62 days across all three; Lever alone is 97 |
| Common Crawl CDX | 502/504 on essentially every request; see below |

## Why Common Crawl is only the fallback

Common Crawl is the usual answer to "give me every URL under a domain", and it was this
project's first implementation. It loses on both axes that matter.

**Reliability.** Over an afternoon it returned 502/504 on essentially every request, and
the failure is server-side, not anything you can fix:

- `url=example.com`, a trivially cheap exact lookup, 504s identically to an expensive
  wildcard — so it isn't query cost.
- Indexes that don't exist (`CC-MAIN-2026-18`) 504 the same way — so it isn't a bad query.
- `collinfo.json`, on the same host, returns 200 in 45ms — the static file server is fine;
  the index backend is what times out.

Their docs explain it: the index server handles several million requests/day and sheds
requests on **queue overflow**. Everyone hits this at the same odds.

**Coverage.** For Ashby its estimate was ~3,400 candidate slugs; the Wayback Machine
yielded 3,617 *validated live* boards from a far larger URL set, and 13,132 across all
three platforms.

If you do fall through to it, the status codes differ in meaning. **502/504 = server
overloaded**, retry later, nothing you did. **503 = you are going too fast**; per their
docs a repeatedly-abusive IP can be blocked for 24 hours, so the script raises a distinct
error with that guidance rather than burning retries. CDX requests are throttled to
1/second, and `showNumPages` is avoided because it is the most expensive query they offer.

`boards.seed.json` (60 verified boards across the three platforms) is bundled regardless,
so **Phase 1 is always optional** and a fresh clone works without either index.

## For coding agents (LLMs)

If you have been pointed at this repository by a human, read this section first, then
[`openwiki/quickstart.md`](openwiki/quickstart.md).

**Orientation.** `README.md` is the user-facing narrative. `openwiki/` is the
engineer-facing map — [architecture](openwiki/architecture/overview.md),
[board discovery](openwiki/workflows/board-discovery.md),
[job scrape](openwiki/workflows/job-scrape.md),
[data model](openwiki/architecture/data-model.md),
[runbook](openwiki/operations/runbook.md), [testing](openwiki/testing.md).
The whole tool is one file, `job_boards.py`, zero dependencies. Per-platform differences
live in the `SOURCES` table and the three `normalize_*` functions; everything else is
platform-agnostic. Adding an ATS should mean one `SOURCES` entry and one normaliser, not
changes scattered through the pipeline.

**Before you run anything network-facing:**

```bash
export JOB_SCRAPER_CONTACT="the-user@example.com"   # ask; do not invent an address
uv run test_job_boards.py                             # offline, no network, ~1s
python3 test_job_boards.py                            # same suite without uv (>= 3.9)
```

If `uv` is not on your `PATH`, use the second command — it runs the identical suite on
any Python 3.9+, including macOS's system `python3`. Never report the tests as
unrunnable without trying it.

The test suite is the fast feedback loop — it covers every filter, all three normalisers,
the SQLite lifecycle and the archive-parsing paths without touching the network. Run it
before and after any change. A full `--refresh-boards --all` spans 13,146 boards across
three platforms and takes tens of minutes; do not run it casually, and never in a loop. `--ats <one> --limit 10` is
the cheap way to exercise a real request path.

**Things that look like bugs but are load-bearing.** Each is pinned by a test; if you
"fix" one, a test will fail and it is telling you the truth:

| Looks wrong | Why it is correct |
|---|---|
| `--since`/`--new-only` never set `closed_at` | A filtered run did not see what it skipped, so it cannot call those postings gone |
| Lever reads `text`, not `title` | That is Lever's field name. Reading `title` gives an empty column, not an error |
| Lever's `publishedAt` is converted from a number | `createdAt` is epoch **milliseconds**; left raw it sorts wrongly against the other platforms |
| The primary key is `(ats, id)`, not `id` | Greenhouse ids are integers, Ashby/Lever UUIDs — a bare key risks silent cross-platform overwrites |
| Greenhouse is queried without descriptions by default | `?content=true` is 26x the bytes; it is added only when `--grep` needs it |
| Fuzzy match requires a ≥2-word title in the reverse direction | Without it, `--title "senior software engineer"` matches every job titled `Engineer` |
| Only `--all` runs set `closed_at` | A filtered run cannot distinguish "gone" from "did not match my filter" |
| Closing is scoped to boards actually scanned | Otherwise `--limit 10` would "close" postings at thousands of unvisited companies |
| Validation uses `HEAD`, not `GET` | `GET` would download hundreds of KB per board — gigabytes per refresh |
| The User-Agent is stripped to ASCII | HTTP headers are latin-1; one em-dash made *every* request fail |
| `boards.json` is gitignored, `boards.seed.json` is committed | A full crawl is effectively three vendors' customer lists and must not be published |
| Only `--all --new-only` sends `If-None-Match` | A `304` means "body unchanged", which is only "no new postings" if the fetch that stored the ETag persisted every posting |
| Response headers are read in lowercase | The pooled path returns a plain dict, not urlopen's case-insensitive `Message` — and Lever sends `ETag` where the others send `etag` |
| Lever postings dated 2009 are kept | Upstream data, not a parse error. Palantir's board really does carry a `createdAt` of `2009-12-05` |

**Do not commit:** `boards.json`, `*.csv`, `*.json` outputs, `*.db`. `.gitignore` denies
these by default and allows only `boards.seed.json`. If you add an output format, add it
to `.gitignore` in the same change.

**Do not hand-edit `openwiki/`.** Those pages are generated. Change the source or the
README and let OpenWiki regenerate them (`openwiki --update`).

**Network etiquette is a requirement, not a style preference.** Concurrency is capped at
8, Common Crawl is throttled to its stated 1 request/second, and every request identifies
itself. Do not raise these to make something finish faster. A `429` or `403` backs off
exponentially and honours `Retry-After` when the server sends one, capped at 30 seconds;
backing off is the polite response to being throttled, so do not shorten it either.

**If you add a platform, add its posting-API host to `_POOLED_HOSTS`.** Connection
pooling is opt-in per host, so a new host silently keeps opening a fresh TLS connection
per request — no error, just the pre-pooling cost back for that platform. Pinned by
`test_every_posting_api_host_is_pooled`. Optimising the parsing is not worth doing at
all: it is 0.2% of a run.

**If you add a filter, update both gates.** Two functions decide what a run is entitled
to conclude, and a filter missing from either produces a normal-looking run that quietly
corrupts data. Both fail silently and permanently, so add your filter to both functions
and both tests in the same change.

| gate | what it decides | what a missing filter does |
|---|---|---|
| `may_close_postings()` | may this run stamp `closed_at`? | `--all --since 7d` closes every posting older than a week — the fill-rate signal is corrupted. Pinned by `test_only_an_unfiltered_run_may_close_postings` |
| `may_use_etags()` | may this run store and trust a `304`? | a `--title` run stores an ETag after saving matching rows only; a later run skips that board on `304`, so its other postings stay invisible even once a query matches them. Pinned by `test_etags_are_only_trusted_on_an_unfiltered_run` |

**If you are adding a search mode,** note that `--grep` patterns without `\b` are a
documented footgun (`rust` matches "t**rust**": 1350 hits vs 72). The script warns about
it. Keep that warning.

## Skipped, and when to add

- **Merging Wayback with Common Crawl** — the union would add slugs one index missed.
  Wayback alone already validates thousands per platform, so this buys little.
- **Token title matching** — fuzzy still misses "Software Development Engineer", where
  the words are present but not contiguous. `--grep` covers most of this need already.
- **Per-board caching** — postings change daily; caching mostly serves staleness.
- **Rate-limit backoff** — no 429s observed on any platform. Add on first sighting.
- **More ATS platforms** — SmartRecruiters and Workable expose similar public APIs and
  would each be one `SOURCES` entry plus a normaliser. Workday is per-tenant and would
  need real work.

## Keeping the wiki in sync

`openwiki/` is generated, and it goes stale fast — last time code moved without it, four
of its claims were wrong within the hour, including its own note about not being able to
run the tests.

The split is deliberate: **CI enforces that you regenerated; a human does the
regenerating.**

| workflow | trigger | what it does |
|---|---|---|
| `openwiki-drift-check.yml` | a PR touching `job_boards.py` or the tests | **fails** the PR if `openwiki/` was not updated too |
| `openwiki-update.yml` | manual (`workflow_dispatch`) | full refresh in CI, opens a PR — needs an API key |

So the loop is:

```bash
openwiki --update
git add openwiki AGENTS.md CLAUDE.md
git commit -m "docs: sync OpenWiki"
```

It watches source only. The wiki documents mechanisms rather than prose or data, so
README edits and `boards.seed.json` additions cannot invalidate it and do not trigger the
check. Put `[skip-wiki]` in the PR title to bypass it for a source change that genuinely
does not affect the wiki — a comment, a rename.

**Why the drift check doesn't just regenerate for you.** Regenerating needs provider
credentials, and OpenWiki can authenticate with a ChatGPT subscription — an OAuth
access/refresh pair scoped to the *whole account*, which expires and rotates. In a public
repository's Actions secrets that would be an account-level credential that also breaks
silently on expiry, so the check needs no credentials at all instead. A scoped
`OPENAI_API_KEY` or `OPENROUTER_API_KEY` is safe to add if you want CI to do the
regeneration — that is what `openwiki-update.yml` uses.

Both workflows keep `actions/checkout` on `head.sha` rather than a branch name, pass
every untrusted value through `env:` instead of interpolating it into a `run:` block, and
pin all actions to commit SHAs. The drift check runs with `contents: read` only.

## Performance

The tool is network-bound: json parsing and normalisation are **0.2%** of a run. So the
only levers are how many round trips it makes and how many connections it opens.

**Connections are pooled per thread.** Every posting-API request used to open a fresh
TLS connection — 13,146 handshakes in a full run. Now each of the 8 workers keeps one
connection per host open:

| | connections (300 boards) | full `--since 1d` run |
|---|---|---|
| before | 300 | 6m 35s |
| after | **23** | **3m 51s** |
| | 13x fewer | **41% faster** |

Measured by alternating A/B over 4 rounds, because wall clock on a network-bound tool
has ~56% run-to-run spread and a single comparison is not evidence. Pooling won every
round.

This is also the polite direction: 13x fewer TLS handshakes is less work for Ashby,
Greenhouse and Lever, not more. **Raising `--concurrency` is not on the table** — 8 is a
requirement, not a tunable, and it is the one knob that would speed things up by pushing
cost onto someone else's servers.

### Conditional requests on `--all --new-only`

All three APIs honour `If-None-Match` and answer `304` when a board has not changed.
`--all --new-only` stores each board's `ETag` and sends it back on the next run, so an
unchanged board transfers **nothing at all**:

```
450/450 boards | 0 404 | 0 err | 450 unchanged | 0 matches
```

Measured over 180 boards on a repeat pass: **17.23 MB → 0 MB, 100% of bytes eliminated.**
Wall clock improves less than that suggests, because a `304` still costs a round trip and
this workload is latency-bound — but it is a large reduction in what the platforms have
to serve.

**Only `--all --new-only` uses them, and that restriction is load-bearing.** A `304` says
the body is unchanged; concluding "no new postings" from that *also* requires that the
fetch which stored the ETag persisted every posting. A `--title` run stores matching rows
only, so trusting its ETag later would skip a board whose non-matching postings were
never recorded — they would stay invisible even once a later query did match them.
Storing and using ETags share one gate, `may_use_etags()`, so an ETag in the database
always came from a full, persisted fetch.

The three APIs disagree on header casing — Ashby and Greenhouse send `etag`, Lever sends
`ETag`. A case-sensitive lookup silently returns nothing, which is what made a first
measurement here report 1 of 3 platforms supporting `304` when all 3 do. Header names are
normalised to lowercase for exactly this reason.

## Being a good citizen

This reads only public, unauthenticated posting APIs — the same data any visitor
sees on a company's job board page. Requests are capped at 8 concurrent, Common Crawl is
throttled to their stated 1/second, and every request identifies itself via
`JOB_SCRAPER_CONTACT`. Please keep it that way if you fork.

## Author

Built by Matt Herzog.

- LinkedIn — [mtmherzog](https://www.linkedin.com/in/mtmherzog)
- X — [@mattherzogx](https://x.com/mattherzogx)
- YouTube — [@mattherzogtv](https://www.youtube.com/@mattherzogtv)

## License

MIT — see [LICENSE](LICENSE).
