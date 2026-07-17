"""Post-import data validation.

Checks per game:

* wrong number-count per draw (vs. the game definition)
* numbers outside the valid range
* repeated numbers within one draw
* invalid dates (in the future, or numbering year != calendar year)
* duplicate draws (same game/year/number stored twice)
* missing draw numbers within each covered year (gaps)
* broken sequences (draw date not increasing with draw number)

Gap warnings are expected while the official source only exposes a rolling
window (see docs/RESEARCH.md); they mark data we know is missing, not data
corruption. Every run is persisted (``validation_runs``/``validation_issues``)
and each draw's ``validation_status`` is updated.
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
        seen_refs: dict[tuple[int, int], int] = {}

        def add(draw: Draw, issue_type: str, severity: str, description: str) -> None:
            report.issues.append(
                ReportedIssue(
                    issue_type=issue_type,
                    severity=severity,
                    description=description,
                    draw_ref=f"{draw.draw_number}/{draw.draw_year}",
                )
            )
            severities.setdefault(draw.id, []).append(severity)

        for draw in draws:
            main_values = [n.value for n in draw.numbers if not n.is_bonus]

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
            ref = (draw.draw_year, draw.draw_number)
            if ref in seen_refs:
                add(draw, ISSUE_DUPLICATE_DRAW, "error", "same draw stored more than once")
            seen_refs[ref] = draw.id
        return severities

    @staticmethod
    def _check_gaps(game_code: str, draws: list[Draw], report: GameValidationReport) -> None:
        by_year: dict[int, list[int]] = {}
        for draw in draws:
            by_year.setdefault(draw.draw_year, []).append(draw.draw_number)
        for year, numbers in sorted(by_year.items()):
            present = set(numbers)
            missing = sorted(set(range(min(present), max(present) + 1)) - present)
            if missing:
                shown = ", ".join(map(str, missing[:20])) + (" ..." if len(missing) > 20 else "")
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
    def _check_sequence(game_code: str, draws: list[Draw], report: GameValidationReport) -> None:
        ordered = sorted(draws, key=lambda d: (d.draw_year, d.draw_number))
        for previous, current in zip(ordered, ordered[1:]):
            if previous.draw_year == current.draw_year and previous.draw_date >= current.draw_date:
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
        number_str, _, year_str = draw_ref.partition("/")
        for draw in draws:
            if draw.draw_number == int(number_str) and draw.draw_year == int(year_str):
                return draw.id
        return None
