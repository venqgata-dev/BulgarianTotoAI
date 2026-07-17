"""Seed the games table from the domain game definitions."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import Game
from app.models.domain import SUPPORTED_GAMES
from app.services.logging_service import get_logger


def seed_games(session: Session) -> None:
    """Insert missing supported games; existing rows are left untouched."""
    log = get_logger("database")
    existing = {g.code for g in session.scalars(select(Game))}
    for definition in SUPPORTED_GAMES:
        if definition.code in existing:
            continue
        session.add(
            Game(
                code=definition.code,
                name=definition.name,
                main_count=definition.main_count,
                main_min=definition.main_min,
                main_max=definition.main_max,
                bonus_count=definition.bonus_count,
                bonus_min=definition.bonus_min,
                bonus_max=definition.bonus_max,
            )
        )
        log.info("Seeded game %s", definition.code)
