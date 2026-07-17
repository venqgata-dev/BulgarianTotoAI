"""SQLAlchemy ORM models.

Schema notes
------------
* The official draw numbering restarts at 1 every calendar year, so a draw is
  uniquely identified by ``(game, draw_year, draw_number)``.
* Monetary values are stored as ``Numeric`` plus an ISO currency code column.
  Bulgaria switched from BGN to EUR during the covered period, so amounts from
  different years carry different currencies and must never be mixed blindly.
* ``draws.day_of_week``/``month``/``year`` are denormalised from ``draw_date``
  for cheap filtering and grouping in the analysis milestones.
* Winning numbers live in the ``draw_numbers`` child table (one row per ball)
  so per-number statistics are plain SQL. ``is_bonus`` is reserved for games
  with bonus balls; none of the three supported games currently draws one.
* Until the mid-2010s one draw session of 6/49 and 5/35 comprised **two
  drawings** ("I-во теглене" / "II-ро теглене"). Each drawing is stored as its
  own row distinguished by ``drawing`` (1 or 2); modern draws are always 1.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class ValidationStatus(enum.Enum):
    PENDING = "pending"
    VALID = "valid"
    WARNING = "warning"
    INVALID = "invalid"


class Game(Base):
    """A lottery game (6/49, 6/42, 5/35, ...)."""

    __tablename__ = "games"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True)
    name: Mapped[str] = mapped_column(String(64))
    main_count: Mapped[int] = mapped_column(Integer)
    main_min: Mapped[int] = mapped_column(Integer)
    main_max: Mapped[int] = mapped_column(Integer)
    bonus_count: Mapped[int] = mapped_column(Integer, default=0)
    bonus_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bonus_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    draws: Mapped[list["Draw"]] = relationship(back_populates="game")

    def __repr__(self) -> str:
        return f"<Game {self.code}>"


class Draw(Base):
    """One official draw of one game."""

    __tablename__ = "draws"
    __table_args__ = (
        UniqueConstraint(
            "game_id", "draw_year", "draw_number", "drawing", name="uq_draw_game_year_number"
        ),
        Index("ix_draws_game_date", "game_id", "draw_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)
    draw_number: Mapped[int] = mapped_column(Integer)
    draw_year: Mapped[int] = mapped_column(Integer)
    drawing: Mapped[int] = mapped_column(Integer, default=1)  # historical II-ро теглене = 2
    draw_date: Mapped[date] = mapped_column(Date, index=True)

    # Denormalised calendar fields (draw_date is authoritative).
    day_of_week: Mapped[int] = mapped_column(Integer)  # ISO: 1=Monday .. 7=Sunday
    month: Mapped[int] = mapped_column(Integer)
    year: Mapped[int] = mapped_column(Integer)

    jackpot_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    prize_pool_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    winners_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)

    source_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Provenance labels: live | wayback-info | wayback-classic | wayback-home-legacy
    # | wayback-home-modern (see docs/RESEARCH.md)
    source: Mapped[str] = mapped_column(String(32), default="live")
    content_hash: Mapped[str] = mapped_column(String(64))
    validation_status: Mapped[ValidationStatus] = mapped_column(
        Enum(ValidationStatus, values_callable=lambda e: [m.value for m in e]),
        default=ValidationStatus.PENDING,
    )
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    extra: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON for source oddities

    game: Mapped[Game] = relationship(back_populates="draws")
    numbers: Mapped[list["DrawNumber"]] = relationship(
        back_populates="draw", cascade="all, delete-orphan", order_by="DrawNumber.position"
    )
    prize_tiers: Mapped[list["PrizeTier"]] = relationship(
        back_populates="draw", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Draw {self.draw_number}/{self.draw_year} game_id={self.game_id}>"


class DrawNumber(Base):
    """A single drawn ball."""

    __tablename__ = "draw_numbers"
    __table_args__ = (
        UniqueConstraint("draw_id", "is_bonus", "position", name="uq_drawnumber_slot"),
        Index("ix_draw_numbers_value", "value"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    draw_id: Mapped[int] = mapped_column(ForeignKey("draws.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)  # order as published (site shows ascending)
    value: Mapped[int] = mapped_column(Integer)
    is_bonus: Mapped[bool] = mapped_column(Boolean, default=False)

    draw: Mapped[Draw] = relationship(back_populates="numbers")


class PrizeTier(Base):
    """One row of the official winnings table of a draw ("6 числа", ...)."""

    __tablename__ = "prize_tiers"
    __table_args__ = (UniqueConstraint("draw_id", "label", name="uq_prizetier_draw_label"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    draw_id: Mapped[int] = mapped_column(ForeignKey("draws.id"), index=True)
    label: Mapped[str] = mapped_column(String(32))  # verbatim, e.g. "6 числа"
    match_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    winners: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prize_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)

    draw: Mapped[Draw] = relationship(back_populates="prize_tiers")


class ScrapeCheckpoint(Base):
    """Progress marker so interrupted imports resume where they stopped.

    ``segment`` identifies one unit of scraping work, e.g. a single draw URL
    or a Wayback snapshot URL.
    """

    __tablename__ = "scrape_checkpoints"
    __table_args__ = (UniqueConstraint("game_id", "segment", name="uq_checkpoint_game_segment"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)
    segment: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(String(16), default="done")  # done | failed | skipped
    detail: Mapped[str | None] = mapped_column(String(512), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ValidationRun(Base):
    """One execution of the validation pipeline."""

    __tablename__ = "validation_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int | None] = mapped_column(ForeignKey("games.id"), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    draws_checked: Mapped[int] = mapped_column(Integer, default=0)
    issues_found: Mapped[int] = mapped_column(Integer, default=0)

    issues: Mapped[list["ValidationIssue"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class ValidationIssue(Base):
    """A single problem discovered by validation."""

    __tablename__ = "validation_issues"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("validation_runs.id"), index=True)
    game_id: Mapped[int | None] = mapped_column(ForeignKey("games.id"), nullable=True)
    draw_id: Mapped[int | None] = mapped_column(ForeignKey("draws.id"), nullable=True)
    issue_type: Mapped[str] = mapped_column(String(48))
    severity: Mapped[str] = mapped_column(String(16), default="error")  # error | warning
    description: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    run: Mapped[ValidationRun] = relationship(back_populates="issues")
