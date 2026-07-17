"""Bulgarian Toto AI - entry point.

Usage:
    python main.py                  Launch the desktop shell
    python main.py init             Initialise database + config, then exit
    python main.py scrape           Import draws (live window + Wayback backfill)
    python main.py scrape --source live|wayback|all [--game 6x49]
    python main.py validate [--game 6x49]
    python main.py check            Headless self-check (used by CI/tests)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.config.settings import AppConfig, ConfigManager, default_config_path
from app.database.engine import Database
from app.database.seed import seed_games
from app.models.domain import SUPPORTED_GAMES
from app.services.logging_service import get_logger, setup_logging

GAME_CODES = tuple(g.code for g in SUPPORTED_GAMES)


def bootstrap(config_path: Path | None = None) -> tuple[AppConfig, Database]:
    """Load config, configure logging, open the database and seed games."""
    manager = ConfigManager(config_path or default_config_path())
    config = manager.load()
    if not manager.config_path.exists():
        manager.save(config)  # materialise defaults for the user to edit
    setup_logging(Path(config.log_dir), config.log_level)
    database = Database(Path(config.database_path))
    database.create_schema()
    with database.session() as session:
        seed_games(session)
    return config, database


def _run_gui(config: AppConfig, database: Database) -> int:
    from PySide6.QtWidgets import QApplication

    from app.ui.main_window import MainWindow
    from app.ui.theme import DARK_QSS

    app = QApplication(sys.argv[:1])
    if config.theme == "dark":
        app.setStyleSheet(DARK_QSS)
    window = MainWindow(database)
    window.show()
    return app.exec()


def _run_scrape(config: AppConfig, database: Database, source: str, game: str | None) -> int:
    from app.scraper.fetch import BotProtectionError, ChromeCdpFetcher, RequestsFetcher
    from app.scraper.service import ScraperService

    log = get_logger("app")
    games = [game] if game else list(GAME_CODES)
    archive_fetcher = RequestsFetcher(config)
    live_fetcher = ChromeCdpFetcher(config)
    service = ScraperService(
        database,
        live_fetcher=live_fetcher,
        archive_fetcher=archive_fetcher,
        progress=lambda message: print(message, flush=True),
    )

    exit_code = 0
    if source in ("wayback", "all"):
        stats = service.import_wayback(games)
        print(f"Wayback import: {stats.summary()}")
    if source in ("live", "all"):
        profile_dir = Path(config.database_path).parent / "chrome_profile"
        if not live_fetcher.ensure_chrome(profile_dir):
            log.error(
                "No debuggable Chrome found at %s and it could not be started. "
                "Live import skipped; Wayback data is unaffected.",
                config.chrome_debug_url,
            )
            print("Live import skipped: Chrome (DevTools) unavailable.")
            exit_code = 1
        else:
            try:
                stats = service.import_live(games)
                print(f"Live import: {stats.summary()}")
            except BotProtectionError as exc:
                print(f"Live import interrupted by bot protection: {exc}")
                exit_code = 1
            finally:
                live_fetcher.close()
    return exit_code


def _run_validate(database: Database, game: str | None) -> int:
    from app.services.validation import ValidationService

    report = ValidationService(database).validate(game)
    print(report.to_text())
    return 0


def _run_check(config: AppConfig, database: Database) -> int:
    """Headless health check: config, logging, schema and seeds all worked."""
    with database.session() as session:
        from app.database.repository import GameRepository

        games = GameRepository(session).all_games()
    print(f"OK: database at {config.database_path} with {len(games)} games seeded")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="BulgarianTotoAI", description=__doc__)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init")
    sub.add_parser("check")
    scrape = sub.add_parser("scrape")
    scrape.add_argument("--source", choices=("live", "wayback", "all"), default="all")
    scrape.add_argument("--game", choices=GAME_CODES)
    validate = sub.add_parser("validate")
    validate.add_argument("--game", choices=GAME_CODES)
    args = parser.parse_args(argv)

    config, database = bootstrap()
    try:
        if args.command in (None, "gui"):
            return _run_gui(config, database)
        if args.command == "init":
            print(f"Initialised database at {config.database_path}")
            return 0
        if args.command == "check":
            return _run_check(config, database)
        if args.command == "scrape":
            code = _run_scrape(config, database, args.source, args.game)
            _run_validate(database, args.game)
            return code
        if args.command == "validate":
            return _run_validate(database, args.game)
        parser.error(f"unknown command {args.command!r}")
        return 2
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
