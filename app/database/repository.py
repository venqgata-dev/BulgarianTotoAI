"""Repositories: all persistence queries live here, not in services."""

from __future__ import annotations

import hashlib

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.database.models import (
    Draw,
    DrawNumber,
    Game,
    PrizeTier,
    ScrapeCheckpoint,
    ValidationStatus,
)
from app.models.domain import ParsedDraw


def content_hash(parsed: ParsedDraw) -> str:
    """Stable hash of the draw's identifying content for duplicate detection."""
    payload = "|".join(
        [
            parsed.game_code,
            str(parsed.draw_year),
            str(parsed.draw_number),
            parsed.draw_date.isoformat(),
            ",".join(map(str, parsed.numbers)),
            ",".join(map(str, parsed.bonus_numbers)),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class GameRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def by_code(self, code: str) -> Game:
        game = self._session.scalar(select(Game).where(Game.code == code))
        if game is None:
            raise LookupError(f"Game {code!r} is not seeded in the database")
        return game

    def all_games(self) -> list[Game]:
        return list(self._session.scalars(select(Game).order_by(Game.code)))


class DrawRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, game_id: int, draw_year: int, draw_number: int) -> Draw | None:
        return self._session.scalar(
            select(Draw).where(
                Draw.game_id == game_id,
                Draw.draw_year == draw_year,
                Draw.draw_number == draw_number,
            )
        )

    def exists(self, game_id: int, draw_year: int, draw_number: int) -> bool:
        return self.get(game_id, draw_year, draw_number) is not None

    def count(self, game_id: int | None = None) -> int:
        stmt = select(func.count(Draw.id))
        if game_id is not None:
            stmt = stmt.where(Draw.game_id == game_id)
        return int(self._session.scalar(stmt) or 0)

    def all_for_game(self, game_id: int) -> list[Draw]:
        return list(
            self._session.scalars(
                select(Draw)
                .where(Draw.game_id == game_id)
                .options(selectinload(Draw.numbers))
                .order_by(Draw.draw_year, Draw.draw_number)
            )
        )

    def add_parsed(self, game: Game, parsed: ParsedDraw, source: str) -> Draw:
        """Persist a parsed draw with its numbers and prize tiers."""
        winners = [t.winners for t in parsed.prize_tiers if t.winners is not None]
        totals = [t.total_amount for t in parsed.prize_tiers if t.total_amount is not None]
        draw = Draw(
            game_id=game.id,
            draw_number=parsed.draw_number,
            draw_year=parsed.draw_year,
            draw_date=parsed.draw_date,
            day_of_week=parsed.draw_date.isoweekday(),
            month=parsed.draw_date.month,
            year=parsed.draw_date.year,
            jackpot_amount=parsed.jackpot_amount,
            prize_pool_total=sum(totals) if totals else None,
            winners_total=sum(winners) if winners else None,
            currency=parsed.currency,
            source_url=parsed.source_url or None,
            source=source,
            content_hash=content_hash(parsed),
            validation_status=ValidationStatus.PENDING,
            extra=None,
        )
        for position, value in enumerate(parsed.numbers, start=1):
            draw.numbers.append(DrawNumber(position=position, value=value, is_bonus=False))
        for position, value in enumerate(parsed.bonus_numbers, start=1):
            draw.numbers.append(DrawNumber(position=position, value=value, is_bonus=True))
        for tier in parsed.prize_tiers:
            draw.prize_tiers.append(
                PrizeTier(
                    label=tier.label,
                    match_count=tier.match_count,
                    winners=tier.winners,
                    prize_amount=tier.prize_amount,
                    total_amount=tier.total_amount,
                    currency=tier.currency,
                )
            )
        self._session.add(draw)
        return draw


class CheckpointRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def is_done(self, game_id: int, segment: str) -> bool:
        checkpoint = self._session.scalar(
            select(ScrapeCheckpoint).where(
                ScrapeCheckpoint.game_id == game_id,
                ScrapeCheckpoint.segment == segment,
            )
        )
        return checkpoint is not None and checkpoint.status in ("done", "skipped")

    def mark(self, game_id: int, segment: str, status: str, detail: str | None = None) -> None:
        checkpoint = self._session.scalar(
            select(ScrapeCheckpoint).where(
                ScrapeCheckpoint.game_id == game_id,
                ScrapeCheckpoint.segment == segment,
            )
        )
        if checkpoint is None:
            checkpoint = ScrapeCheckpoint(game_id=game_id, segment=segment)
            self._session.add(checkpoint)
        checkpoint.status = status
        checkpoint.detail = detail
