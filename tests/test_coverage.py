"""Coverage engine tests."""

from __future__ import annotations

import csv
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.database.engine import Database
from app.database.repository import DrawRepository, GameRepository
from app.models.domain import ParsedDraw
from app.services.coverage import CoverageService, expected_draw_dates
from app.services.validation import ValidationService

TODAY = date(2026, 7, 17)


def store(
    database: Database,
    game_code: str,
    number: int,
    draw_date: date,
    numbers: tuple[int, ...],
    drawing: int = 1,
    source: str = "live",
) -> None:
    with database.session() as session:
        game = GameRepository(session).by_code(game_code)
        DrawRepository(session).add_parsed(
            game,
            ParsedDraw(
                game_code=game_code,
                draw_number=number,
                draw_year=draw_date.year,
                draw_date=draw_date,
                drawing=drawing,
                numbers=numbers,
                jackpot_amount=Decimal("1000.00"),
                currency="EUR",
                source_url=f"test://{game_code}/{draw_date.year}-{number}#{drawing}",
            ),
            source=source,
        )


class TestExpectedDrawDates:
    def test_thursday_sunday_cadence_hand_verified(self) -> None:
        # 2026-01-01 is a Thursday; hand-verified Thu/Sun run through 01-18.
        dates = expected_draw_dates(2026, date(2026, 1, 20))
        assert dates == [
            date(2026, 1, 1),
            date(2026, 1, 4),
            date(2026, 1, 8),
            date(2026, 1, 11),
            date(2026, 1, 15),
            date(2026, 1, 18),
        ]

    def test_bounded_by_today_mid_week(self) -> None:
        # today falls between the Thu 01-08 and Sun 01-11 expected dates.
        dates = expected_draw_dates(2026, date(2026, 1, 10))
        assert dates == [date(2026, 1, 1), date(2026, 1, 4), date(2026, 1, 8)]

    def test_future_year_yields_nothing(self) -> None:
        assert expected_draw_dates(2027, date(2026, 1, 20)) == []

    def test_past_year_is_full_year(self) -> None:
        dates = expected_draw_dates(2024, date(2026, 1, 1))
        assert dates[0] == date(2024, 1, 4)  # first Thu/Sun of 2024 (Jan 1 is a Monday)
        assert dates[-1].year == 2024


class TestCoverageService:
    def test_empty_game_has_no_determinable_coverage(self, database: Database) -> None:
        report = CoverageService(database).analyze("6x49", today=TODAY)
        game = report.games[0]
        assert game.imported_sessions == 0
        assert game.expected_sessions is None
        assert game.coverage_percent is None
        assert game.earliest_date is None and game.latest_date is None
        assert game.missing_years == []
        assert game.missing_draw_numbers == {}

    def test_game_filter_returns_only_requested_game(self, database: Database) -> None:
        report = CoverageService(database).analyze("6x49", today=TODAY)
        assert [g.game_code for g in report.games] == ["6x49"]

        report_all = CoverageService(database).analyze(today=TODAY)
        assert {g.game_code for g in report_all.games} == {"6x49", "6x42", "5x35"}

    def test_missing_draw_numbers_and_dates(self, database: Database) -> None:
        store(database, "6x49", 1, date(2026, 1, 1), (1, 2, 3, 4, 5, 6))
        store(database, "6x49", 2, date(2026, 1, 4), (1, 2, 3, 4, 5, 7))
        # draw 3 (which would fall on 01-08) and the 01-11 Sunday are both
        # skipped; draw 4 resumes on 01-15.
        store(database, "6x49", 4, date(2026, 1, 15), (1, 2, 3, 4, 5, 8))
        report = CoverageService(database).analyze("6x49", today=TODAY)
        game = report.games[0]

        assert game.imported_sessions == 3
        assert game.missing_draw_numbers == {2026: [3]}
        assert game.missing_dates == {2026: [date(2026, 1, 8), date(2026, 1, 11)]}
        assert game.missing_years == []  # only one year is in span at all
        assert game.earliest_ref == "1/2026" and game.earliest_date == date(2026, 1, 1)
        assert game.latest_ref == "4/2026" and game.latest_date == date(2026, 1, 15)

    def test_missing_years_detected(self, database: Database) -> None:
        store(database, "6x49", 1, date(2023, 1, 1), (1, 2, 3, 4, 5, 6))
        store(database, "6x49", 1, date(2025, 1, 2), (1, 2, 3, 4, 5, 7))
        report = CoverageService(database).analyze("6x49", today=TODAY)
        game = report.games[0]
        assert game.missing_years == [2024]

    def test_two_drawing_session_counted_once(self, database: Database) -> None:
        store(database, "5x35", 12, date(2016, 3, 3), (2, 13, 30, 31, 33), drawing=1)
        store(database, "5x35", 12, date(2016, 3, 3), (2, 9, 24, 33, 34), drawing=2)
        report = CoverageService(database).analyze("5x35", today=TODAY)
        game = report.games[0]
        assert game.imported_sessions == 1
        assert game.imported_rows == 2
        assert game.two_drawing_sessions == 1

    def test_sources_breakdown(self, database: Database) -> None:
        store(database, "6x49", 1, date(2026, 1, 1), (1, 2, 3, 4, 5, 6), source="live")
        store(database, "6x49", 2, date(2026, 1, 4), (1, 2, 3, 4, 5, 7), source="wayback")
        store(database, "6x49", 4, date(2026, 1, 15), (1, 2, 3, 4, 5, 8), source="wayback-classic")
        report = CoverageService(database).analyze("6x49", today=TODAY)
        game = report.games[0]
        assert game.sources == {"live": 1, "wayback": 1, "wayback-classic": 1}

    def test_confidence_reflects_validation_status(self, database: Database) -> None:
        store(database, "6x49", 1, date(2026, 1, 1), (1, 2, 3, 4, 5, 6), source="live")
        store(database, "6x49", 2, date(2026, 1, 4), (1, 2, 3, 4, 5), source="wayback")  # wrong count
        ValidationService(database).validate("6x49", today=TODAY)
        report = CoverageService(database).analyze("6x49", today=TODAY)
        game = report.games[0]
        assert game.confidence["live"] == {"valid": 1}
        assert game.confidence["wayback"] == {"invalid": 1}

    def test_duplicate_sessions_detected(self, database: Database) -> None:
        store(database, "6x49", 1, date(2026, 1, 1), (1, 2, 3, 4, 5, 6))
        store(database, "6x49", 2, date(2026, 1, 1), (1, 2, 3, 4, 5, 6))  # same numbers+date
        report = CoverageService(database).analyze("6x49", today=TODAY)
        game = report.games[0]
        assert set(game.duplicate_sessions) == {"1/2026#1", "2/2026#1"}

    def test_coverage_percent_matches_expected_dates(self, database: Database) -> None:
        store(database, "6x49", 1, date(2026, 1, 1), (1, 2, 3, 4, 5, 6))
        report = CoverageService(database).analyze("6x49", today=date(2026, 1, 4))
        game = report.games[0]
        # Only 01-01 and 01-04 are expected within [start of year, today].
        assert game.expected_sessions == 2
        assert game.coverage_percent == pytest.approx(50.0)


class TestReportRendering:
    def test_to_text_contains_expected_sections(self, database: Database) -> None:
        store(database, "6x49", 1, date(2026, 1, 1), (1, 2, 3, 4, 5, 6))
        text = CoverageService(database).analyze("6x49", today=TODAY).to_text()
        assert "6/49" in text
        assert "Imported draws:" in text
        assert "Coverage:" in text
        assert "Earliest:" in text
        assert "Latest:" in text
        assert "Missing years:" in text
        assert "Missing draw numbers:" in text
        assert "Sources:" in text
        assert "Live:" in text
        assert "Wayback:" in text
        assert "Other:" in text

    def test_to_text_handles_no_games(self, database: Database) -> None:
        from app.services.coverage import CoverageReport
        from datetime import datetime, timezone

        text = CoverageReport(generated_at=datetime.now(timezone.utc), games=[]).to_text()
        assert "No games checked." in text

    def test_write_json_round_trips(self, database: Database, tmp_path: Path) -> None:
        store(database, "6x49", 1, date(2026, 1, 1), (1, 2, 3, 4, 5, 6))
        report = CoverageService(database).analyze("6x49", today=TODAY)
        out = tmp_path / "nested" / "coverage.json"
        report.write_json(out)
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["games"][0]["game_code"] == "6x49"
        assert payload["games"][0]["imported_sessions"] == 1

    def test_write_csv_has_one_row_per_game(self, database: Database, tmp_path: Path) -> None:
        store(database, "6x49", 1, date(2026, 1, 1), (1, 2, 3, 4, 5, 6))
        report = CoverageService(database).analyze(today=TODAY)
        out = tmp_path / "coverage.csv"
        report.write_csv(out)
        with out.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert {row["game_code"] for row in rows} == {"6x49", "6x42", "5x35"}
        six49 = next(r for r in rows if r["game_code"] == "6x49")
        assert six49["imported_sessions"] == "1"
