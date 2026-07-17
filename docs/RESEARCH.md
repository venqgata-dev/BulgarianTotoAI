# Official Bulgarian Toto Website — Research Findings

*Research performed on 17 July 2026 against the live site and the Internet
Archive. All selectors and URLs in the codebase come from this inspection —
nothing is guessed.*

## Site structure

| Property | Finding |
|---|---|
| Main domain | `toto.bg` — JavaScript SPA for online play; **no results archive** |
| Results subsite | `info.toto.bg` — classic server-rendered pages (Bootstrap 3) |
| Results URL scheme | `https://info.toto.bg/results/<game>/<year>-<draw>` |
| Game codes | `6x49`, `6x42`, `5x35` (also `joker`, `zodiak`, `rojdenden` — out of scope) |
| List page | `https://info.toto.bg/results/<game>` shows the latest draw in full plus a dropdown linking the recent draws |

## Bot protection (critical)

The whole `*.toto.bg` zone sits behind **Radware Bot Manager**:

* Plain HTTP clients (requests, curl, PowerShell) receive a JavaScript
  challenge page (`<title>Radware Captcha Page</title>`) instead of content —
  status 200, no data.
* Headless Chrome is detected and escalated to a CAPTCHA.
* A normal, headful Chrome session passes the challenge automatically.
* Radware injects its bootstrap script (`__uzdbm_*`, `stormcaster.js`,
  `validate.perfdrive.com`) into **legitimate pages too**, so challenge
  detection must key on the challenge page title, not on script presence.

Consequence for the scraper: live pages are fetched through a local,
**visible** Chrome instance driven over the DevTools protocol
(`--remote-debugging-port`). The application never attempts to bypass or
solve a challenge; if a CAPTCHA is ever shown, the user completes it in the
Chrome window and re-runs the import.

## Data organisation

* **Draw numbering restarts at 1 every calendar year.** A draw is uniquely
  identified by *(game, year, draw number)*, e.g. draw `55/2026`.
* All three Toto 2 games are drawn together, currently twice a week
  (Thursday and Sunday), so draw numbers align across games.
* A draw page (`div.tir_result`) contains:
  * `h2.tir_title` — "Тираж 55 - 16.07.2026" (number + date)
  * `span.ball-white` — winning numbers (displayed in ascending order)
  * `div.tir_jackpot` — jackpot amount, when a jackpot is accumulating
  * a winnings table — one row per prize tier ("6 числа", "5 числа", ...)
    with winner count, prize per winner and total amount
* **No bonus ball** exists in any of the three games (schema still supports
  one for future games).
* **Currency changed over time**: amounts are labelled "лева" (BGN) in
  2023–2025 snapshots and "euro" (EUR) on 2026 pages. Amounts are stored
  with an explicit ISO currency code and must never be aggregated across
  currencies blindly.
* The sidebar of every page repeats the latest numbers of *all* games —
  parsing must stay scoped to `div.tir_result`.

## Archive depth / pagination

* The live site exposes a **rolling window of only the 26 most recent draws
  per game** (~3 months). Older URLs return HTTP 404 — verified:
  `2026-30` (oldest dropdown entry) works, `2026-29` is 404.
* There is **no year navigation, no pagination, no deep archive** anywhere on
  the current official site (checked results pages, the statistics pages and
  the sitemap). The statistics pages contain aggregates only.
* **Internet Archive (Wayback Machine)** holds snapshots of the same official
  URLs. Coverage found via the CDX API (July 2026):

| Game | Archived draw pages | Earliest draw | Earliest date |
|---|---|---|---|
| 6/49 | 124 | `2024-11` | 08 Feb 2024 |
| 6/42 | 130 | `2023-103` | 28 Dec 2023 |
| 5/35 | 57 | `2023-100` | 17 Dec 2023 |

* Some Wayback captures from May 2026 archived the *challenge page* instead
  of content; these are detected and skipped (they stay `failed` in
  `scrape_checkpoints`).
* Coverage is partial — gaps exist inside the archived years and are
  reported by the validation pipeline as warnings (known-missing data).

## Earliest available data (official sources, as of July 2026)

* **6/49:** draw 11/2024 (8 Feb 2024) — via Wayback
* **6/42:** draw 103/2023 (28 Dec 2023) — via Wayback
* **5/35:** draw 100/2023 (17 Dec 2023) — via Wayback
* Live site alone: draw 30/2026 (19 Apr 2026) for all three games.

## Limitations discovered

1. Decades of pre-2023 history (6/49 has existed since 1968) are **not
   available** on the current official site in any form. The pre-redesign
   toto.bg site exists in the Wayback Machine with different markup — a
   possible future backfill source requiring a dedicated parser.
2. The official pages do not publish machine-readable data (no API, no CSV);
   only rendered HTML.
3. Draw pages show the jackpot only when one is accumulating — the column is
   nullable.
4. Running the application regularly (the live window moves twice a week) is
   required to accumulate a complete uninterrupted history going forward.
