"""Historical browser service tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.database.engine import Database
from app.database.repository import DrawRepository, GameRepository
from app.models.domain import ParsedDraw, ParsedPrizeTier
from app.services.browser import HistoricalBrowserService


def store(
    database: Database,
    game_code: str,
    number: int,
    draw_date: date,
    numbers: tuple[int, ...],
    drawing: int = 1,
    source: str = "live",
    prize_tiers: list[ParsedPrizeTier] | None = None,
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
                jackpot_amount=Decimal("2724436.24"),
                currency="EUR",
                prize_tiers=prize_tiers or [],
                source_url=f"test://{game_code}/{draw_date.year}-{number}#{drawing}",
            ),
            source=source,
        )


def seed_three_draws(database: Database) -> None:
    store(database, "6x49", 54, date(2026, 7, 12), (8, 14, 35, 39, 42, 49), source="wayback")
    store(
        database,
        "6x49",
        55,
        date(2026, 7, 16),
        (5, 10, 17, 20, 42, 47),
        source="live",
        prize_tiers=[
            ParsedPrizeTier("6 числа", 6, 0, Decimal("0.00"), Decimal("0.00"), "EUR"),
            ParsedPrizeTier("5 числа", 5, 19, Decimal("1281.80"), Decimal("24354.20"), "EUR"),
        ],
    )
    store(database, "6x49", 56, date(2026, 7, 19), (1, 2, 3, 4, 5, 6), source="live")


class TestListing:
    def test_list_years(self, database: Database) -> None:
        seed_three_draws(database)
        store(database, "6x49", 1, date(2025, 1, 2), (1, 2, 3, 4, 5, 6))
        assert HistoricalBrowserService(database).list_years("6x49") == [2025, 2026]

    def test_list_draw_numbers(self, database: Database) -> None:
        seed_three_draws(database)
        assert HistoricalBrowserService(database).list_draw_numbers("6x49", 2026) == [54, 55, 56]

    def test_list_draw_numbers_empty_year(self, database: Database) -> None:
        seed_three_draws(database)
        assert HistoricalBrowserService(database).list_draw_numbers("6x49", 1999) == []


class TestGet:
    def test_get_returns_full_detail(self, database: Database) -> None:
        seed_three_draws(database)
        detail = HistoricalBrowserService(database).get("6x49", 2026, 55, 1)
        assert detail is not None
        assert detail.ref == "55/2026"
        assert detail.numbers == (5, 10, 17, 20, 42, 47)
        assert detail.jackpot_amount == Decimal("2724436.24")
        assert detail.currency == "EUR"
        assert detail.source == "live"
        assert detail.validation_status == "pending"  # validate() has not run
        assert detail.is_second_drawing is False
        assert len(detail.prize_tiers) == 2
        assert detail.prize_tiers[0].label == "6 числа"
        assert detail.prize_tiers[0].winners == 0
        assert detail.prize_tiers[1].winners == 19

    def test_get_missing_returns_none(self, database: Database) -> None:
        seed_three_draws(database)
        assert HistoricalBrowserService(database).get("6x49", 2026, 999, 1) is None

    def test_get_second_drawing_flagged(self, database: Database) -> None:
        store(database, "5x35", 12, date(2016, 3, 3), (2, 13, 30, 31, 33), drawing=1)
        store(database, "5x35", 12, date(2016, 3, 3), (2, 9, 24, 33, 34), drawing=2)
        detail = HistoricalBrowserService(database).get("5x35", 2016, 12, 2)
        assert detail is not None
        assert detail.is_second_drawing is True
        assert detail.ref == "12/2016#2"


class TestLatest:
    def test_latest_returns_most_recent(self, database: Database) -> None:
        seed_three_draws(database)
        detail = HistoricalBrowserService(database).latest("6x49")
        assert detail is not None
        assert detail.ref == "56/2026"

    def test_latest_none_when_empty(self, database: Database) -> None:
        assert HistoricalBrowserService(database).latest("6x49") is None


class TestNavigate:
    def test_prev_and_next(self, database: Database) -> None:
        seed_three_draws(database)
        service = HistoricalBrowserService(database)
        current = service.get("6x49", 2026, 55, 1)
        prev_draw = service.navigate("6x49", current.draw_year, current.draw_number, current.drawing, "prev")
        next_draw = service.navigate("6x49", current.draw_year, current.draw_number, current.drawing, "next")
        assert prev_draw.ref == "54/2026"
        assert next_draw.ref == "56/2026"

    def test_prev_at_start_is_none(self, database: Database) -> None:
        seed_three_draws(database)
        service = HistoricalBrowserService(database)
        result = service.navigate("6x49", 2026, 54, 1, "prev")
        assert result is None

    def test_next_at_end_is_none(self, database: Database) -> None:
        seed_three_draws(database)
        service = HistoricalBrowserService(database)
        result = service.navigate("6x49", 2026, 56, 1, "next")
        assert result is None

    def test_navigate_unknown_ref_is_none(self, database: Database) -> None:
        seed_three_draws(database)
        service = HistoricalBrowserService(database)
        assert service.navigate("6x49", 2099, 1, 1, "next") is None

    def test_navigate_rejects_bad_direction(self, database: Database) -> None:
        seed_three_draws(database)
        service = HistoricalBrowserService(database)
        try:
            service.navigate("6x49", 2026, 55, 1, "sideways")
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")

    def test_navigate_steps_between_drawings_of_one_session(self, database: Database) -> None:
        store(database, "5x35", 12, date(2016, 3, 3), (2, 13, 30, 31, 33), drawing=1)
        store(database, "5x35", 12, date(2016, 3, 3), (2, 9, 24, 33, 34), drawing=2)
        store(database, "5x35", 13, date(2016, 3, 6), (1, 2, 3, 4, 5), drawing=1)
        service = HistoricalBrowserService(database)
        second = service.navigate("5x35", 2016, 12, 1, "next")
        assert second.ref == "12/2016#2"
        third = service.navigate("5x35", 2016, 12, 2, "next")
        assert third.ref == "13/2016"


class TestSearch:
    def test_search_by_date_single_match(self, database: Database) -> None:
        seed_three_draws(database)
        matches = HistoricalBrowserService(database).search_by_date("6x49", date(2026, 7, 16))
        assert [m.ref for m in matches] == ["55/2026"]

    def test_search_by_date_two_drawings(self, database: Database) -> None:
        store(database, "5x35", 12, date(2016, 3, 3), (2, 13, 30, 31, 33), drawing=1)
        store(database, "5x35", 12, date(2016, 3, 3), (2, 9, 24, 33, 34), drawing=2)
        matches = HistoricalBrowserService(database).search_by_date("5x35", date(2016, 3, 3))
        assert [m.drawing for m in matches] == [1, 2]

    def test_search_by_date_no_match(self, database: Database) -> None:
        seed_three_draws(database)
        assert HistoricalBrowserService(database).search_by_date("6x49", date(2020, 1, 1)) == []

    def test_search_by_draw_number_across_years(self, database: Database) -> None:
        seed_three_draws(database)
        store(database, "6x49", 55, date(2025, 7, 17), (1, 2, 3, 4, 5, 6))
        matches = HistoricalBrowserService(database).search_by_draw_number("6x49", 55)
        assert [m.draw_year for m in matches] == [2025, 2026]
