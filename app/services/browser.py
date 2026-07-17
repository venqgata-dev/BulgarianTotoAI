"""Historical draw browser.

Read-only navigation over the imported draws for one game: list years, list
draw numbers within a year, fetch one draw's full detail (numbers, jackpot,
prize tiers, provenance, validation status), step to the previous/next
drawing in chronological order, and search by draw number or calendar date.

Nothing here is persisted; it is a pure query layer over
:class:`app.database.repository.DrawRepository`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.database.engine import Database
from app.database.models import Draw
from app.database.repository import DrawRepository, GameRepository


@dataclass(slots=True)
class PrizeTierDetail:
    label: str
    match_count: int | None
    winners: int | None
    prize_amount: Decimal | None
    total_amount: Decimal | None
    currency: str | None

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "match_count": self.match_count,
            "winners": self.winners,
            "prize_amount": str(self.prize_amount) if self.prize_amount is not None else None,
            "total_amount": str(self.total_amount) if self.total_amount is not None else None,
            "currency": self.currency,
        }


@dataclass(slots=True)
class DrawDetail:
    """Everything the Historical Browser needs to display about one drawing."""

    game_code: str
    game_name: str
    ref: str
    draw_number: int
    draw_year: int
    drawing: int
    is_second_drawing: bool
    draw_date: date
    numbers: tuple[int, ...]
    bonus_numbers: tuple[int, ...]
    jackpot_amount: Decimal | None
    prize_pool_total: Decimal | None
    winners_total: int | None
    currency: str | None
    prize_tiers: list[PrizeTierDetail] = field(default_factory=list)
    source: str = ""
    source_url: str | None = None
    validation_status: str = "pending"

    @staticmethod
    def from_draw(game_code: str, game_name: str, draw: Draw) -> "DrawDetail":
        suffix = f"#{draw.drawing}" if draw.drawing != 1 else ""
        return DrawDetail(
            game_code=game_code,
            game_name=game_name,
            ref=f"{draw.draw_number}/{draw.draw_year}{suffix}",
            draw_number=draw.draw_number,
            draw_year=draw.draw_year,
            drawing=draw.drawing,
            is_second_drawing=draw.drawing == 2,
            draw_date=draw.draw_date,
            numbers=tuple(n.value for n in draw.numbers if not n.is_bonus),
            bonus_numbers=tuple(n.value for n in draw.numbers if n.is_bonus),
            jackpot_amount=draw.jackpot_amount,
            prize_pool_total=draw.prize_pool_total,
            winners_total=draw.winners_total,
            currency=draw.currency,
            prize_tiers=[
                PrizeTierDetail(
                    label=tier.label,
                    match_count=tier.match_count,
                    winners=tier.winners,
                    prize_amount=tier.prize_amount,
                    total_amount=tier.total_amount,
                    currency=tier.currency,
                )
                for tier in draw.prize_tiers
            ],
            source=draw.source,
            source_url=draw.source_url,
            validation_status=draw.validation_status.value,
        )

    def to_dict(self) -> dict:
        return {
            "game_code": self.game_code,
            "game_name": self.game_name,
            "ref": self.ref,
            "draw_number": self.draw_number,
            "draw_year": self.draw_year,
            "drawing": self.drawing,
            "is_second_drawing": self.is_second_drawing,
            "date": self.draw_date.isoformat(),
            "numbers": list(self.numbers),
            "bonus_numbers": list(self.bonus_numbers),
            "jackpot_amount": str(self.jackpot_amount) if self.jackpot_amount is not None else None,
            "prize_pool_total": str(self.prize_pool_total) if self.prize_pool_total is not None else None,
            "winners_total": self.winners_total,
            "currency": self.currency,
            "prize_tiers": [t.to_dict() for t in self.prize_tiers],
            "source": self.source,
            "source_url": self.source_url,
            "validation_status": self.validation_status,
        }


class HistoricalBrowserService:
    """Navigation/search over one game's imported history."""

    def __init__(self, database: Database) -> None:
        self._db = database

    def list_years(self, game_code: str) -> list[int]:
        with self._db.session() as session:
            game = GameRepository(session).by_code(game_code)
            draws = DrawRepository(session).all_for_game(game.id)
            return sorted({d.draw_year for d in draws})

    def list_draw_numbers(self, game_code: str, draw_year: int) -> list[int]:
        with self._db.session() as session:
            game = GameRepository(session).by_code(game_code)
            draws = DrawRepository(session).all_for_game(game.id)
            return sorted({d.draw_number for d in draws if d.draw_year == draw_year})

    def get(self, game_code: str, draw_year: int, draw_number: int, drawing: int = 1) -> DrawDetail | None:
        with self._db.session() as session:
            game = GameRepository(session).by_code(game_code)
            draw = DrawRepository(session).get(game.id, draw_year, draw_number, drawing)
            return DrawDetail.from_draw(game_code, game.name, draw) if draw is not None else None

    def latest(self, game_code: str) -> DrawDetail | None:
        """Most recent drawing on record, or ``None`` if the game has no draws yet."""
        with self._db.session() as session:
            game = GameRepository(session).by_code(game_code)
            draws = DrawRepository(session).all_for_game(game.id)
            if not draws:
                return None
            latest_draw = max(draws, key=lambda d: (d.draw_date, d.drawing))
            return DrawDetail.from_draw(game_code, game.name, latest_draw)

    def navigate(
        self, game_code: str, draw_year: int, draw_number: int, drawing: int, direction: str
    ) -> DrawDetail | None:
        """Step to the chronologically previous/next drawing, or ``None`` at either end."""
        if direction not in ("prev", "next"):
            raise ValueError(f"direction must be 'prev' or 'next', got {direction!r}")
        with self._db.session() as session:
            game = GameRepository(session).by_code(game_code)
            draws = DrawRepository(session).all_for_game(game.id)
            ordered = sorted(draws, key=lambda d: (d.draw_date, d.drawing))
            refs = [(d.draw_year, d.draw_number, d.drawing) for d in ordered]
            try:
                index = refs.index((draw_year, draw_number, drawing))
            except ValueError:
                return None
            new_index = index - 1 if direction == "prev" else index + 1
            if not 0 <= new_index < len(refs):
                return None
            return DrawDetail.from_draw(game_code, game.name, ordered[new_index])

    def search_by_draw_number(self, game_code: str, draw_number: int) -> list[DrawDetail]:
        """Every session (any year, any drawing) matching this draw number, oldest first."""
        with self._db.session() as session:
            game = GameRepository(session).by_code(game_code)
            draws = DrawRepository(session).all_for_game(game.id)
            matches = sorted(
                (d for d in draws if d.draw_number == draw_number), key=lambda d: (d.draw_date, d.drawing)
            )
            return [DrawDetail.from_draw(game_code, game.name, d) for d in matches]

    def search_by_date(self, game_code: str, target_date: date) -> list[DrawDetail]:
        """Every drawing (1 and/or 2) published on this calendar date."""
        with self._db.session() as session:
            game = GameRepository(session).by_code(game_code)
            draws = DrawRepository(session).all_for_game(game.id)
            matches = sorted((d for d in draws if d.draw_date == target_date), key=lambda d: d.drawing)
            return [DrawDetail.from_draw(game_code, game.name, d) for d in matches]
