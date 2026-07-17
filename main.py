"""Bulgarian Toto AI - entry point.

Usage:
    python main.py                  Launch the desktop shell
    python main.py init             Initialise database + config, then exit
    python main.py scrape           Import draws (live window + Wayback backfill)
    python main.py scrape --source live|wayback|all [--game 6x49]
    python main.py validate [--game 6x49]
    python main.py coverage [--game 6x49] [--json PATH] [--csv PATH]
    python main.py stats --game 6x49 [--years 2024,2025] [--last-n 50] [--json PATH] [--csv PATH]
    python main.py browse --game 6x49 [--year Y --number N [--drawing 1|2]]
    python main.py browse --game 6x49 [--date YYYY-MM-DD] [--find-number N]
    python main.py backtest --game 6x49 [--strategy hot|cold|gap|balanced|hybrid|random|all]
    python main.py backtest --game 6x49 --strategy hybrid --years 2025,2026
    python main.py backtest --game 6x49 --strategy random --seed 42
    python main.py backtest --game 6x49 [--from-date Y-M-D] [--to-date Y-M-D] [--last-n N]
    python main.py backtest --game 6x49 [--json PATH] [--csv PATH]
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


def _run_coverage(
    database: Database, game: str | None, json_path: str | None, csv_path: str | None
) -> int:
    from app.services.coverage import CoverageService

    report = CoverageService(database).analyze(game)
    print(report.to_text())
    if json_path:
        report.write_json(Path(json_path))
        print(f"Coverage JSON written to {json_path}")
    if csv_path:
        report.write_csv(Path(csv_path))
        print(f"Coverage CSV written to {csv_path}")
    return 0


def _run_stats(
    database: Database,
    game: str,
    years: str | None,
    last_n: int | None,
    json_path: str | None,
    csv_path: str | None,
) -> int:
    from app.analysis.statistics import StatisticsService

    year_list = [int(y) for y in years.split(",")] if years else None
    report = StatisticsService(database).analyze(game, years=year_list, last_n=last_n)
    print(report.to_text())
    if json_path:
        report.write_json(Path(json_path))
        print(f"Statistics JSON written to {json_path}")
    if csv_path:
        report.write_csv(Path(csv_path))
        print(f"Statistics CSV (number frequency table) written to {csv_path}")
    return 0


def _print_draw_detail(detail) -> None:
    print(f"{detail.game_name} - {detail.ref}")
    print("-" * 40)
    print(f"Date: {detail.draw_date}")
    print(f"Drawing: {detail.drawing}" + ("  (historical second drawing)" if detail.is_second_drawing else ""))
    print(f"Numbers: {', '.join(map(str, detail.numbers))}")
    if detail.bonus_numbers:
        print(f"Bonus: {', '.join(map(str, detail.bonus_numbers))}")
    if detail.jackpot_amount is not None:
        print(f"Jackpot: {detail.jackpot_amount} {detail.currency or ''}".strip())
    if detail.prize_tiers:
        print("Prize tiers:")
        for tier in detail.prize_tiers:
            print(f"  {tier.label}: winners={tier.winners} prize={tier.prize_amount} {tier.currency or ''}")
    print(f"Source: {detail.source}")
    if detail.source_url:
        print(f"Source URL: {detail.source_url}")
    print(f"Validation status: {detail.validation_status}")


def _run_browse(
    database: Database,
    game: str,
    year: int | None,
    number: int | None,
    drawing: int,
    target_date: str | None,
    find_number: int | None,
) -> int:
    from datetime import date as date_cls

    from app.services.browser import HistoricalBrowserService

    service = HistoricalBrowserService(database)

    if target_date:
        matches = service.search_by_date(game, date_cls.fromisoformat(target_date))
        if not matches:
            print(f"No draws found for {game} on {target_date}")
            return 1
        for detail in matches:
            _print_draw_detail(detail)
            print()
        return 0

    if find_number is not None:
        matches = service.search_by_draw_number(game, find_number)
        if not matches:
            print(f"No draws found for {game} with draw number {find_number}")
            return 1
        for detail in matches:
            _print_draw_detail(detail)
            print()
        return 0

    if year is not None and number is not None:
        detail = service.get(game, year, number, drawing)
        if detail is None:
            print(f"Draw {number}/{year}#{drawing} not found for {game}")
            return 1
        _print_draw_detail(detail)
        return 0

    detail = service.latest(game)
    if detail is None:
        print(f"No draws imported yet for {game}")
        return 1
    _print_draw_detail(detail)
    return 0


def _run_backtest(
    database: Database,
    game: str,
    strategy: str,
    years: str | None,
    from_date: str | None,
    to_date: str | None,
    last_n: int | None,
    seed: int | None,
    json_path: str | None,
    csv_path: str | None,
) -> int:
    from datetime import date as date_cls

    from app.analysis.backtest import BacktestService
    from app.analysis.strategies import STRATEGY_REGISTRY

    year_list = [int(y) for y in years.split(",")] if years else None
    date_from = date_cls.fromisoformat(from_date) if from_date else None
    date_to = date_cls.fromisoformat(to_date) if to_date else None
    service = BacktestService(database)

    def params_for(name: str) -> dict:
        return {"seed": seed} if name == "random" and seed is not None else {}

    if strategy in (None, "all", "compare"):
        names = sorted(STRATEGY_REGISTRY)
        report = service.compare(
            game,
            names,
            strategy_params={n: params_for(n) for n in names},
            years=year_list,
            date_from=date_from,
            date_to=date_to,
            last_n=last_n,
        )
        print(report.to_text())
        if json_path:
            report.write_json(Path(json_path))
            print(f"Comparison JSON written to {json_path}")
        if csv_path:
            report.write_csv(Path(csv_path))
            print(f"Comparison CSV written to {csv_path}")
        return 0

    report = service.run(
        game,
        strategy,
        strategy_params=params_for(strategy),
        years=year_list,
        date_from=date_from,
        date_to=date_to,
        last_n=last_n,
    )
    print(report.to_text())
    if json_path:
        report.write_json(Path(json_path))
        print(f"Backtest JSON written to {json_path}")
    if csv_path:
        report.write_csv(Path(csv_path))
        print(f"Backtest CSV (history table) written to {csv_path}")
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
    coverage = sub.add_parser("coverage")
    coverage.add_argument("--game", choices=GAME_CODES)
    coverage.add_argument("--json", dest="json_path", metavar="PATH")
    coverage.add_argument("--csv", dest="csv_path", metavar="PATH")
    stats = sub.add_parser("stats")
    stats.add_argument("--game", choices=GAME_CODES, required=True)
    stats.add_argument("--years", metavar="Y1,Y2,...")
    stats.add_argument("--last-n", dest="last_n", type=int, metavar="N")
    stats.add_argument("--json", dest="json_path", metavar="PATH")
    stats.add_argument("--csv", dest="csv_path", metavar="PATH")
    browse = sub.add_parser("browse")
    browse.add_argument("--game", choices=GAME_CODES, required=True)
    browse.add_argument("--year", type=int)
    browse.add_argument("--number", type=int)
    browse.add_argument("--drawing", type=int, default=1)
    browse.add_argument("--date", metavar="YYYY-MM-DD")
    browse.add_argument("--find-number", dest="find_number", type=int, metavar="N")
    backtest = sub.add_parser("backtest")
    backtest.add_argument("--game", choices=GAME_CODES, required=True)
    backtest.add_argument("--strategy", default="all")
    backtest.add_argument("--years", metavar="Y1,Y2,...")
    backtest.add_argument("--from-date", dest="from_date", metavar="YYYY-MM-DD")
    backtest.add_argument("--to-date", dest="to_date", metavar="YYYY-MM-DD")
    backtest.add_argument("--last-n", dest="last_n", type=int, metavar="N")
    backtest.add_argument("--seed", type=int, metavar="N")
    backtest.add_argument("--json", dest="json_path", metavar="PATH")
    backtest.add_argument("--csv", dest="csv_path", metavar="PATH")
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
        if args.command == "coverage":
            return _run_coverage(database, args.game, args.json_path, args.csv_path)
        if args.command == "stats":
            return _run_stats(database, args.game, args.years, args.last_n, args.json_path, args.csv_path)
        if args.command == "browse":
            return _run_browse(
                database, args.game, args.year, args.number, args.drawing, args.date, args.find_number
            )
        if args.command == "backtest":
            return _run_backtest(
                database,
                args.game,
                args.strategy,
                args.years,
                args.from_date,
                args.to_date,
                args.last_n,
                args.seed,
                args.json_path,
                args.csv_path,
            )
        parser.error(f"unknown command {args.command!r}")
        return 2
    finally:
        database.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
