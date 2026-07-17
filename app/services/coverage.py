"""Historical coverage and provenance reporting.

Turns the imported ``draws`` table into an auditable measurement of how much
of each game's history is actually present, where it came from, and how
confident we are in it - a read-only companion to
:mod:`app.services.validation` (which flags per-draw *correctness* problems;
this module measures *completeness*).

Expected-draw-count model
--------------------------
The official cadence is twice a week, Thursday and Sunday (see
docs/RESEARCH.md). "Expected" draw sessions for a year are therefore every
Thursday/Sunday in that year, bounded by *today* for the current year. This
is only computed for years the game already has at least one imported draw
in - i.e. the span ``[earliest_year, latest_year]`` - so coverage is never
computed against decades of pre-2023 history that docs/RESEARCH.md says is
not recoverable from any current source. That is what "where determinable"
means throughout this module: a game with zero imported draws has no
determinable expected count at all.

A "session" is one (game, draw_year, draw_number); a historical session can
have one or two drawing rows (see app/database/models.py). Counts here are
session counts unless a field name says otherwise, so a two-drawing session
does not inflate coverage above what a single-drawing session would.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.database.engine import Database
from app.database.models import Draw, ValidationStatus
from app.database.repository import DrawRepository, GameRepository
from app.services.logging_service import get_logger
from app.services.validation import duplicate_number_groups, missing_numbers_by_year

#: Thursday=3, Sunday=6 (datetime.date.weekday(): Monday=0 .. Sunday=6).
_DRAW_WEEKDAYS = (3, 6)


def expected_draw_dates(year: int, today: date) -> list[date]:
    """Calendar dates a draw session is expected on on the documented
    twice-weekly (Thu/Sun) cadence, bounded to ``today``: future years (and
    the remainder of the current year) contribute no expected dates yet."""
    if year > today.year:
        return []
    start = date(year, 1, 1)
    end = date(year, 12, 31) if year < today.year else today
    if start > end:
        return []
    span = (end - start).days
    return [
        start + timedelta(days=offset)
        for offset in range(span + 1)
        if (start + timedelta(days=offset)).weekday() in _DRAW_WEEKDAYS
    ]


def display_code(game_code: str) -> str:
    """``6x49`` -> ``6/49`` (matches the official-site notation used in refs)."""
    return game_code.replace("x", "/")


@dataclass(slots=True)
class DrawSession:
    """One (draw_year, draw_number) session, one or two drawing rows."""

    draw_year: int
    draw_number: int
    rows: list[Draw]

    @property
    def date(self) -> date:
        return self.rows[0].draw_date

    @property
    def ref(self) -> str:
        return f"{self.draw_number}/{self.draw_year}"


def _group_sessions(draws: list[Draw]) -> list[DrawSession]:
    by_key: dict[tuple[int, int], list[Draw]] = {}
    for draw in draws:
        by_key.setdefault((draw.draw_year, draw.draw_number), []).append(draw)
    return [
        DrawSession(draw_year=year, draw_number=number, rows=rows)
        for (year, number), rows in by_key.items()
    ]


@dataclass(slots=True)
class GameCoverage:
    game_code: str
    game_name: str
    imported_sessions: int
    imported_rows: int
    two_drawing_sessions: int
    expected_sessions: int | None
    coverage_percent: float | None
    earliest_date: date | None
    earliest_ref: str | None
    latest_date: date | None
    latest_ref: str | None
    missing_years: list[int] = field(default_factory=list)
    missing_draw_numbers: dict[int, list[int]] = field(default_factory=dict)
    missing_dates: dict[int, list[date]] = field(default_factory=dict)
    duplicate_sessions: list[str] = field(default_factory=list)
    sources: dict[str, int] = field(default_factory=dict)
    confidence: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "game_code": self.game_code,
            "game_name": self.game_name,
            "imported_sessions": self.imported_sessions,
            "imported_rows": self.imported_rows,
            "two_drawing_sessions": self.two_drawing_sessions,
            "expected_sessions": self.expected_sessions,
            "coverage_percent": self.coverage_percent,
            "earliest_date": self.earliest_date.isoformat() if self.earliest_date else None,
            "earliest_ref": self.earliest_ref,
            "latest_date": self.latest_date.isoformat() if self.latest_date else None,
            "latest_ref": self.latest_ref,
            "missing_years": self.missing_years,
            "missing_draw_numbers": {str(y): n for y, n in sorted(self.missing_draw_numbers.items())},
            "missing_dates": {
                str(y): [d.isoformat() for d in dates]
                for y, dates in sorted(self.missing_dates.items())
            },
            "duplicate_sessions": self.duplicate_sessions,
            "sources": self.sources,
            "confidence": self.confidence,
        }


@dataclass(slots=True)
class CoverageReport:
    generated_at: datetime
    games: list[GameCoverage] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at.isoformat(),
            "games": [g.to_dict() for g in self.games],
        }

    def to_text(self) -> str:
        lines: list[str] = []
        for game in self.games:
            header = display_code(game.game_code)
            lines.append(header)
            lines.append("-" * max(len(header), 6))
            lines.append(f"Imported draws: {game.imported_sessions}")
            if game.two_drawing_sessions:
                lines.append(
                    f"  ({game.imported_rows} rows: {game.two_drawing_sessions} session(s) "
                    "have a second drawing)"
                )
            coverage = f"{game.coverage_percent:.1f}%" if game.coverage_percent is not None else "n/a"
            expected = f" (expected {game.expected_sessions})" if game.expected_sessions is not None else ""
            lines.append(f"Coverage: {coverage}{expected}")
            lines.append("")
            earliest = f"{game.earliest_ref} ({game.earliest_date})" if game.earliest_date else "n/a"
            latest = f"{game.latest_ref} ({game.latest_date})" if game.latest_date else "n/a"
            lines.append(f"Earliest: {earliest}")
            lines.append(f"Latest: {latest}")
            lines.append("")
            if game.missing_years:
                lines.append(f"Missing years: {', '.join(map(str, game.missing_years))}")
            else:
                lines.append("Missing years: none")
            if game.missing_draw_numbers:
                lines.append("Missing draw numbers:")
                for year, numbers in sorted(game.missing_draw_numbers.items()):
                    shown = ", ".join(map(str, numbers[:20])) + (" ..." if len(numbers) > 20 else "")
                    lines.append(f"  {year}: {shown} ({len(numbers)} total)")
            else:
                lines.append("Missing draw numbers: none")
            if game.missing_dates:
                lines.append("Missing dates (expected cadence, no draw found):")
                for year, dates in sorted(game.missing_dates.items()):
                    shown = ", ".join(d.isoformat() for d in dates[:10])
                    shown += " ..." if len(dates) > 10 else ""
                    lines.append(f"  {year}: {shown} ({len(dates)} total)")
            else:
                lines.append("Missing dates: none")
            lines.append("")
            lines.append("Sources:")
            lines.append(f"Live: {game.sources.get('live', 0)}")
            lines.append(f"Wayback: {game.sources.get('wayback', 0)}")
            other = sum(count for src, count in game.sources.items() if src not in ("live", "wayback"))
            lines.append(f"Other: {other}")
            if game.confidence:
                lines.append("")
                lines.append("Confidence (validation status by source):")
                for source, statuses in sorted(game.confidence.items()):
                    breakdown = ", ".join(f"{status}={count}" for status, count in sorted(statuses.items()))
                    lines.append(f"  {source}: {breakdown}")
            lines.append("")
            lines.append(f"Duplicate sessions detected: {len(game.duplicate_sessions)}")
            for ref in game.duplicate_sessions[:10]:
                lines.append(f"  {ref}")
            lines.append("")
        if not self.games:
            lines.append("No games checked.")
        return "\n".join(lines).rstrip() + "\n"

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def write_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "game_code",
            "game_name",
            "imported_sessions",
            "imported_rows",
            "two_drawing_sessions",
            "expected_sessions",
            "coverage_percent",
            "earliest_ref",
            "earliest_date",
            "latest_ref",
            "latest_date",
            "missing_years_count",
            "missing_draw_numbers_count",
            "missing_dates_count",
            "duplicate_sessions_count",
            "live_count",
            "wayback_count",
            "other_count",
        ]
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for game in self.games:
                other = sum(c for src, c in game.sources.items() if src not in ("live", "wayback"))
                writer.writerow(
                    {
                        "game_code": game.game_code,
                        "game_name": game.game_name,
                        "imported_sessions": game.imported_sessions,
                        "imported_rows": game.imported_rows,
                        "two_drawing_sessions": game.two_drawing_sessions,
                        "expected_sessions": game.expected_sessions if game.expected_sessions is not None else "",
                        "coverage_percent": f"{game.coverage_percent:.2f}" if game.coverage_percent is not None else "",
                        "earliest_ref": game.earliest_ref or "",
                        "earliest_date": game.earliest_date.isoformat() if game.earliest_date else "",
                        "latest_ref": game.latest_ref or "",
                        "latest_date": game.latest_date.isoformat() if game.latest_date else "",
                        "missing_years_count": len(game.missing_years),
                        "missing_draw_numbers_count": sum(len(n) for n in game.missing_draw_numbers.values()),
                        "missing_dates_count": sum(len(d) for d in game.missing_dates.values()),
                        "duplicate_sessions_count": len(game.duplicate_sessions),
                        "live_count": game.sources.get("live", 0),
                        "wayback_count": game.sources.get("wayback", 0),
                        "other_count": other,
                    }
                )


class CoverageService:
    """Computes :class:`CoverageReport` from the current database contents.

    Read-only: unlike :class:`app.services.validation.ValidationService`,
    nothing is persisted, so running ``coverage`` repeatedly has no side
    effects on the database.
    """

    def __init__(self, database: Database) -> None:
        self._db = database
        self._log = get_logger("app.coverage")

    def analyze(self, game_code: str | None = None, today: date | None = None) -> CoverageReport:
        today = today or date.today()
        with self._db.session() as session:
            games = GameRepository(session).all_games()
            if game_code is not None:
                games = [g for g in games if g.code == game_code]
            reports = [
                self._analyze_game(game.code, game.name, DrawRepository(session).all_for_game(game.id), today)
                for game in games
            ]
        self._log.info("Coverage analysis: %d game(s)", len(reports))
        return CoverageReport(generated_at=datetime.now(timezone.utc), games=reports)

    def _analyze_game(
        self, game_code: str, game_name: str, draws: list[Draw], today: date
    ) -> GameCoverage:
        sessions = _group_sessions(draws)

        sources: dict[str, int] = {}
        confidence: dict[str, dict[str, int]] = {}
        for draw in draws:
            sources[draw.source] = sources.get(draw.source, 0) + 1
            status = draw.validation_status.value if isinstance(draw.validation_status, ValidationStatus) else str(draw.validation_status)
            confidence.setdefault(draw.source, {}).setdefault(status, 0)
            confidence[draw.source][status] += 1

        duplicate_refs = sorted(
            {f"{d.draw_number}/{d.draw_year}#{d.drawing}" for group in duplicate_number_groups(draws) for d in group}
        )

        if not sessions:
            return GameCoverage(
                game_code=game_code,
                game_name=game_name,
                imported_sessions=0,
                imported_rows=0,
                two_drawing_sessions=0,
                expected_sessions=None,
                coverage_percent=None,
                earliest_date=None,
                earliest_ref=None,
                latest_date=None,
                latest_ref=None,
                sources=sources,
                confidence=confidence,
                duplicate_sessions=duplicate_refs,
            )

        earliest = min(sessions, key=lambda s: s.date)
        latest = max(sessions, key=lambda s: s.date)
        min_year = min(s.draw_year for s in sessions)
        max_year = max(s.draw_year for s in sessions)

        expected_sessions = sum(len(expected_draw_dates(year, today)) for year in range(min_year, max_year + 1))
        coverage_percent = (
            (len(sessions) / expected_sessions * 100) if expected_sessions else None
        )

        years_with_data = {s.draw_year for s in sessions}
        missing_years = sorted(set(range(min_year, max_year + 1)) - years_with_data)

        missing_draw_numbers = missing_numbers_by_year(draws)

        missing_dates: dict[int, list[date]] = {}
        for year in sorted(years_with_data):
            year_sessions = [s for s in sessions if s.draw_year == year]
            observed_dates = {s.date for s in year_sessions}
            span_start = min(observed_dates)
            span_end = max(observed_dates)
            expected_in_span = [d for d in expected_draw_dates(year, today) if span_start <= d <= span_end]
            missing = sorted(set(expected_in_span) - observed_dates)
            if missing:
                missing_dates[year] = missing

        return GameCoverage(
            game_code=game_code,
            game_name=game_name,
            imported_sessions=len(sessions),
            imported_rows=len(draws),
            two_drawing_sessions=sum(1 for s in sessions if len(s.rows) > 1),
            expected_sessions=expected_sessions,
            coverage_percent=coverage_percent,
            earliest_date=earliest.date,
            earliest_ref=earliest.ref,
            latest_date=latest.date,
            latest_ref=latest.ref,
            missing_years=missing_years,
            missing_draw_numbers=missing_draw_numbers,
            missing_dates=missing_dates,
            duplicate_sessions=duplicate_refs,
            sources=sources,
            confidence=confidence,
        )
