# Bulgarian Toto AI

Windows desktop application for collecting, validating and (in future
milestones) statistically analysing every available historical draw of the
Bulgarian Toto games **6/49**, **6/42** and **5/35**.

**Milestone 1 (current): data foundation.** Database, scraper and validation
pipeline are operational; the UI is a minimal navigation shell. No prediction
or ML code exists yet — by design.

## Quick start

```powershell
# Python 3.12+
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt

.\.venv\Scripts\python main.py init      # create database + default config
.\.venv\Scripts\python main.py scrape    # import draws (Wayback + live site)
.\.venv\Scripts\python main.py validate  # print a validation report
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
├── main.py                  # entry point: gui | init | scrape | validate | check
├── alembic.ini              # migration config (schema versioning)
├── app/
│   ├── analysis/            # reserved for statistics/ML (future milestones)
│   ├── config/              # JSON config system (settings.py)
│   ├── database/            # SQLAlchemy models, engine, repositories, seed
│   │   └── migrations/      # Alembic environment + versions
│   ├── models/              # framework-free domain objects (games, parsed draws)
│   ├── scraper/             # fetchers (HTTP + Chrome CDP), parser, Wayback, orchestrator
│   ├── services/            # logging setup, validation pipeline
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
| `draws` | one row per official draw: number, year, date, day-of-week/month/year, jackpot, prize pool total, winners total, currency, source URL, source (live/wayback), content hash, validation status, import timestamp | unique `(game_id, draw_year, draw_number)`; indexes on game/date |
| `draw_numbers` | one row per drawn ball (position, value, is_bonus) | unique `(draw_id, is_bonus, position)`; index on `value` |
| `prize_tiers` | official winnings table per draw (label, match count, winners, prize, total, currency) | unique `(draw_id, label)` |
| `scrape_checkpoints` | per-URL import progress for resume | unique `(game_id, segment)` |
| `validation_runs` / `validation_issues` | persisted validation history | — |

Draw numbering restarts every calendar year (official scheme), hence the
three-column uniqueness. Monetary amounts carry an ISO currency code because
the source switched from BGN to EUR during the covered period.

## Validation pipeline

After every import (or via `main.py validate`) each game is checked for:
wrong number counts, out-of-range numbers, repeated numbers, future dates,
numbering-year mismatches, duplicate draws, missing draw numbers (gaps) and
draw dates that do not increase with the draw number. Results are printed as
a report, persisted to `validation_runs`/`validation_issues`, and each draw's
`validation_status` is updated (`valid`/`warning`/`invalid`).

Gap warnings for 2023–2025 are expected: they mark draws that predate the
live site's rolling window and are absent from the Internet Archive.

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

41 tests cover configuration, database schema/repositories, the parser
(against real captured pages, including a BGN-era Wayback snapshot and the
Radware challenge page), the validation pipeline and scraper orchestration
(resume, duplicate detection, failure recovery) — no network access needed.

## Roadmap

1. **Milestone 1 (done):** research, database, scraper, validation, shell UI
2. Historical browser + per-number statistics UI
3. Statistical models and the Prediction Lab
4. Backtesting engine
5. Packaging (PyInstaller)

*This software analyses historical data. Lottery draws are random; no
prediction can improve the odds of winning.*
