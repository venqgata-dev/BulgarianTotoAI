"""Framework-free domain objects shared between scraper, database and services."""

from app.models.domain import GameDefinition, ParsedDraw, ParsedPrizeTier, SUPPORTED_GAMES

__all__ = ["GameDefinition", "ParsedDraw", "ParsedPrizeTier", "SUPPORTED_GAMES"]
