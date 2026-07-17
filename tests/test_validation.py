"""Validation pipeline tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.database.engine import Database
from app.database.models import ValidationStatus
from app.database.repository import DrawRepository, GameRepository
from app.models.domain import ParsedDraw
from app.services.validation import (
    ISSUE_BROKEN_SEQUENCE,
    ISSUE_DUPLICATE_CONTENT,
    ISSUE_DUPLICATE_DRAW,
    ISSUE_FUTURE_DATE,
    ISSUE_IMPOSSIBLE_NUMBERING,
    ISSUE_INVALID_DRAWING,
    ISSUE_MISSING_DRAWS,
    ISSUE_MISSING_SECOND_DRAWING,
    ISSUE_OUT_OF_RANGE,
    ISSUE_REPEATED_NUMBER,
    ISSUE_WRONG_COUNT,
    ValidationService,
    duplicate_number_groups,
    missing_numbers_by_year,
)

TODAY = date(2026, 7, 17)


def store(
    database: Database,
    game_code: str,
    number: int,
    draw_date: date,
    numbers: tuple[int, ...],
    drawing: int = 1,
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
            source="live",
        )


def issues_of(report, game_code: str, issue_type: str):
    game = next(g for g in report.games if g.game_code == game_code)
    return [i for i in game.issues if i.issue_type == issue_type]


def test_clean_data_passes(database: Database) -> None:
    store(database, "6x49", 54, date(2026, 7, 12), (8, 14, 35, 39, 42, 49))
    store(database, "6x49", 55, date(2026, 7, 16), (5, 10, 17, 20, 42, 47))
    report = ValidationService(database).validate("6x49", today=TODAY)
    game = report.games[0]
    assert game.draws_checked == 2
    assert game.error_count == 0
    assert game.warning_count == 0
    with database.session() as session:
        g = GameRepository(session).by_code("6x49")
        for draw in DrawRepository(session).all_for_game(g.id):
            assert draw.validation_status is ValidationStatus.VALID


def test_wrong_count_detected(database: Database) -> None:
    store(database, "6x49", 55, date(2026, 7, 16), (5, 10, 17, 20, 42))  # only 5
    report = ValidationService(database).validate("6x49", today=TODAY)
    assert issues_of(report, "6x49", ISSUE_WRONG_COUNT)


def test_out_of_range_detected(database: Database) -> None:
    store(database, "5x35", 55, date(2026, 7, 16), (1, 2, 3, 4, 36))  # 36 > 35
    report = ValidationService(database).validate("5x35", today=TODAY)
    assert issues_of(report, "5x35", ISSUE_OUT_OF_RANGE)


def test_repeated_number_detected(database: Database) -> None:
    store(database, "6x49", 55, date(2026, 7, 16), (5, 5, 17, 20, 42, 47))
    report = ValidationService(database).validate("6x49", today=TODAY)
    assert issues_of(report, "6x49", ISSUE_REPEATED_NUMBER)


def test_future_date_detected(database: Database) -> None:
    store(database, "6x49", 60, date(2026, 8, 1), (5, 10, 17, 20, 42, 47))
    report = ValidationService(database).validate("6x49", today=TODAY)
    assert issues_of(report, "6x49", ISSUE_FUTURE_DATE)


def test_gap_detected_as_warning(database: Database) -> None:
    store(database, "6x49", 53, date(2026, 7, 9), (1, 2, 3, 4, 5, 6))
    store(database, "6x49", 55, date(2026, 7, 16), (5, 10, 17, 20, 42, 47))  # 54 missing
    report = ValidationService(database).validate("6x49", today=TODAY)
    gaps = issues_of(report, "6x49", ISSUE_MISSING_DRAWS)
    assert len(gaps) == 1
    assert gaps[0].severity == "warning"
    assert "54" in gaps[0].description


def test_two_drawings_same_date_pass_clean(database: Database) -> None:
    store(database, "5x35", 12, date(2016, 3, 3), (2, 13, 30, 31, 33), drawing=1)
    store(database, "5x35", 12, date(2016, 3, 3), (2, 9, 24, 33, 34), drawing=2)
    report = ValidationService(database).validate("5x35", today=TODAY)
    game = report.games[0]
    assert game.draws_checked == 2
    assert game.error_count == 0
    assert not issues_of(report, "5x35", ISSUE_DUPLICATE_DRAW)
    assert not issues_of(report, "5x35", ISSUE_BROKEN_SEQUENCE)


def test_two_drawings_with_different_dates_flagged(database: Database) -> None:
    store(database, "5x35", 12, date(2016, 3, 3), (2, 13, 30, 31, 33), drawing=1)
    store(database, "5x35", 12, date(2016, 3, 6), (2, 9, 24, 33, 34), drawing=2)
    report = ValidationService(database).validate("5x35", today=TODAY)
    assert issues_of(report, "5x35", ISSUE_BROKEN_SEQUENCE)


def test_broken_sequence_detected(database: Database) -> None:
    store(database, "6x49", 54, date(2026, 7, 16), (1, 2, 3, 4, 5, 6))
    store(database, "6x49", 55, date(2026, 7, 12), (5, 10, 17, 20, 42, 47))  # earlier date
    report = ValidationService(database).validate("6x49", today=TODAY)
    assert issues_of(report, "6x49", ISSUE_BROKEN_SEQUENCE)


def test_run_and_issues_persisted(database: Database) -> None:
    store(database, "6x49", 55, date(2026, 7, 16), (5, 10, 17, 20, 42))
    report = ValidationService(database).validate("6x49", today=TODAY)
    assert report.run_id is not None
    from sqlalchemy import select

    from app.database.models import ValidationIssue, ValidationRun

    with database.session() as session:
        run = session.get(ValidationRun, report.run_id)
        assert run is not None and run.finished_at is not None
        issues = list(session.scalars(select(ValidationIssue).where(ValidationIssue.run_id == run.id)))
        assert run.issues_found == len(issues) > 0


def test_report_text_renders(database: Database) -> None:
    store(database, "6x49", 55, date(2026, 7, 16), (5, 10, 17, 20, 42, 47))
    text = ValidationService(database).validate(today=TODAY).to_text()
    assert "VALIDATION REPORT" in text
    assert "6x49" in text


def test_impossible_numbering_detected(database: Database) -> None:
    store(database, "6x49", 0, date(2026, 7, 16), (5, 10, 17, 20, 42, 47))
    report = ValidationService(database).validate("6x49", today=TODAY)
    assert issues_of(report, "6x49", ISSUE_IMPOSSIBLE_NUMBERING)


def test_invalid_drawing_number_detected(database: Database) -> None:
    store(database, "6x49", 55, date(2026, 7, 16), (5, 10, 17, 20, 42, 47), drawing=3)
    report = ValidationService(database).validate("6x49", today=TODAY)
    issues = issues_of(report, "6x49", ISSUE_INVALID_DRAWING)
    assert issues
    assert "55/2026#3" in issues[0].draw_ref


def test_missing_second_drawing_detected_when_year_format_established(database: Database) -> None:
    store(database, "5x35", 12, date(2016, 3, 3), (2, 13, 30, 31, 33), drawing=1)
    store(database, "5x35", 12, date(2016, 3, 3), (2, 9, 24, 33, 34), drawing=2)
    store(database, "5x35", 13, date(2016, 3, 6), (1, 2, 3, 4, 5), drawing=1)  # no drawing 2
    report = ValidationService(database).validate("5x35", today=TODAY)
    issues = issues_of(report, "5x35", ISSUE_MISSING_SECOND_DRAWING)
    assert len(issues) == 1
    assert issues[0].draw_ref == "13/2016"


def test_missing_second_drawing_not_flagged_when_year_never_had_one(database: Database) -> None:
    store(database, "6x49", 54, date(2026, 7, 12), (8, 14, 35, 39, 42, 49))
    store(database, "6x49", 55, date(2026, 7, 16), (5, 10, 17, 20, 42, 47))
    report = ValidationService(database).validate("6x49", today=TODAY)
    assert not issues_of(report, "6x49", ISSUE_MISSING_SECOND_DRAWING)


def test_duplicate_content_detected(database: Database) -> None:
    store(database, "6x49", 1, date(2026, 1, 1), (1, 2, 3, 4, 5, 6))
    store(database, "6x49", 2, date(2026, 1, 1), (1, 2, 3, 4, 5, 6))  # identical numbers+date
    report = ValidationService(database).validate("6x49", today=TODAY)
    issues = issues_of(report, "6x49", ISSUE_DUPLICATE_CONTENT)
    assert len(issues) == 1
    assert "1/2026#1" in issues[0].description and "2/2026#1" in issues[0].description


def test_missing_numbers_by_year_helper() -> None:
    from app.database.models import Draw

    draws = [
        Draw(draw_year=2026, draw_number=1),
        Draw(draw_year=2026, draw_number=3),
        Draw(draw_year=2025, draw_number=5),
    ]
    assert missing_numbers_by_year(draws) == {2026: [2]}


def test_duplicate_number_groups_helper_allows_distinct_drawings(database: Database) -> None:
    # A legitimate two-drawing session (different numbers per drawing, the
    # normal case) must not be reported as a content duplicate.
    store(database, "5x35", 12, date(2016, 3, 3), (2, 13, 30, 31, 33), drawing=1)
    store(database, "5x35", 12, date(2016, 3, 3), (2, 9, 24, 33, 34), drawing=2)
    with database.session() as session:
        game = GameRepository(session).by_code("5x35")
        draws = DrawRepository(session).all_for_game(game.id)
        groups = duplicate_number_groups(draws)
    assert groups == []


def test_duplicate_number_groups_helper_flags_identical_drawings_too(database: Database) -> None:
    # Two rows of the *same* session publishing identical numbers is just as
    # suspicious as two different sessions doing so (e.g. a parsing bug that
    # copied drawing 1's numbers into drawing 2's row) and must be flagged.
    store(database, "5x35", 12, date(2016, 3, 3), (2, 13, 30, 31, 33), drawing=1)
    store(database, "5x35", 12, date(2016, 3, 3), (2, 13, 30, 31, 33), drawing=2)
    with database.session() as session:
        game = GameRepository(session).by_code("5x35")
        draws = DrawRepository(session).all_for_game(game.id)
        groups = duplicate_number_groups(draws)
    assert len(groups) == 1 and len(groups[0]) == 2
