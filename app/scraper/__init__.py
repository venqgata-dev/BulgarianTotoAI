"""Scraper for official Bulgarian Toto results.

Research findings (July 2026) are documented in docs/RESEARCH.md. Key facts
that shape this package:

* Results are served by ``https://info.toto.bg/results/<game>/<year>-<draw>``
  as server-rendered HTML (games: ``6x49``, ``6x42``, ``5x35``).
* The live site only exposes a rolling window of the most recent 26 draws
  per game.
* The whole ``*.toto.bg`` zone sits behind Radware Bot Manager: plain HTTP
  clients receive a JavaScript challenge instead of content, so live pages
  are fetched through a local Chrome instance via the DevTools protocol
  (:class:`app.scraper.fetch.ChromeCdpFetcher`). The application never
  attempts to solve challenges or CAPTCHAs; if one is shown, the user
  completes it in the visible Chrome window.
* Older draws (back to Dec 2023 / Feb 2024) are recovered from Internet
  Archive snapshots of the same official URLs (:mod:`app.scraper.wayback`),
  which are fetched with plain HTTP.
"""

from app.scraper.fetch import (
    BotProtectionError,
    ChromeCdpFetcher,
    FetchError,
    Fetcher,
    RequestsFetcher,
)
from app.scraper.parser import ParseError, TotoParser
from app.scraper.service import ImportStats, ScraperService

__all__ = [
    "BotProtectionError",
    "ChromeCdpFetcher",
    "FetchError",
    "Fetcher",
    "ImportStats",
    "ParseError",
    "RequestsFetcher",
    "ScraperService",
    "TotoParser",
]
