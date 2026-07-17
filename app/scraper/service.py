"""Import orchestration: fetch -> parse -> deduplicate -> persist -> checkpoint.

Each draw page is processed in its own transaction, so an interrupted run
loses at most one page of work and can always be resumed. Completed segments
are recorded in ``scrape_checkpoints`` and skipped on the next run.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from app.database.engine import Database
from app.database.repository import (
    CheckpointRepository,
    DrawRepository,
    GameRepository,
    content_hash,
)
from app.models.domain import ParsedDraw
from app.scraper.fetch import BotProtectionError, FetchError, Fetcher
from app.scraper.parser import ParseError, TotoParser
from app.scraper.wayback import WaybackClient
from app.services.logging_service import get_logger

ProgressCallback = Callable[[str], None]

LIVE_RESULTS_URL = "https://info.toto.bg/results/{game_code}"


@dataclass(slots=True)
class ImportStats:
    """Outcome of one import run."""

    pages_fetched: int = 0
    draws_imported: int = 0
    skipped_existing: int = 0
    skipped_checkpointed: int = 0
    failures: int = 0
    errors: list[str] = field(default_factory=list)

    def merge(self, other: "ImportStats") -> None:
        self.pages_fetched += other.pages_fetched
        self.draws_imported += other.draws_imported
        self.skipped_existing += other.skipped_existing
        self.skipped_checkpointed += other.skipped_checkpointed
        self.failures += other.failures
        self.errors.extend(other.errors)

    def summary(self) -> str:
        return (
            f"fetched={self.pages_fetched} imported={self.draws_imported} "
            f"existing={self.skipped_existing} checkpointed={self.skipped_checkpointed} "
            f"failed={self.failures}"
        )


class ScraperService:
    """Coordinates live-site and Wayback imports for the supported games."""

    def __init__(
        self,
        database: Database,
        live_fetcher: Fetcher,
        archive_fetcher: Fetcher,
        parser: TotoParser | None = None,
        progress: ProgressCallback | None = None,
    ) -> None:
        self._db = database
        self._live_fetcher = live_fetcher
        self._archive = WaybackClient(archive_fetcher)
        self._parser = parser or TotoParser()
        self._progress = progress or (lambda _message: None)
        self._log = get_logger("scraper")

    # -- public API --------------------------------------------------------------

    def import_live(self, game_codes: Sequence[str]) -> ImportStats:
        """Import every draw currently exposed by the live site (rolling window)."""
        stats = ImportStats()
        for game_code in game_codes:
            stats.merge(self._import_live_game(game_code))
        self._log.info("Live import finished: %s", stats.summary())
        return stats

    def import_wayback(self, game_codes: Sequence[str]) -> ImportStats:
        """Backfill historical draws from Internet Archive snapshots."""
        stats = ImportStats()
        for game_code in game_codes:
            stats.merge(self._import_wayback_game(game_code))
        self._log.info("Wayback import finished: %s", stats.summary())
        return stats

    # -- live site ---------------------------------------------------------------

    def _import_live_game(self, game_code: str) -> ImportStats:
        stats = ImportStats()
        list_url = LIVE_RESULTS_URL.format(game_code=game_code)
        self._progress(f"[{game_code}] loading results page...")
        try:
            html = self._live_fetcher.fetch(list_url)
            stats.pages_fetched += 1
        except FetchError as exc:
            self._record_failure(stats, game_code, list_url, exc)
            return stats

        # The list page itself shows the latest draw in full.
        try:
            latest = self._parser.parse_draw_page(html, game_code, source_url=list_url)
            for parsed in latest:
                self._store_draw(game_code, parsed, source="live", stats=stats)
        except ParseError as exc:
            self._record_failure(stats, game_code, list_url, exc)

        links = self._parser.parse_draw_links(html, game_code)
        self._log.info("[%s] live archive window lists %d draws", game_code, len(links))
        for index, url in enumerate(links, start=1):
            self._progress(f"[{game_code}] live draw {index}/{len(links)}")
            self._import_draw_url(game_code, url, source="live", stats=stats)
        return stats

    def _import_draw_url(self, game_code: str, url: str, source: str, stats: ImportStats) -> None:
        segment = url
        game_id = self._game_id(game_code)
        with self._db.session() as session:
            if CheckpointRepository(session).is_done(game_id, segment):
                stats.skipped_checkpointed += 1
                return
            ref = self._parser.draw_ref_from_url(url)
            # A page's drawings are always stored together in one _store_draw
            # pass below, so the first drawing existing is a reliable proxy
            # for "this URL was already imported".
            if ref and DrawRepository(session).exists(game_id, *ref):
                CheckpointRepository(session).mark(game_id, segment, "skipped", "already in database")
                stats.skipped_existing += 1
                return
        try:
            html = self._live_fetcher.fetch(url)
            stats.pages_fetched += 1
            parsed = self._parser.parse_draw_page(html, game_code, source_url=url)
        except BotProtectionError:
            raise  # not recoverable within this run; caller decides
        except (FetchError, ParseError) as exc:
            self._record_failure(stats, game_code, url, exc)
            with self._db.session() as session:
                CheckpointRepository(session).mark(game_id, segment, "failed", str(exc)[:500])
            return
        for draw in parsed:
            self._store_draw(game_code, draw, source=source, stats=stats, segment=segment)

    # -- wayback -----------------------------------------------------------------

    def _import_wayback_game(self, game_code: str) -> ImportStats:
        stats = ImportStats()
        game_id = self._game_id(game_code)
        self._progress(f"[{game_code}] querying Internet Archive index...")
        try:
            snapshots = self._archive.list_draw_snapshots(game_code)
        except FetchError as exc:
            self._record_failure(stats, game_code, "CDX index", exc)
            return stats

        for index, snapshot in enumerate(snapshots, start=1):
            segment = f"wayback:{snapshot.original_url}"
            with self._db.session() as session:
                if CheckpointRepository(session).is_done(game_id, segment):
                    stats.skipped_checkpointed += 1
                    continue
                ref = self._parser.draw_ref_from_url(snapshot.original_url)
                if ref and DrawRepository(session).exists(game_id, *ref):
                    CheckpointRepository(session).mark(game_id, segment, "skipped", "already in database")
                    stats.skipped_existing += 1
                    continue
            self._progress(f"[{game_code}] wayback snapshot {index}/{len(snapshots)}")
            try:
                html = self._archive.fetch_snapshot(snapshot)
                stats.pages_fetched += 1
                parsed = self._parser.parse_draw_page(
                    html, game_code, source_url=snapshot.snapshot_url
                )
            except (FetchError, ParseError) as exc:
                self._record_failure(stats, game_code, snapshot.snapshot_url, exc)
                with self._db.session() as session:
                    CheckpointRepository(session).mark(game_id, segment, "failed", str(exc)[:500])
                continue
            for draw in parsed:
                self._store_draw(game_code, draw, source="wayback", stats=stats, segment=segment)
        return stats

    # -- shared helpers ----------------------------------------------------------

    def _game_id(self, game_code: str) -> int:
        with self._db.session() as session:
            return GameRepository(session).by_code(game_code).id

    def _store_draw(
        self,
        game_code: str,
        parsed: ParsedDraw,
        source: str,
        stats: ImportStats,
        segment: str | None = None,
    ) -> None:
        with self._db.session() as session:
            game = GameRepository(session).by_code(game_code)
            draws = DrawRepository(session)
            existing = draws.get(game.id, parsed.draw_year, parsed.draw_number, parsed.drawing)
            if existing is not None:
                if existing.content_hash != content_hash(parsed):
                    message = (
                        f"[{game_code}] draw {parsed.official_ref} already stored with different "
                        f"content (existing source={existing.source}); keeping original"
                    )
                    self._log.warning(message)
                    stats.errors.append(message)
                stats.skipped_existing += 1
                if segment:
                    CheckpointRepository(session).mark(game.id, segment, "skipped", "duplicate")
                return
            draws.add_parsed(game, parsed, source=source)
            if segment:
                CheckpointRepository(session).mark(game.id, segment, "done")
            stats.draws_imported += 1
            self._log.info(
                "[%s] imported draw %s (%s) numbers=%s",
                game_code,
                parsed.official_ref,
                parsed.draw_date.isoformat(),
                ",".join(map(str, parsed.numbers)),
            )

    def _record_failure(self, stats: ImportStats, game_code: str, url: str, exc: Exception) -> None:
        message = f"[{game_code}] {url}: {exc}"
        self._log.error(message)
        stats.failures += 1
        stats.errors.append(message)
