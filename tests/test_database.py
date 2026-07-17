"""Database schema, seed and repository tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError

from app.database.engine import Database
from app.database.repository import DrawRepository, GameRepository, content_hash
from app.models.domain import ParsedDraw, ParsedPrizeTier


def make_parsed(number: int = 55, year: int = 2026, **overrides) -> ParsedDraw:
    defaults = dict(
        game_code="6x49",
        draw_number=number,
        draw_year=year,
        draw_date=date(2026, 7, 16),
        numbers=(5, 10, 17, 20, 42, 47),
        jackpot_amount=Decimal("2724436.24"),
        currency="EUR",
        prize_tiers=[
            ParsedPrizeTier("6 числа", 6, 0, Decimal("0.00"), Decimal("0.00"), "EUR"),
            ParsedPrizeTier("5 числа", 5, 19, Decimal("1281.80"), Decimal("24354.20"), "EUR"),
        ],
        source_url="https://info.toto.bg/results/6x49/2026-55",
    )
    defaults.update(overrides)
    return ParsedDraw(**defaults)


def test_games_seeded(database: Database) -> None:
    with database.session() as session:
        games = GameRepository(session).all_games()
    assert {g.code for g in games} == {"6x49", "6x42", "5x35"}
    six49 = next(g for g in games if g.code == "6x49")
    assert (six49.main_count, six49.main_min, six49.main_max) == (6, 1, 49)


def test_seeding_is_idempotent(database: Database) -> None:
    from app.database.seed import seed_games

    with database.session() as session:
        seed_games(session)  # second run
        assert len(GameRepository(session).all_games()) == 3


def test_store_and_read_draw(database: Database) -> None:
    with database.session() as session:
        game = GameRepository(session).by_code("6x49")
        DrawRepository(session).add_parsed(game, make_parsed(), source="live")
    with database.session() as session:
        game = GameRepository(session).by_code("6x49")
        repo = DrawRepository(session)
        draw = repo.get(game.id, 2026, 55)
        assert draw is not None
        assert [n.value for n in draw.numbers] == [5, 10, 17, 20, 42, 47]
        assert draw.day_of_week == 4  # 16.07.2026 is a Thursday
        assert draw.month == 7 and draw.year == 2026
        assert draw.winners_total == 19
        assert draw.prize_pool_total == Decimal("24354.20")
        assert draw.currency == "EUR"
        assert len(draw.prize_tiers) == 2
        assert repo.count(game.id) == 1


def test_duplicate_draw_rejected_by_constraint(database: Database) -> None:
    with database.session() as session:
        game = GameRepository(session).by_code("6x49")
        DrawRepository(session).add_parsed(game, make_parsed(), source="live")
    with pytest.raises(IntegrityError):
        with database.session() as session:
            game = GameRepository(session).by_code("6x49")
            DrawRepository(session).add_parsed(game, make_parsed(), source="wayback")


def test_two_drawings_of_same_session_allowed(database: Database) -> None:
    with database.session() as session:
        repo = DrawRepository(session)
        game = GameRepository(session).by_code("6x49")
        repo.add_parsed(game, make_parsed(drawing=1), source="wayback")
        repo.add_parsed(
            game, make_parsed(drawing=2, numbers=(1, 2, 3, 4, 6, 7)), source="wayback"
        )
    with database.session() as session:
        game = GameRepository(session).by_code("6x49")
        repo = DrawRepository(session)
        assert repo.count(game.id) == 2
        first = repo.get(game.id, 2026, 55, drawing=1)
        second = repo.get(game.id, 2026, 55, drawing=2)
        assert first is not None and second is not None
        assert [n.value for n in first.numbers] == [5, 10, 17, 20, 42, 47]
        assert [n.value for n in second.numbers] == [1, 2, 3, 4, 6, 7]


def test_duplicate_drawing_rejected_by_constraint(database: Database) -> None:
    with database.session() as session:
        game = GameRepository(session).by_code("6x49")
        DrawRepository(session).add_parsed(game, make_parsed(drawing=2), source="live")
    with pytest.raises(IntegrityError):
        with database.session() as session:
            game = GameRepository(session).by_code("6x49")
            DrawRepository(session).add_parsed(game, make_parsed(drawing=2), source="wayback")


def test_same_draw_number_allowed_across_years_and_games(database: Database) -> None:
    with database.session() as session:
        repo = DrawRepository(session)
        games = GameRepository(session)
        repo.add_parsed(games.by_code("6x49"), make_parsed(year=2026), source="live")
        repo.add_parsed(
            games.by_code("6x49"),
            make_parsed(year=2025, draw_date=date(2025, 7, 16)),
            source="live",
        )
        repo.add_parsed(games.by_code("6x42"), make_parsed(game_code="6x42"), source="live")
    with database.session() as session:
        assert DrawRepository(session).count() == 3


def test_content_hash_stable_and_sensitive(database: Database) -> None:
    assert content_hash(make_parsed()) == content_hash(make_parsed())
    changed = make_parsed(numbers=(1, 2, 3, 4, 5, 6))
    assert content_hash(changed) != content_hash(make_parsed())
    other_drawing = make_parsed(drawing=2)
    assert content_hash(other_drawing) != content_hash(make_parsed())
