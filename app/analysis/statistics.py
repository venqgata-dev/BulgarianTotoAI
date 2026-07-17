"""Historical statistics engine.

Computes descriptive statistics over the numbers, sums and combinations of
imported draws for one game, scoped to the whole history, a set of years, or
the last N draws. Each row of ``draws`` (see ``app.database.models.Draw``)
is treated as one independent statistical observation: a historical
two-drawing session contributes two observations, one per drawing, since
each drawing publishes its own independently drawn numbers.

Read-only: nothing is persisted, and the scraper/validation/coverage
pipelines are untouched.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from itertools import combinations
from pathlib import Path
from statistics import mean

from app.database.engine import Database
from app.database.models import Draw
from app.database.repository import DrawRepository, GameRepository
from app.models.domain import GameDefinition, game_by_code
from app.services.logging_service import get_logger


def is_prime(value: int) -> bool:
    if value < 2:
        return False
    for divisor in range(2, int(value**0.5) + 1):
        if value % divisor == 0:
            return False
    return True


def main_numbers(draw: Draw) -> list[int]:
    return sorted(n.value for n in draw.numbers if not n.is_bonus)


def draw_ref(draw: Draw) -> str:
    suffix = f"#{draw.drawing}" if draw.drawing != 1 else ""
    return f"{draw.draw_number}/{draw.draw_year}{suffix}"


def _decade_label(value: int) -> str:
    start = ((value - 1) // 10) * 10 + 1
    return f"{start}-{start + 9}"


#: One statistical observation: the draw row plus its already-sorted numbers.
Observation = tuple[Draw, list[int]]


@dataclass(slots=True)
class NumberStat:
    value: int
    frequency: int
    percentage: float
    first_seen_date: date | None
    first_seen_ref: str | None
    last_seen_date: date | None
    last_seen_ref: str | None
    current_streak: int
    longest_streak: int
    average_gap: float | None

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "frequency": self.frequency,
            "percentage": round(self.percentage, 2),
            "first_seen_date": self.first_seen_date.isoformat() if self.first_seen_date else None,
            "first_seen_ref": self.first_seen_ref,
            "last_seen_date": self.last_seen_date.isoformat() if self.last_seen_date else None,
            "last_seen_ref": self.last_seen_ref,
            "current_streak": self.current_streak,
            "longest_streak": self.longest_streak,
            "average_gap": round(self.average_gap, 2) if self.average_gap is not None else None,
        }


@dataclass(slots=True)
class DrawStat:
    ref: str
    draw_date: date
    numbers: tuple[int, ...]
    odd_count: int
    even_count: int
    low_count: int
    high_count: int
    total_sum: int
    consecutive_count: int
    repeated_from_previous: int | None

    def to_dict(self) -> dict:
        return {
            "ref": self.ref,
            "date": self.draw_date.isoformat(),
            "numbers": list(self.numbers),
            "odd_count": self.odd_count,
            "even_count": self.even_count,
            "low_count": self.low_count,
            "high_count": self.high_count,
            "total_sum": self.total_sum,
            "consecutive_count": self.consecutive_count,
            "repeated_from_previous": self.repeated_from_previous,
        }


@dataclass(slots=True)
class ComboStat:
    numbers: tuple[int, ...]
    frequency: int

    def to_dict(self) -> dict:
        return {"numbers": list(self.numbers), "frequency": self.frequency}


@dataclass(slots=True)
class DistributionStats:
    last_digit: dict[int, int]
    prime_count: int
    non_prime_count: int
    even_count: int
    odd_count: int
    decade: dict[str, int]
    heatmap: dict[int, int]

    def to_dict(self) -> dict:
        return {
            "last_digit": {str(k): v for k, v in sorted(self.last_digit.items())},
            "prime_count": self.prime_count,
            "non_prime_count": self.non_prime_count,
            "even_count": self.even_count,
            "odd_count": self.odd_count,
            "decade": dict(sorted(self.decade.items(), key=lambda kv: int(kv[0].split("-")[0]))),
            "heatmap": {str(k): v for k, v in sorted(self.heatmap.items())},
        }


@dataclass(slots=True)
class StatisticsReport:
    game_code: str
    game_name: str
    scope: str
    generated_at: datetime
    draw_count: int
    numbers: list[NumberStat] = field(default_factory=list)
    hottest: list[NumberStat] = field(default_factory=list)
    coldest: list[NumberStat] = field(default_factory=list)
    draws: list[DrawStat] = field(default_factory=list)
    most_common_pairs: list[ComboStat] = field(default_factory=list)
    least_common_pairs: list[ComboStat] = field(default_factory=list)
    most_common_triplets: list[ComboStat] = field(default_factory=list)
    least_common_triplets: list[ComboStat] = field(default_factory=list)
    distribution: DistributionStats | None = None

    def to_dict(self) -> dict:
        return {
            "game_code": self.game_code,
            "game_name": self.game_name,
            "scope": self.scope,
            "generated_at": self.generated_at.isoformat(),
            "draw_count": self.draw_count,
            "numbers": [n.to_dict() for n in self.numbers],
            "hottest": [n.to_dict() for n in self.hottest],
            "coldest": [n.to_dict() for n in self.coldest],
            "draws": [d.to_dict() for d in self.draws],
            "most_common_pairs": [c.to_dict() for c in self.most_common_pairs],
            "least_common_pairs": [c.to_dict() for c in self.least_common_pairs],
            "most_common_triplets": [c.to_dict() for c in self.most_common_triplets],
            "least_common_triplets": [c.to_dict() for c in self.least_common_triplets],
            "distribution": self.distribution.to_dict() if self.distribution else None,
        }

    def to_text(self) -> str:
        lines = [f"STATISTICS REPORT - {self.game_name} ({self.scope})", "=" * 60]
        lines.append(f"Draws analyzed: {self.draw_count}")
        lines.append("")
        lines.append(f"Hot numbers (top {len(self.hottest)}):")
        lines.append("  " + ", ".join(f"{n.value} ({n.frequency}, {n.percentage:.1f}%)" for n in self.hottest))
        lines.append(f"Cold numbers (bottom {len(self.coldest)}):")
        lines.append("  " + ", ".join(f"{n.value} ({n.frequency}, {n.percentage:.1f}%)" for n in self.coldest))
        lines.append("")
        lines.append(f"Most common pairs (top {len(self.most_common_pairs)}):")
        for combo in self.most_common_pairs:
            lines.append(f"  {combo.numbers}: {combo.frequency}")
        lines.append(f"Most common triplets (top {len(self.most_common_triplets)}):")
        for combo in self.most_common_triplets:
            lines.append(f"  {combo.numbers}: {combo.frequency}")
        lines.append("")
        if self.distribution:
            d = self.distribution
            lines.append("Distribution:")
            lines.append(f"  Even: {d.even_count}   Odd: {d.odd_count}")
            lines.append(f"  Prime: {d.prime_count}   Non-prime: {d.non_prime_count}")
            last_digit = ", ".join(f"{k}={v}" for k, v in sorted(d.last_digit.items()))
            lines.append(f"  Last digit: {last_digit}")
            decade = ", ".join(
                f"{k}={v}" for k, v in sorted(d.decade.items(), key=lambda kv: int(kv[0].split("-")[0]))
            )
            lines.append(f"  Decades: {decade}")
        return "\n".join(lines) + "\n"

    def write_json(self, path: Path) -> None:
        """Write the full report (all sections) as JSON."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def write_csv(self, path: Path) -> None:
        """Write the per-number frequency table (the primary tabular
        statistic) as CSV. Use :meth:`write_json` for the other sections
        (pairs, triplets, distribution, per-draw stats)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "value",
            "frequency",
            "percentage",
            "first_seen_date",
            "first_seen_ref",
            "last_seen_date",
            "last_seen_ref",
            "current_streak",
            "longest_streak",
            "average_gap",
        ]
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for number in self.numbers:
                writer.writerow(number.to_dict())


class StatisticsService:
    """Computes :class:`StatisticsReport` from the current database contents."""

    def __init__(self, database: Database) -> None:
        self._db = database
        self._log = get_logger("app.statistics")

    def analyze(
        self,
        game_code: str,
        *,
        years: list[int] | None = None,
        last_n: int | None = None,
        top_n: int = 10,
    ) -> StatisticsReport:
        """Analyze ``game_code``'s history.

        ``years`` restricts to those numbering years; ``last_n`` (applied
        after any year filter) keeps only the most recent N observations in
        chronological order. Neither given means "whole history".
        """
        definition = game_by_code(game_code)
        with self._db.session() as session:
            game = GameRepository(session).by_code(game_code)
            game_name = game.name
            draws = DrawRepository(session).all_for_game(game.id)
            ordered = sorted(draws, key=lambda d: (d.draw_date, d.drawing))

            scope_parts: list[str] = []
            if years:
                wanted = set(years)
                ordered = [d for d in ordered if d.draw_year in wanted]
                scope_parts.append(f"years {', '.join(str(y) for y in sorted(wanted))}")
            if last_n:
                ordered = ordered[-last_n:]
                scope_parts.append(f"last {last_n} draws")
            scope = " / ".join(scope_parts) if scope_parts else "whole history"

            observations: list[Observation] = [(d, main_numbers(d)) for d in ordered]

        report = self._build_report(game_code, game_name, definition, observations, scope, top_n)
        self._log.info(
            "Statistics analysis: %s (%d observation(s), scope=%s)", game_code, len(observations), scope
        )
        return report

    @staticmethod
    def _build_report(
        game_code: str,
        game_name: str,
        definition: GameDefinition,
        observations: list[Observation],
        scope: str,
        top_n: int,
    ) -> StatisticsReport:
        numbers = StatisticsService._number_stats(definition, observations)
        hottest = sorted(numbers, key=lambda n: (-n.frequency, n.value))[:top_n]
        coldest = sorted(numbers, key=lambda n: (n.frequency, n.value))[:top_n]
        draw_stats = StatisticsService._draw_stats(definition, observations)
        (pairs_most, pairs_least), (triplets_most, triplets_least) = StatisticsService._combo_stats(
            observations, top_n
        )
        distribution = StatisticsService._distribution(observations)
        return StatisticsReport(
            game_code=game_code,
            game_name=game_name,
            scope=scope,
            generated_at=datetime.now(timezone.utc),
            draw_count=len(observations),
            numbers=numbers,
            hottest=hottest,
            coldest=coldest,
            draws=draw_stats,
            most_common_pairs=pairs_most,
            least_common_pairs=pairs_least,
            most_common_triplets=triplets_most,
            least_common_triplets=triplets_least,
            distribution=distribution,
        )

    @staticmethod
    def _number_stats(definition: GameDefinition, observations: list[Observation]) -> list[NumberStat]:
        total = len(observations)
        appearances: dict[int, list[int]] = {
            value: [] for value in range(definition.main_min, definition.main_max + 1)
        }
        for index, (_, nums) in enumerate(observations):
            for value in nums:
                appearances[value].append(index)

        result: list[NumberStat] = []
        for value, indices in appearances.items():
            frequency = len(indices)
            percentage = (frequency / total * 100) if total else 0.0
            if not indices:
                result.append(
                    NumberStat(
                        value=value,
                        frequency=0,
                        percentage=0.0,
                        first_seen_date=None,
                        first_seen_ref=None,
                        last_seen_date=None,
                        last_seen_ref=None,
                        current_streak=total,
                        longest_streak=total,
                        average_gap=None,
                    )
                )
                continue

            first_draw = observations[indices[0]][0]
            last_draw = observations[indices[-1]][0]
            gaps = [b - a for a, b in zip(indices, indices[1:])]
            leading_gap = indices[0]
            trailing_gap = (total - 1) - indices[-1]
            longest_streak = max(gaps + [leading_gap, trailing_gap]) if gaps else max(leading_gap, trailing_gap)
            result.append(
                NumberStat(
                    value=value,
                    frequency=frequency,
                    percentage=percentage,
                    first_seen_date=first_draw.draw_date,
                    first_seen_ref=draw_ref(first_draw),
                    last_seen_date=last_draw.draw_date,
                    last_seen_ref=draw_ref(last_draw),
                    current_streak=trailing_gap,
                    longest_streak=longest_streak,
                    average_gap=mean(gaps) if gaps else None,
                )
            )
        return sorted(result, key=lambda n: n.value)

    @staticmethod
    def _draw_stats(definition: GameDefinition, observations: list[Observation]) -> list[DrawStat]:
        midpoint = (definition.main_min + definition.main_max + 1) // 2
        result: list[DrawStat] = []
        previous_numbers: set[int] | None = None
        for draw, nums in observations:
            odd_count = sum(1 for v in nums if v % 2 == 1)
            low_count = sum(1 for v in nums if v < midpoint)
            consecutive_count = sum(1 for a, b in zip(nums, nums[1:]) if b - a == 1)
            repeated = len(set(nums) & previous_numbers) if previous_numbers is not None else None
            result.append(
                DrawStat(
                    ref=draw_ref(draw),
                    draw_date=draw.draw_date,
                    numbers=tuple(nums),
                    odd_count=odd_count,
                    even_count=len(nums) - odd_count,
                    low_count=low_count,
                    high_count=len(nums) - low_count,
                    total_sum=sum(nums),
                    consecutive_count=consecutive_count,
                    repeated_from_previous=repeated,
                )
            )
            previous_numbers = set(nums)
        return result

    @staticmethod
    def _combo_stats(
        observations: list[Observation], top_n: int
    ) -> tuple[tuple[list[ComboStat], list[ComboStat]], tuple[list[ComboStat], list[ComboStat]]]:
        pair_counts: Counter[tuple[int, ...]] = Counter()
        triplet_counts: Counter[tuple[int, ...]] = Counter()
        for _, nums in observations:
            pair_counts.update(combinations(nums, 2))
            triplet_counts.update(combinations(nums, 3))

        def most_and_least(counts: Counter[tuple[int, ...]]) -> tuple[list[ComboStat], list[ComboStat]]:
            most = [ComboStat(k, v) for k, v in counts.most_common(top_n)]
            least = [
                ComboStat(k, v) for k, v in sorted(counts.items(), key=lambda kv: (kv[1], kv[0]))[:top_n]
            ]
            return most, least

        return most_and_least(pair_counts), most_and_least(triplet_counts)

    @staticmethod
    def _distribution(observations: list[Observation]) -> DistributionStats:
        last_digit: Counter[int] = Counter()
        decade: Counter[str] = Counter()
        heatmap: Counter[int] = Counter()
        even_count = 0
        odd_count = 0
        prime_count = 0
        for _, nums in observations:
            for value in nums:
                last_digit[value % 10] += 1
                heatmap[value] += 1
                decade[_decade_label(value)] += 1
                if value % 2 == 0:
                    even_count += 1
                else:
                    odd_count += 1
                if is_prime(value):
                    prime_count += 1
        total = even_count + odd_count
        return DistributionStats(
            last_digit=dict(last_digit),
            prime_count=prime_count,
            non_prime_count=total - prime_count,
            even_count=even_count,
            odd_count=odd_count,
            decade=dict(decade),
            heatmap=dict(heatmap),
        )
