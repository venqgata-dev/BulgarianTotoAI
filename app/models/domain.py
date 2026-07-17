"""Domain objects describing games and scraped draw results.

These are plain dataclasses with no SQLAlchemy or Qt dependencies so they can
be used freely by the scraper, validation and (later) analysis layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class GameDefinition:
    """Static rules of one lottery game.

    ``code`` matches the identifier used in official result URLs
    (https://info.toto.bg/results/<code>).
    """

    code: str
    name: str
    main_count: int
    main_min: int
    main_max: int
    bonus_count: int = 0
    bonus_min: int | None = None
    bonus_max: int | None = None

    def is_valid_main_number(self, value: int) -> bool:
        return self.main_min <= value <= self.main_max


#: The three games covered by this application. The official draw numbering
#: restarts at 1 every calendar year; all Toto 2 games are drawn together
#: (currently Thursdays and Sundays), so draw numbers align across games.
SUPPORTED_GAMES: tuple[GameDefinition, ...] = (
    GameDefinition(code="6x49", name="Тото 2 - 6 от 49", main_count=6, main_min=1, main_max=49),
    GameDefinition(code="6x42", name="Тото 2 - 6 от 42", main_count=6, main_min=1, main_max=42),
    GameDefinition(code="5x35", name="Тото 2 - 5 от 35", main_count=5, main_min=1, main_max=35),
)


def game_by_code(code: str) -> GameDefinition:
    for game in SUPPORTED_GAMES:
        if game.code == code:
            return game
    raise KeyError(f"Unknown game code: {code!r}")


@dataclass(slots=True)
class ParsedPrizeTier:
    """One row of the winnings table of a draw page ("6 числа", ...)."""

    label: str
    match_count: int | None
    winners: int | None
    prize_amount: Decimal | None
    total_amount: Decimal | None
    currency: str | None  # ISO code: "BGN" or "EUR"


@dataclass(slots=True)
class ParsedDraw:
    """A fully parsed draw page, not yet persisted."""

    game_code: str
    draw_number: int
    draw_year: int
    draw_date: date
    numbers: tuple[int, ...]
    drawing: int = 1  # historical draws had a second drawing ("II-ро теглене")
    bonus_numbers: tuple[int, ...] = ()
    jackpot_amount: Decimal | None = None
    currency: str | None = None
    prize_tiers: list[ParsedPrizeTier] = field(default_factory=list)
    source_url: str = ""

    @property
    def official_ref(self) -> str:
        """Official draw reference, e.g. ``55/2026`` (``55/2026#2`` for a second drawing)."""
        suffix = f"#{self.drawing}" if self.drawing != 1 else ""
        return f"{self.draw_number}/{self.draw_year}{suffix}"
