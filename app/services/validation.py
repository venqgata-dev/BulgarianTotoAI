"""Post-import data validation.

Checks per game:

* impossible numbering (draw number < 1)
* invalid drawing numbers (outside the known 1-2 range)
* wrong number-count per draw (vs. the game definition)
* numbers outside the valid range
* repeated numbers within one draw
* invalid dates (in the future, or numbering year != calendar year)
* duplicate draws (same game/year/number/drawing stored twice)
* duplicate content (identical numbers+date published under different refs)
* missing draw numbers within each covered year (gaps)
* missing second drawing (a session has only drawing 1 in a year otherwise
  established as two-drawing by its sibling sessions)
* broken sequences (draw date not increasing with draw number; two drawings
  of one session dated differently)

Gap warnings are expected while the official source only exposes a rolling
window (see docs/RESEARCH.md); they mark data we know is missing, not data
corruption. Every run is persisted (``validation_runs``/``validation_issues``)
and each draw's ``validation_status`` is updated.

``missing_numbers_by_year`` and ``duplicate_number_groups`` are also used by
the coverage engine (``app.services.coverage``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from app.database.engine import Database
from app.database.models import Draw, ValidationIssue, ValidationRun, ValidationStatus
from app.database.repository import DrawRepository, GameRepository
from app.models.domain import game_by_code
from app.services.logging_service import get_logger

ISSUE_WRONG_COUNT = "wrong_number_count"
ISSUE_OUT_OF_RANGE = "number_out_of_range"
ISSUE_REPEATED_NUMBER = "repeated_number"
ISSUE_FUTURE_DATE = "future_date"
ISSUE_YEAR_MISMATCH = "year_mismatch"
ISSUE_DUPLICATE_DRAW = "duplicate_draw"
ISSUE_MISSING_DRAWS = "missing_draws"
ISSUE_BROKEN_SEQUENCE = "broken_date_sequence"
ISSUE_IMPOSSIBLE_NUMBERING = "impossible_numbering"
ISSUE_INVALID_DRAWING = "invalid_drawing_number"
ISSUE_MISSING_SECOND_DRAWING = "missing_second_drawing"
ISSUE_DUPLICATE_CONTENT = "duplicate_content"

#: The only drawing numbers ever observed in official data (see
#: app/database/models.py schema notes): a session is either a single
#: modern drawing (1) or a historical two-drawing session (1 and 2).
VALID_DRAWING_NUMBERS = (1, 2)


def missing_numbers_by_year(draws: list[Draw]) -> dict[int, list[int]]:
    """Draw numbers missing between the lowest and highest seen per year.

    Shared by :class:`ValidationService` (gap warnings) and the coverage
    engine (``app.services.coverage``), which both need this exact
    per-year-observed-range computation.
    """
    by_year: dict[int, set[int]] = {}
    for draw in draws:
        by_year.setdefault(draw.draw_year, set()).add(draw.draw_number)
    result: dict[int, list[int]] = {}
    for year, numbers in by_year.items():
        missing = sorted(set(range(min(numbers), max(numbers) + 1)) - numbers)
        if missing:
            result[year] = missing
    return result


def duplicate_number_groups(draws: list[Draw]) -> list[list[Draw]]:
    """Group draws that share identical main numbers on the identical date.

    Two distinct official draw refs (different draw_number and/or drawing)
    publishing the exact same winning numbers on the exact same date is not
    a plausible lottery coincidence - it signals a scraping/import bug
    (e.g. the same page content saved under two different draw refs).
    """
    by_numbers: dict[tuple, list[Draw]] = {}
    for draw in draws:
        main = tuple(sorted(n.value for n in draw.numbers if not n.is_bonus))
        by_numbers.setdefault((draw.draw_date, main), []).append(draw)
    return [
        group
        for group in by_numbers.values()
        if len({(d.draw_number, d.draw_year, d.drawing) for d in group}) > 1
    ]


@dataclass(slots=True)
class ReportedIssue:
    issue_type: str
    severity: str  # "error" | "warning"
    description: str
    draw_ref: str | None = None


@dataclass(slots=True)
class GameValidationReport:
    game_code: str
    draws_checked: int = 0
    issues: list[ReportedIssue] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == "warning")


@dataclass(slots=True)
class ValidationReport:
    run_id: int | None = None
    games: list[GameValidationReport] = field(default_factory=list)

    def to_text(self) -> str:
        lines = ["VALIDATION REPORT", "=" * 60]
        for game in self.games:
            lines.append(
                f"{game.game_code}: {game.draws_checked} draws checked, "
                f"{game.error_count} errors, {game.warning_count} warnings"
            )
            for issue in game.issues:
                ref = f" [{issue.draw_ref}]" if issue.draw_ref else ""
                lines.append(f"  {issue.severity.upper():7} {issue.issue_type}{ref}: {issue.description}")
        if not self.games:
            lines.append("No games checked.")
        return "\n".join(lines)


class ValidationService:
    """Runs all checks and persists the outcome."""

    def __init__(self, database: Database) -> None:
        self._db = database
        self._log = get_logger("app.validation")

    def validate(self, game_code: str | None = None, today: date | None = None) -> ValidationReport:
        """Validate one game (or all seeded games) and persist the results."""
        today = today or date.today()
        report = ValidationReport()
        with self._db.session() as session:
            games = GameRepository(session).all_games()
            if game_code is not None:
                games = [g for g in games if g.code == game_code]
            run = ValidationRun()
            session.add(run)
            session.flush()
            report.run_id = run.id

            total_checked = 0
            total_issues = 0
            for game in games:
                game_report = GameValidationReport(game_code=game.code)
                draws = DrawRepository(session).all_for_game(game.id)
                game_report.draws_checked = len(draws)
                total_checked += len(draws)

                per_draw_issues = self._check_draws(game.code, draws, today, game_report)
                self._check_gaps(game.code, draws, game_report)
                self._check_sequence(game.code, draws, game_report)
                self._check_missing_second_drawing(draws, game_report)
                self._check_duplicate_content(draws, game_report)

                for draw in draws:
                    severities = per_draw_issues.get(draw.id, [])
                    if "error" in severities:
                        draw.validation_status = ValidationStatus.INVALID
                    elif "warning" in severities:
                        draw.validation_status = ValidationStatus.WARNING
                    else:
                        draw.validation_status = ValidationStatus.VALID

                for issue in game_report.issues:
                    session.add(
                        ValidationIssue(
                            run_id=run.id,
                            game_id=game.id,
                            draw_id=self._draw_id_for_ref(draws, issue.draw_ref),
                            issue_type=issue.issue_type,
                            severity=issue.severity,
                            description=issue.description[:512],
                        )
                    )
                total_issues += len(game_report.issues)
                report.games.append(game_report)

            run.draws_checked = total_checked
            run.issues_found = total_issues
            run.finished_at = datetime.now(timezone.utc)
        self._log.info(
            "Validation run %s: %d draws, %d issues", report.run_id, total_checked, total_issues
        )
        return report

    # -- individual checks -------------------------------------------------------

    def _check_draws(
        self,
        game_code: str,
        draws: list[Draw],
        today: date,
        report: GameValidationReport,
    ) -> dict[int, list[str]]:
        definition = game_by_code(game_code)
        severities: dict[int, list[str]] = {}
        seen_refs: dict[tuple[int, int, int], int] = {}

        def add(draw: Draw, issue_type: str, severity: str, description: str) -> None:
            suffix = f"#{draw.drawing}" if draw.drawing != 1 else ""
            report.issues.append(
                ReportedIssue(
                    issue_type=issue_type,
                    severity=severity,
                    description=description,
                    draw_ref=f"{draw.draw_number}/{draw.draw_year}{suffix}",
                )
            )
            severities.setdefault(draw.id, []).append(severity)

        for draw in draws:
            main_values = [n.value for n in draw.numbers if not n.is_bonus]

            if draw.draw_number < 1:
                add(
                    draw,
                    ISSUE_IMPOSSIBLE_NUMBERING,
                    "error",
                    f"draw number {draw.draw_number} is not a positive integer",
                )
            if draw.drawing not in VALID_DRAWING_NUMBERS:
                add(
                    draw,
                    ISSUE_INVALID_DRAWING,
                    "error",
                    f"drawing {draw.drawing} is outside the known range {VALID_DRAWING_NUMBERS}",
                )
            if len(main_values) != definition.main_count:
                add(
                    draw,
                    ISSUE_WRONG_COUNT,
                    "error",
                    f"expected {definition.main_count} numbers, found {len(main_values)}",
                )
            out_of_range = [v for v in main_values if not definition.is_valid_main_number(v)]
            if out_of_range:
                add(
                    draw,
                    ISSUE_OUT_OF_RANGE,
                    "error",
                    f"numbers outside {definition.main_min}-{definition.main_max}: {out_of_range}",
                )
            if len(set(main_values)) != len(main_values):
                add(draw, ISSUE_REPEATED_NUMBER, "error", f"repeated numbers in {main_values}")
            if draw.draw_date > today:
                add(draw, ISSUE_FUTURE_DATE, "error", f"draw date {draw.draw_date} is in the future")
            if draw.draw_date.year != draw.draw_year:
                add(
                    draw,
                    ISSUE_YEAR_MISMATCH,
                    "warning",
                    f"numbering year {draw.draw_year} != calendar year {draw.draw_date.year}",
                )
            ref = (draw.draw_year, draw.draw_number, draw.drawing)
            if ref in seen_refs:
                add(draw, ISSUE_DUPLICATE_DRAW, "error", "same draw stored more than once")
            seen_refs[ref] = draw.id
        return severities

    @staticmethod
    def _check_gaps(game_code: str, draws: list[Draw], report: GameValidationReport) -> None:
        for year, missing in sorted(missing_numbers_by_year(draws).items()):
            shown = ", ".join(map(str, missing[:20])) + (" ..." if len(missing) > 20 else "")
            present = {d.draw_number for d in draws if d.draw_year == year}
            report.issues.append(
                ReportedIssue(
                    issue_type=ISSUE_MISSING_DRAWS,
                    severity="warning",
                    description=(
                        f"{game_code} {year}: {len(missing)} draw(s) missing between "
                        f"{min(present)} and {max(present)}: {shown}"
                    ),
                )
            )

    @staticmethod
    def _check_missing_second_drawing(draws: list[Draw], report: GameValidationReport) -> None:
        """Flag sessions missing drawing 2 in a year where that year's format
        is otherwise established (some session in the same year *does* have a
        drawing 2) - i.e. only when determinable from the imported data
        itself, not from an assumed historical cutoff date.
        """
        by_year: dict[int, dict[int, set[int]]] = {}
        for draw in draws:
            by_year.setdefault(draw.draw_year, {}).setdefault(draw.draw_number, set()).add(draw.drawing)
        for year, sessions in sorted(by_year.items()):
            if not any(2 in drawings for drawings in sessions.values()):
                continue  # this year's format is not established as two-drawing
            for number, drawings in sorted(sessions.items()):
                if drawings == {1}:
                    report.issues.append(
                        ReportedIssue(
                            issue_type=ISSUE_MISSING_SECOND_DRAWING,
                            severity="warning",
                            description=(
                                f"draw {number}/{year}: other {year} sessions record a second "
                                'drawing ("II-ро теглене") but this one only has drawing 1'
                            ),
                            draw_ref=f"{number}/{year}",
                        )
                    )

    @staticmethod
    def _check_duplicate_content(draws: list[Draw], report: GameValidationReport) -> None:
        for group in duplicate_number_groups(draws):
            refs = ", ".join(
                sorted(f"{d.draw_number}/{d.draw_year}#{d.drawing}" for d in group)
            )
            report.issues.append(
                ReportedIssue(
                    issue_type=ISSUE_DUPLICATE_CONTENT,
                    severity="error",
                    description=f"identical numbers published on the same date under multiple draw refs: {refs}",
                )
            )

    @staticmethod
    def _check_sequence(game_code: str, draws: list[Draw], report: GameValidationReport) -> None:
        ordered = sorted(draws, key=lambda d: (d.draw_year, d.draw_number, d.drawing))
        for previous, current in zip(ordered, ordered[1:]):
            if previous.draw_year != current.draw_year:
                continue
            if previous.draw_number == current.draw_number:
                # Two drawings of the same draw session must share the date.
                if previous.draw_date != current.draw_date:
                    report.issues.append(
                        ReportedIssue(
                            issue_type=ISSUE_BROKEN_SEQUENCE,
                            severity="error",
                            description=(
                                f"drawings of draw {current.draw_number}/{current.draw_year} "
                                f"have different dates: {previous.draw_date} vs {current.draw_date}"
                            ),
                            draw_ref=f"{current.draw_number}/{current.draw_year}",
                        )
                    )
            elif previous.draw_date >= current.draw_date:
                report.issues.append(
                    ReportedIssue(
                        issue_type=ISSUE_BROKEN_SEQUENCE,
                        severity="error",
                        description=(
                            f"draw {current.draw_number}/{current.draw_year} dated "
                            f"{current.draw_date} not after draw {previous.draw_number}/"
                            f"{previous.draw_year} dated {previous.draw_date}"
                        ),
                        draw_ref=f"{current.draw_number}/{current.draw_year}",
                    )
                )

    @staticmethod
    def _draw_id_for_ref(draws: list[Draw], draw_ref: str | None) -> int | None:
        if not draw_ref:
            return None
        number_str, _, year_part = draw_ref.partition("/")
        year_str, _, drawing_str = year_part.partition("#")
        drawing = int(drawing_str) if drawing_str else 1
        for draw in draws:
            if (
                draw.draw_number == int(number_str)
                and draw.draw_year == int(year_str)
                and draw.drawing == drawing
            ):
                return draw.id
        return None
