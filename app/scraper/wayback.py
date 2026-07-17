"""Internet Archive (Wayback Machine) access for historical draw pages.

The live official site only keeps the most recent 26 draws per game, but the
Wayback Machine holds snapshots of the same ``info.toto.bg/results/...`` URLs
going back to late 2023. Snapshot HTML is identical in structure, so the
regular parser is reused. Fetched via plain HTTP with the standard retry and
rate-limiting policy (the Internet Archive is not bot-protected but deserves
polite crawling).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlencode

from app.scraper.fetch import FetchError, Fetcher
from app.services.logging_service import get_logger

CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"

# The CDX wildcard also matches assets (favicon.ico, ...); keep only real
# draw detail URLs like .../results/6x49/2024-11
_DRAW_PAGE_RE = re.compile(r"/results/[0-9a-z]+/\d{4}-\d+/?$")


@dataclass(frozen=True, slots=True)
class WaybackSnapshot:
    """One archived capture of an original URL."""

    original_url: str
    timestamp: str  # YYYYMMDDhhmmss

    @property
    def snapshot_url(self) -> str:
        # ``id_`` returns the original bytes without Wayback's toolbar/rewriting.
        return f"https://web.archive.org/web/{self.timestamp}id_/{self.original_url}"


class WaybackClient:
    """Lists and fetches archived draw pages for one game."""

    def __init__(self, fetcher: Fetcher) -> None:
        self._fetcher = fetcher
        self._log = get_logger("scraper.wayback")

    def list_draw_snapshots(self, game_code: str) -> list[WaybackSnapshot]:
        """Return one snapshot per unique archived draw URL of ``game_code``."""
        query = urlencode(
            {
                "url": f"info.toto.bg/results/{game_code}/*",
                "output": "json",
                "fl": "original,timestamp,statuscode",
                "collapse": "urlkey",
                "filter": "statuscode:200",
            }
        )
        raw = self._fetcher.fetch(f"{CDX_ENDPOINT}?{query}")
        try:
            rows: list[list[str]] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FetchError(f"CDX API returned invalid JSON for {game_code}") from exc
        snapshots = [
            WaybackSnapshot(original_url=row[0], timestamp=row[1])
            for row in rows[1:]  # first row is the header
            if len(row) >= 2 and _DRAW_PAGE_RE.search(row[0])
        ]
        self._log.info("CDX lists %d archived draw pages for %s", len(snapshots), game_code)
        return snapshots

    def fetch_snapshot(self, snapshot: WaybackSnapshot) -> str:
        return self._fetcher.fetch(snapshot.snapshot_url)
