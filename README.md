# Bulgarian Toto AI

Windows desktop application for collecting, validating and (in future
milestones) statistically analysing every available historical draw of the
Bulgarian Toto games **6/49**, **6/42** and **5/35**.

**Milestone 3 (current): coverage, provenance and data quality.** Database,
scraper and validation pipeline are operational and cover the historical
two-drawings-per-session era (milestone 2); a coverage/provenance engine now
measures how complete and trustworthy the imported history is. The UI is a
minimal navigation shell. No prediction or ML code exists yet — by design.

## Quick start

```powershell
# Python 3.12+
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt

.\.venv\Scripts\python main.py init      # create database + default config
.\.venv\Scripts\python main.py scrape    # import draws (Wayback + live site)
.\.venv\Scripts\python main.py validate  # print a validation report
.\.venv\Scripts\python main.py coverage  # print a coverage/provenance report
.\.venv\Scripts\python main.py           # launch the desktop shell
```

`main.py check` runs a headless self-check (config, logging, schema, seeds).

## How data is obtained

The official results live at `https://info.toto.bg/results/<game>/<year>-<draw>`,
but two constraints shape the scraper (full research notes:
[docs/RESEARCH.md](docs/RESEARCH.md)):

1. **The live site keeps only the 26 most recent draws per game.** Older
   draws are recovered from Internet Archive snapshots of the same official
   URLs (earliest: Dec 2023 for 6/42 and 5/35, Feb 2024 for 6/49).
2. **`*.toto.bg` sits behind Radware Bot Manager.** Plain HTTP clients get a
   challenge page, so live pages are fetched through a local *visible* Chrome
   instance via the DevTools protocol. The app never bypasses or solves
   challenges — if a CAPTCHA appears, you complete it in the Chrome window
   and re-run `scrape`. Wayback pages are fetched with plain, rate-limited
   HTTP.

Imports are resumable (per-page checkpoints), duplicate-safe (DB constraint
plus content hashing) and every page is processed in its own transaction.
Run `scrape` on a schedule (config: `update_frequency_hours`) to accumulate a
gap-free history going forward.

## Project structure

```
BulgarianTotoAI/
├── main.py                  # entry point: gui | init | scrape | validate | coverage | check
├── alembic.ini              # migration config (schema versioning)
├── app/
│   ├── analysis/            # reserved for statistics/ML (future milestones)
│   ├── config/              # JSON config system (settings.py)
│   ├── database/            # SQLAlchemy models, engine, repositories, seed
│   │   └── migrations/      # Alembic environment + versions
│   ├── models/              # framework-free domain objects (games, parsed draws)
│   ├── scraper/             # fetchers (HTTP + Chrome CDP), parser, Wayback, orchestrator
│   ├── services/            # logging setup, validation pipeline, coverage engine
│   └── ui/                  # PySide6 shell (dark theme, navigation only)
├── config/                  # user_config.json (created on first run)
├── data/                    # SQLite database (created on first run)
├── docs/RESEARCH.md         # official-site research findings
├── logs/                    # app.log, scraper.log, database.log, errors.log
├── scripts/                 # summary + UI smoke utilities
└── tests/                   # pytest suite with real captured HTML fixtures
```

## Architecture

* **Dependency injection, no globals.** `main.bootstrap()` builds the config,
  logging and `Database`; everything downstream receives its dependencies.
* **Layering.** `models` (pure domain) ← `database` (persistence) ←
  `scraper`/`services` (use cases) ← `ui`/CLI (delivery). The parser returns
  `ParsedDraw` dataclasses; only repositories touch the ORM.
* **Fetch strategy.** `Fetcher` protocol with two implementations:
  `RequestsFetcher` (retry, exponential backoff, rate limiting, bot-challenge
  detection) and `ChromeCdpFetcher` (DevTools-driven local Chrome for the
  bot-protected live site).
* **Schema versioning.** Fresh databases are created directly from the ORM
  metadata; schema changes ship as Alembic migrations
  (`alembic upgrade head`).

## Database schema (SQLite)

| Table | Purpose | Key constraints |
|---|---|---|
| `games` | game definitions (pool size, numbers drawn, bonus rules) | `code` unique |
| `draws` | one row per official *drawing*: number, year, drawing (1, or 2 for a historical second drawing), date, day-of-week/month/year, jackpot, prize pool total, winners total, currency, source URL, source (live/wayback/...), content hash, validation status, import timestamp | unique `(game_id, draw_year, draw_number, drawing)`; indexes on game/date |
| `draw_numbers` | one row per drawn ball (position, value, is_bonus) | unique `(draw_id, is_bonus, position)`; index on `value` |
| `prize_tiers` | official winnings table per draw (label, match count, winners, prize, total, currency) | unique `(draw_id, label)` |
| `scrape_checkpoints` | per-URL import progress for resume | unique `(game_id, segment)` |
| `validation_runs` / `validation_issues` | persisted validation history | — |

Draw numbering restarts every calendar year (official scheme), hence the
uniqueness on `(game, year, draw number)`. Until the mid-2010s a single draw
*session* of 6/49 and 5/35 published **two independent drawings** ("I-во
теглене" / "II-ро теглене"); each is stored as its own row distinguished by
`drawing` (1, or 2 for the second), which is why that column is part of the
uniqueness too. Monetary amounts carry an ISO currency code because the
source switched from BGN to EUR during the covered period.

Schema changes ship as Alembic migrations under
`app/database/migrations/versions/` (`0001` initial schema, `0002` adds the
`drawing` column and widens `draws.source`). Run `alembic upgrade head` to
bring an existing database up to date; a fresh database created via
`main.py init` already has the current schema.

## Validation pipeline

After every import (or via `main.py validate`) each game is checked for:

* **impossible numbering** — a draw number below 1
* **invalid drawing numbers** — `drawing` outside the known 1–2 range
* wrong number counts, out-of-range numbers, repeated numbers within a draw
* invalid dates (in the future, or numbering year != calendar year)
* **duplicate draws** — the same `(year, draw number, drawing)` stored twice
  (structurally prevented by the DB constraint; kept as a defensive check)
* **duplicate content** — identical winning numbers published on the
  identical date under two different draw refs, which is not a plausible
  lottery coincidence and signals a scraping/import bug
* **missing draw numbers (gaps)** within each covered year
* **missing second drawing** — a session has only drawing 1 in a year where
  sibling sessions *do* record a drawing 2, i.e. that year's two-drawing
  format is established by the data itself (see note below)
* **broken sequences** — draw dates not increasing with the draw number, or
  two drawings of one session dated differently

Results are printed as a report, persisted to
`validation_runs`/`validation_issues`, and each draw's `validation_status` is
updated (`valid`/`warning`/`invalid`).

Gap warnings for 2023–2025 are expected: they mark draws that predate the
live site's rolling window and are absent from the Internet Archive.

> **Why "missing second drawing" is data-driven, not date-driven.** We do
> not hardcode a historical cutoff year for when the two-drawing format
> ended, because there is no independently verified source for the exact
> transition date. Instead, a year is only treated as "should have two
> drawings" once *some* session in our own imported data for that year
> already has a `drawing=2` row — at which point any other session in that
> same year with only `drawing=1` is flagged. This means the check can only
> fire where the year's format is *determinable from the data we hold*, and
> will never flag a genuinely single-drawing year.

## Coverage & provenance reporting

`main.py coverage` measures how *complete* and *trustworthy* the imported
history is, per game — a read-only companion to `validate` (which measures
per-draw *correctness*; nothing here is persisted, so running it repeatedly
has no side effects).

```powershell
.\.venv\Scripts\python main.py coverage                              # print report to stdout
.\.venv\Scripts\python main.py coverage --game 6x49                  # one game only
.\.venv\Scripts\python main.py coverage --json output/coverage.json  # also write JSON
.\.venv\Scripts\python main.py coverage --csv output/coverage.csv    # also write CSV (one row per game)
```

Example output (real data, one game shown):

```
6/49
------
Imported draws: 124
Coverage: 46.8% (expected 265)

Earliest: 11/2024 (2024-02-08)
Latest: 55/2026 (2026-07-16)

Missing years: none
Missing draw numbers:
  2024: 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56 (20 total)
Missing dates (expected cadence, no draw found):
  2024: 2024-05-09, 2024-05-12, ... (20 total)

Sources:
Live: 26
Wayback: 98
Other: 0

Confidence (validation status by source):
  live: valid=26
  wayback: valid=98

Duplicate sessions detected: 0
```

### What each field means

* **Imported draws / Coverage** — count of distinct *sessions*
  `(game, year, draw number)`, not raw rows, so a historical two-drawing
  session counts once (its row count and drawing-count are shown separately
  when non-zero). Coverage is `imported / expected`.
* **Expected draw count** — the official cadence is twice a week, Thursday
  and Sunday (see [docs/RESEARCH.md](docs/RESEARCH.md)). "Expected" is every
  Thursday/Sunday from the earliest to the latest *imported* year for that
  game, bounded by today for the current year. This is only computed where
  **determinable**: a game with zero imported draws has no expected count at
  all, and the calculation never extrapolates into the decades of pre-2023
  history that docs/RESEARCH.md documents as unrecoverable from any current
  source — it only measures the span we actually have a foothold in.
* **Missing years** — years inside the earliest–latest span with *zero*
  imported draws. This is a gap `validate`'s own gap-check cannot see (that
  check only looks for gaps *within* a year that already has data).
* **Missing draw numbers** — numbering gaps within each year's observed
  range (e.g. have 1, 2, 4 → missing 3).
* **Missing dates** — expected Thursday/Sunday calendar dates within each
  year's observed date range that have no imported draw, independent of
  whether the draw-number sequence itself has a gap.
* **Duplicate sessions** — sessions implicated in a "duplicate content"
  finding (see validation pipeline above).
* **Sources / Confidence** — row counts per `source` label (`live`,
  `wayback`, and any other value in use), broken down by each draw's current
  `validation_status` as a proxy for confidence. A source with many
  `invalid`/`warning` rows is less trustworthy than one that is entirely
  `valid`; `pending` means `validate` has not run since import.

### Data quality explanation

Coverage percentages under 100% are **expected and not necessarily a bug**:
the live site only exposes a rolling window and the Internet Archive's
earliest snapshots already start mid-sequence (e.g. 6/49's earliest
recoverable draw is 11/2024 — draws 1–10/2024 are permanently unavailable,
per docs/RESEARCH.md). The coverage report distinguishes *where* the gaps
are (missing years vs. missing numbers vs. missing dates) so genuine scraper
regressions (a newly-opened gap in an already-covered range) are easy to
tell apart from historically-unavailable data (draws before this project's
earliest recoverable source).

## Configuration (`config/user_config.json`)

| Key | Default | Meaning |
|---|---|---|
| `database_path` | `data/toto.db` | SQLite location |
| `log_dir` / `log_level` | `logs` / `INFO` | logging |
| `theme` | `dark` | UI theme |
| `request_timeout_seconds` | `30` | per-request timeout |
| `retry_count` / `retry_backoff_seconds` | `4` / `2` | retry policy (exponential) |
| `rate_limit_seconds` | `1.5` | minimum interval between requests |
| `update_frequency_hours` | `24` | intended scrape cadence |
| `toto_base_url` / `chrome_debug_url` | official site / `http://localhost:9222` | endpoints |

## Tests

```powershell
.\.venv\Scripts\python -m pytest tests
```

72 tests cover configuration, database schema/repositories, the parser
(against real captured pages, including a BGN-era Wayback snapshot, a
synthetic two-drawing session and the Radware challenge page), the
validation pipeline (including the historical-drawing and duplicate-content
checks), the coverage engine and scraper orchestration (resume, duplicate
detection, failure recovery) — no network access needed.

## Roadmap

1. **Milestone 1 (done):** research, database, scraper, validation, shell UI
2. **Milestone 2 (done):** historical two-drawing sessions
3. **Milestone 3 (done):** coverage & provenance reporting, expanded validation
4. Historical browser + per-number statistics UI
5. Statistical models and the Prediction Lab
6. Backtesting engine
7. Packaging (PyInstaller)

*This software analyses historical data. Lottery draws are random; no
prediction can improve the odds of winning.*
