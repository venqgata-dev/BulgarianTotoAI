"""Deterministic backtesting engine for prediction strategies.

For every historical draw in scope, builds a :class:`~app.analysis.strategies.History`
containing only strictly-earlier draws, asks the strategy to predict, and
compares the prediction against the actual result. This module knows
nothing about any concrete strategy - only the shared
:class:`~app.analysis.strategies.PredictionStrategy` interface, resolved by
name through :func:`app.analysis.strategies.create_strategy` (see Part 8 of
the milestone spec / app/analysis/strategies.py docstring).

Determinism: replaying the exact same (game, strategy, scope, parameters)
always produces the exact same :class:`BacktestReport`, because history is
built purely from already-imported data and each strategy's own randomness
(if any) is confined to a seeded RNG (see ``RandomStrategy``).
"""

from __future__ import annotations

import csv
import json
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import mean, median

from app.analysis.statistics import draw_ref, main_numbers
from app.analysis.strategies import History, HistoryEntry, PredictionStrategy, create_strategy
from app.database.engine import Database
from app.database.repository import DrawRepository, GameRepository
from app.models.domain import game_by_code
from app.services.logging_service import get_logger

#: Minimum hit count considered a "win" for streak purposes (lowest prize
#: tier in all three supported games starts at 3 matches).
WIN_THRESHOLD = 3


@dataclass(slots=True)
class BacktestRecord:
    """One target draw's prediction vs. actual outcome."""

    draw_ref: str
    draw_date: date
    predicted_numbers: tuple[int, ...]
    actual_numbers: tuple[int, ...]
    matching_numbers: tuple[int, ...]
    missed_numbers: tuple[int, ...]
    hit_count: int
    strategy_name: str

    def to_dict(self) -> dict:
        return {
            "draw_ref": self.draw_ref,
            "date": self.draw_date.isoformat(),
            "predicted_numbers": list(self.predicted_numbers),
            "actual_numbers": list(self.actual_numbers),
            "matching_numbers": list(self.matching_numbers),
            "missed_numbers": list(self.missed_numbers),
            "hit_count": self.hit_count,
            "strategy_name": self.strategy_name,
        }


@dataclass(slots=True)
class BacktestMetrics:
    predictions: int
    average_hits: float
    median_hits: float
    max_hits: int
    min_hits: int
    hit_distribution: dict[int, int]
    hit_percentage_3_plus: float
    hit_percentage_4_plus: float
    hit_percentage_5_plus: float
    hit_percentage_6: float  # a perfect match: all main_count numbers correct
    longest_winning_streak: int
    longest_losing_streak: int
    average_score: float  # mean(hits / main_count) as a percentage

    def to_dict(self) -> dict:
        return {
            "predictions": self.predictions,
            "average_hits": round(self.average_hits, 3),
            "median_hits": self.median_hits,
            "max_hits": self.max_hits,
            "min_hits": self.min_hits,
            "hit_distribution": {str(k): v for k, v in sorted(self.hit_distribution.items())},
            "hit_percentage_3_plus": round(self.hit_percentage_3_plus, 2),
            "hit_percentage_4_plus": round(self.hit_percentage_4_plus, 2),
            "hit_percentage_5_plus": round(self.hit_percentage_5_plus, 2),
            "hit_percentage_6": round(self.hit_percentage_6, 2),
            "longest_winning_streak": self.longest_winning_streak,
            "longest_losing_streak": self.longest_losing_streak,
            "average_score": round(self.average_score, 2),
        }


def compute_metrics(records: list[BacktestRecord], main_count: int) -> BacktestMetrics:
    if not records:
        return BacktestMetrics(
            predictions=0,
            average_hits=0.0,
            median_hits=0.0,
            max_hits=0,
            min_hits=0,
            hit_distribution={k: 0 for k in range(main_count + 1)},
            hit_percentage_3_plus=0.0,
            hit_percentage_4_plus=0.0,
            hit_percentage_5_plus=0.0,
            hit_percentage_6=0.0,
            longest_winning_streak=0,
            longest_losing_streak=0,
            average_score=0.0,
        )

    hits = [r.hit_count for r in records]
    total = len(hits)
    distribution = {k: 0 for k in range(main_count + 1)}
    for h in hits:
        distribution[h] += 1

    def pct_at_least(threshold: int) -> float:
        return sum(1 for h in hits if h >= threshold) / total * 100

    longest_win = longest_lose = current_win = current_lose = 0
    for h in hits:
        if h >= WIN_THRESHOLD:
            current_win += 1
            current_lose = 0
        else:
            current_lose += 1
            current_win = 0
        longest_win = max(longest_win, current_win)
        longest_lose = max(longest_lose, current_lose)

    return BacktestMetrics(
        predictions=total,
        average_hits=mean(hits),
        median_hits=median(hits),
        max_hits=max(hits),
        min_hits=min(hits),
        hit_distribution=distribution,
        hit_percentage_3_plus=pct_at_least(3),
        hit_percentage_4_plus=pct_at_least(4),
        hit_percentage_5_plus=pct_at_least(5),
        hit_percentage_6=pct_at_least(main_count),
        longest_winning_streak=longest_win,
        longest_losing_streak=longest_lose,
        average_score=mean(h / main_count for h in hits) * 100,
    )


@dataclass(slots=True)
class BacktestReport:
    game_code: str
    game_name: str
    strategy_name: str
    strategy_parameters: dict[str, object]
    scope: str
    generated_at: datetime
    execution_seconds: float
    records: list[BacktestRecord] = field(default_factory=list)
    metrics: BacktestMetrics | None = None

    def to_dict(self) -> dict:
        return {
            "game_code": self.game_code,
            "game_name": self.game_name,
            "strategy_name": self.strategy_name,
            "strategy_parameters": self.strategy_parameters,
            "scope": self.scope,
            "generated_at": self.generated_at.isoformat(),
            "execution_seconds": round(self.execution_seconds, 4),
            "metrics": self.metrics.to_dict() if self.metrics else None,
            "records": [r.to_dict() for r in self.records],
        }

    def to_text(self) -> str:
        m = self.metrics
        lines = [f"BACKTEST REPORT - {self.game_name} - strategy: {self.strategy_name}", "=" * 60]
        lines.append(f"Scope: {self.scope}")
        lines.append(f"Parameters: {self.strategy_parameters}")
        lines.append(f"Execution time: {self.execution_seconds:.3f}s")
        lines.append("")
        if m is None or m.predictions == 0:
            lines.append("No predictions were made in this scope.")
            return "\n".join(lines) + "\n"
        lines.append(f"Predictions: {m.predictions}")
        lines.append(f"Average hits: {m.average_hits:.3f}   Median: {m.median_hits}   Max: {m.max_hits}   Min: {m.min_hits}")
        lines.append(f"Average score: {m.average_score:.2f}%")
        dist = ", ".join(f"{k}={v}" for k, v in sorted(m.hit_distribution.items()))
        lines.append(f"Hit distribution: {dist}")
        lines.append(
            f"Hit %: 3+={m.hit_percentage_3_plus:.1f}%  4+={m.hit_percentage_4_plus:.1f}%  "
            f"5+={m.hit_percentage_5_plus:.1f}%  6={m.hit_percentage_6:.1f}%"
        )
        lines.append(f"Longest winning streak: {m.longest_winning_streak}   Longest losing streak: {m.longest_losing_streak}")
        return "\n".join(lines) + "\n"

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def write_csv(self, path: Path) -> None:
        """Write the per-draw history table (one row per prediction) as CSV."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "draw_ref",
            "date",
            "predicted_numbers",
            "actual_numbers",
            "matching_numbers",
            "missed_numbers",
            "hit_count",
            "strategy_name",
        ]
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for record in self.records:
                row = record.to_dict()
                for key in ("predicted_numbers", "actual_numbers", "matching_numbers", "missed_numbers"):
                    row[key] = " ".join(map(str, row[key]))
                writer.writerow({name: row[name] for name in fieldnames})


@dataclass(slots=True)
class StrategyComparisonRow:
    strategy_name: str
    predictions: int
    average_hits: float
    max_hits: int
    hit_percentage_3_plus: float
    hit_percentage_4_plus: float
    hit_percentage_5_plus: float
    hit_percentage_6: float
    longest_winning_streak: int
    longest_losing_streak: int
    execution_seconds: float

    def to_dict(self) -> dict:
        return {
            "strategy_name": self.strategy_name,
            "predictions": self.predictions,
            "average_hits": round(self.average_hits, 3),
            "max_hits": self.max_hits,
            "hit_percentage_3_plus": round(self.hit_percentage_3_plus, 2),
            "hit_percentage_4_plus": round(self.hit_percentage_4_plus, 2),
            "hit_percentage_5_plus": round(self.hit_percentage_5_plus, 2),
            "hit_percentage_6": round(self.hit_percentage_6, 2),
            "longest_winning_streak": self.longest_winning_streak,
            "longest_losing_streak": self.longest_losing_streak,
            "execution_seconds": round(self.execution_seconds, 4),
        }


@dataclass(slots=True)
class ComparisonReport:
    game_code: str
    game_name: str
    scope: str
    generated_at: datetime
    rows: list[StrategyComparisonRow] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "game_code": self.game_code,
            "game_name": self.game_name,
            "scope": self.scope,
            "generated_at": self.generated_at.isoformat(),
            "strategies": [r.to_dict() for r in self.rows],
        }

    def to_text(self) -> str:
        lines = [f"STRATEGY COMPARISON - {self.game_name}", "=" * 60, f"Scope: {self.scope}", ""]
        header = f"{'Strategy':<10}{'Preds':>7}{'AvgHits':>9}{'Max':>5}{'3+':>7}{'4+':>7}{'5+':>7}{'6':>7}{'BestStk':>9}{'WorstStk':>9}{'Time(s)':>9}"
        lines.append(header)
        lines.append("-" * len(header))
        for row in self.rows:
            lines.append(
                f"{row.strategy_name:<10}{row.predictions:>7}{row.average_hits:>9.3f}{row.max_hits:>5}"
                f"{row.hit_percentage_3_plus:>6.1f}%{row.hit_percentage_4_plus:>6.1f}%"
                f"{row.hit_percentage_5_plus:>6.1f}%{row.hit_percentage_6:>6.1f}%"
                f"{row.longest_winning_streak:>9}{row.longest_losing_streak:>9}{row.execution_seconds:>9.3f}"
            )
        return "\n".join(lines) + "\n"

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def write_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "strategy_name",
            "predictions",
            "average_hits",
            "max_hits",
            "hit_percentage_3_plus",
            "hit_percentage_4_plus",
            "hit_percentage_5_plus",
            "hit_percentage_6",
            "longest_winning_streak",
            "longest_losing_streak",
            "execution_seconds",
        ]
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for row in self.rows:
                writer.writerow(row.to_dict())


class BacktestService:
    """Replays every in-scope historical draw against a prediction strategy."""

    def __init__(self, database: Database) -> None:
        self._db = database
        self._log = get_logger("app.backtest")

    def run(
        self,
        game_code: str,
        strategy_name: str,
        *,
        strategy_params: dict[str, object] | None = None,
        years: list[int] | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        last_n: int | None = None,
    ) -> BacktestReport:
        definition = game_by_code(game_code)
        strategy = create_strategy(strategy_name, **(strategy_params or {}))
        start = time.perf_counter()

        with self._db.session() as session:
            game = GameRepository(session).by_code(game_code)
            game_name = game.name
            draws = DrawRepository(session).all_for_game(game.id)
            ordered = sorted(draws, key=lambda d: (d.draw_date, d.drawing))
            all_entries = [
                HistoryEntry(ref=draw_ref(d), draw_date=d.draw_date, numbers=tuple(main_numbers(d)))
                for d in ordered
            ]

            target_indices, scope = self._select_targets(ordered, years, date_from, date_to, last_n)

            records: list[BacktestRecord] = []
            for index in target_indices:
                target = ordered[index]
                history = History(
                    game=definition,
                    entries=all_entries[:index],
                    target_date=target.draw_date,
                    target_ref=draw_ref(target),
                )
                prediction = strategy.predict(history)
                actual = tuple(main_numbers(target))
                matched = tuple(sorted(set(prediction.numbers) & set(actual)))
                missed = tuple(sorted(set(actual) - set(prediction.numbers)))
                records.append(
                    BacktestRecord(
                        draw_ref=draw_ref(target),
                        draw_date=target.draw_date,
                        predicted_numbers=prediction.numbers,
                        actual_numbers=actual,
                        matching_numbers=matched,
                        missed_numbers=missed,
                        hit_count=len(matched),
                        strategy_name=strategy.name,
                    )
                )

        metrics = compute_metrics(records, definition.main_count)
        elapsed = time.perf_counter() - start
        self._log.info(
            "Backtest: %s/%s (%d predictions, scope=%s, %.3fs)",
            game_code,
            strategy.name,
            len(records),
            scope,
            elapsed,
        )
        return BacktestReport(
            game_code=game_code,
            game_name=game_name,
            strategy_name=strategy.name,
            strategy_parameters=dict(strategy.parameters),
            scope=scope,
            generated_at=datetime.now(timezone.utc),
            execution_seconds=elapsed,
            records=records,
            metrics=metrics,
        )

    def compare(
        self,
        game_code: str,
        strategy_names: list[str],
        *,
        strategy_params: dict[str, dict[str, object]] | None = None,
        years: list[int] | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        last_n: int | None = None,
    ) -> ComparisonReport:
        params_by_name = strategy_params or {}
        rows: list[StrategyComparisonRow] = []
        scope = ""
        game_name = ""
        for name in strategy_names:
            report = self.run(
                game_code,
                name,
                strategy_params=params_by_name.get(name),
                years=years,
                date_from=date_from,
                date_to=date_to,
                last_n=last_n,
            )
            scope = report.scope
            game_name = report.game_name
            m = report.metrics
            rows.append(
                StrategyComparisonRow(
                    strategy_name=report.strategy_name,
                    predictions=m.predictions if m else 0,
                    average_hits=m.average_hits if m else 0.0,
                    max_hits=m.max_hits if m else 0,
                    hit_percentage_3_plus=m.hit_percentage_3_plus if m else 0.0,
                    hit_percentage_4_plus=m.hit_percentage_4_plus if m else 0.0,
                    hit_percentage_5_plus=m.hit_percentage_5_plus if m else 0.0,
                    hit_percentage_6=m.hit_percentage_6 if m else 0.0,
                    longest_winning_streak=m.longest_winning_streak if m else 0,
                    longest_losing_streak=m.longest_losing_streak if m else 0,
                    execution_seconds=report.execution_seconds,
                )
            )
        return ComparisonReport(
            game_code=game_code,
            game_name=game_name,
            scope=scope,
            generated_at=datetime.now(timezone.utc),
            rows=rows,
        )

    @staticmethod
    def _select_targets(
        ordered: list,
        years: list[int] | None,
        date_from: date | None,
        date_to: date | None,
        last_n: int | None,
    ) -> tuple[list[int], str]:
        indices = list(range(len(ordered)))
        scope_parts: list[str] = []
        if years:
            wanted = set(years)
            indices = [i for i in indices if ordered[i].draw_year in wanted]
            scope_parts.append(f"years {', '.join(str(y) for y in sorted(wanted))}")
        if date_from:
            indices = [i for i in indices if ordered[i].draw_date >= date_from]
            scope_parts.append(f"from {date_from.isoformat()}")
        if date_to:
            indices = [i for i in indices if ordered[i].draw_date <= date_to]
            scope_parts.append(f"to {date_to.isoformat()}")
        if last_n:
            indices = indices[-last_n:]
            scope_parts.append(f"last {last_n} draws")
        scope = " / ".join(scope_parts) if scope_parts else "whole history"
        return indices, scope
