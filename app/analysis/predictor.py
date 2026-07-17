"""Deterministic prediction engine with automatic weight calibration.

Generates a large pool of random candidate combinations and scores each one
using historical signals built from the imported draws for one game:
overall frequency, recent frequency, current gap (draws since last seen),
pair/triplet co-occurrence, odd/even balance, low/high balance, sum
distribution and decade spread. The highest-scoring candidates become the
recommended and alternative combinations.

This is descriptive statistics used as a ranking heuristic, not a
predictive model. Lottery draws are independent random events: no
combination is more likely to win than any other, and nothing here changes
that. The score only measures how closely a candidate resembles patterns
seen in past draws.

Automatic calibration
----------------------
The nine signals above are combined into one score via a weight vector
(:class:`ScoreWeights`). Rather than trust one hand-picked vector, this
module *calibrates* it per game: a small set of candidate weight vectors
(the hand-picked default, a uniform split, one vector per signal emphasised,
and a handful of deterministically-generated random vectors) is each
replayed against the game's most recent draws using the same
no-look-ahead-history replay technique as ``app.analysis.backtest``
(strictly self-contained here - it does not use the strategy registry, so
this stays invisible to the existing Backtesting page/CLI) - see
:func:`calibrate`. Whichever vector achieves the highest average hit count
becomes the vector actually used for the full-size recommendation. The
result is cached to disk (keyed by a fingerprint of the imported data) so
normal predictions stay fast; recalibration only happens the first time a
game is used, or after new draws have been imported.

Determinism: candidate generation uses a seeded ``random.Random`` (default
seed: :data:`DEFAULT_SEED`), so the same game + same imported history +
same calibrated weights always produces the same recommendation. Pass
``seed=None`` to intentionally use a non-deterministic, system-entropy seed
for the final candidate pool (calibration itself always uses a fixed seed,
so the calibrated weights themselves stay reproducible).
"""

from __future__ import annotations

import json
import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import combinations
from math import comb
from pathlib import Path
from statistics import mean, pstdev

from app.analysis.statistics import main_numbers
from app.database.engine import Database
from app.database.models import Draw
from app.database.repository import DrawRepository, GameRepository
from app.models.domain import GameDefinition, game_by_code
from app.services.logging_service import get_logger

DEFAULT_SEED = 42
DEFAULT_POOL_SIZE = 100_000
DEFAULT_ALTERNATIVES = 5

#: Component names, in the order the user asked them to be tunable.
SCORE_COMPONENTS = (
    "frequency",
    "recent_frequency",
    "gap",
    "pair",
    "triplet",
    "odd_even",
    "low_high",
    "sum",
    "decade",
)

#: How many of the most recent draws feed the "recent frequency" signal.
RECENT_WINDOW_DRAWS = 30

#: Calibration is skipped (default weights used) below this much history -
#: not enough data for a meaningful backtest comparison between vectors.
MIN_CALIBRATION_DRAWS = 10
#: How many of the most recent draws calibration backtests each vector against.
CALIBRATION_DRAWS = 25
#: Candidate pool size used *during calibration* (deliberately much smaller
#: than DEFAULT_POOL_SIZE - calibration scores this pool ~19 times over,
#: once per candidate weight vector, so it must stay cheap).
CALIBRATION_POOL_SIZE = 1000
#: How many extra random weight vectors to try, beyond the hand-picked ones.
CALIBRATION_RANDOM_VECTORS = 8
#: Fixed seed for both candidate-vector generation and calibration replay
#: sampling, so calibration itself is 100% reproducible.
CALIBRATION_SEED = 20250101

CALIBRATION_CACHE_FILENAME = "predictor_calibration.json"


def max_combinations(game: GameDefinition) -> int:
    pool = game.main_max - game.main_min + 1
    return comb(pool, game.main_count)


@dataclass(frozen=True, slots=True)
class ScoreWeights:
    """One weighting of the nine scoring signals. Always sums to ~1.0."""

    frequency: float
    recent_frequency: float
    gap: float
    pair: float
    triplet: float
    odd_even: float
    low_high: float
    sum: float
    decade: float

    def to_dict(self) -> dict:
        return {name: getattr(self, name) for name in SCORE_COMPONENTS}

    @staticmethod
    def from_dict(data: dict) -> "ScoreWeights":
        return ScoreWeights(**{name: float(data[name]) for name in SCORE_COMPONENTS})

    @staticmethod
    def default() -> "ScoreWeights":
        """The original hand-picked weighting - also the always-included
        calibration baseline, so calibration can never do worse than this."""
        return ScoreWeights(
            frequency=0.20,
            recent_frequency=0.15,
            gap=0.15,
            pair=0.15,
            triplet=0.10,
            odd_even=0.10,
            low_high=0.05,
            sum=0.05,
            decade=0.05,
        )


def _uniform_weights() -> ScoreWeights:
    share = 1 / len(SCORE_COMPONENTS)
    return ScoreWeights(**{name: share for name in SCORE_COMPONENTS})


def _emphasis_weights(component_index: int, emphasis: float = 0.5) -> ScoreWeights:
    """One signal gets `emphasis` share; the rest evenly split the remainder."""
    remaining = (1 - emphasis) / (len(SCORE_COMPONENTS) - 1)
    values = {name: remaining for name in SCORE_COMPONENTS}
    values[SCORE_COMPONENTS[component_index]] = emphasis
    return ScoreWeights(**values)


def _random_weights(rng: random.Random) -> ScoreWeights:
    raw = [rng.random() + 0.05 for _ in SCORE_COMPONENTS]  # +0.05 avoids near-zero shares
    total = sum(raw)
    return ScoreWeights(**{name: value / total for name, value in zip(SCORE_COMPONENTS, raw)})


def candidate_weight_vectors(seed: int) -> list[ScoreWeights]:
    """Deterministic set of weight vectors calibration chooses among."""
    vectors = [ScoreWeights.default(), _uniform_weights()]
    vectors.extend(_emphasis_weights(index) for index in range(len(SCORE_COMPONENTS)))
    rng = random.Random(seed)
    vectors.extend(_random_weights(rng) for _ in range(CALIBRATION_RANDOM_VECTORS))
    return vectors


@dataclass(slots=True)
class ScoredCombination:
    numbers: tuple[int, ...]
    score: float

    def to_dict(self) -> dict:
        return {"numbers": list(self.numbers), "score": round(self.score, 4)}


@dataclass(slots=True)
class CalibrationResult:
    weights: ScoreWeights
    average_hits: float
    draws_evaluated: int
    candidates_tried: int
    calibrated_at: datetime

    def to_dict(self) -> dict:
        return {
            "weights": self.weights.to_dict(),
            "average_hits": round(self.average_hits, 4),
            "draws_evaluated": self.draws_evaluated,
            "candidates_tried": self.candidates_tried,
            "calibrated_at": self.calibrated_at.isoformat(),
        }


@dataclass(slots=True)
class PredictionResult:
    game_code: str
    game_name: str
    generated_at: datetime
    pool_size: int
    seed: int | None
    recommended: ScoredCombination
    alternatives: list[ScoredCombination] = field(default_factory=list)
    calibration: CalibrationResult | None = None

    def to_dict(self) -> dict:
        return {
            "game_code": self.game_code,
            "game_name": self.game_name,
            "generated_at": self.generated_at.isoformat(),
            "pool_size": self.pool_size,
            "seed": self.seed,
            "recommended": self.recommended.to_dict(),
            "alternatives": [a.to_dict() for a in self.alternatives],
            "calibration": self.calibration.to_dict() if self.calibration else None,
        }

    def to_text(self) -> str:
        lines = [f"PREDICTION - {self.game_name}", "=" * 60]
        seed_label = self.seed if self.seed is not None else "random (non-deterministic)"
        lines.append(f"Candidate pool: {self.pool_size:,} combinations   Seed: {seed_label}")
        if self.calibration:
            lines.append(
                f"Calibrated weights (avg {self.calibration.average_hits:.3f} hits over "
                f"{self.calibration.draws_evaluated} backtested draws, "
                f"{self.calibration.candidates_tried} weightings tried)"
            )
        lines.append("")
        lines.append(
            "Recommended Combination: "
            f"{', '.join(map(str, self.recommended.numbers))}  (score {self.recommended.score:.4f})"
        )
        lines.append("")
        lines.append(f"{len(self.alternatives)} Alternative Combinations:")
        for alt in self.alternatives:
            lines.append(f"  {', '.join(map(str, alt.numbers))}  (score {alt.score:.4f})")
        lines.append("")
        lines.append(
            "Lottery draws are independent random events; no combination is more "
            "likely to win than any other."
        )
        return "\n".join(lines) + "\n"


class _HistoricalProfile:
    """Precomputed historical signals used by :meth:`score` (built once per replay)."""

    def __init__(self, game: GameDefinition, entries: list[tuple[int, ...]], weights: ScoreWeights) -> None:
        self.weights = weights
        self.total = len(entries)
        self.midpoint = (game.main_min + game.main_max + 1) // 2
        self.main_count = game.main_count

        self.frequency = {v: 0 for v in range(game.main_min, game.main_max + 1)}
        last_index = {v: -1 for v in self.frequency}
        self.pair_counts: Counter[tuple[int, int]] = Counter()
        self.triplet_counts: Counter[tuple[int, int, int]] = Counter()
        sums: list[int] = []
        odd_counts: list[int] = []
        low_counts: list[int] = []

        for index, numbers in enumerate(entries):
            for value in numbers:
                self.frequency[value] += 1
                last_index[value] = index
            self.pair_counts.update(combinations(numbers, 2))
            self.triplet_counts.update(combinations(numbers, 3))
            sums.append(sum(numbers))
            odd_counts.append(sum(1 for v in numbers if v % 2 == 1))
            low_counts.append(sum(1 for v in numbers if v < self.midpoint))

        self.gap = {
            v: (self.total - 1 - last_index[v]) if last_index[v] >= 0 else self.total
            for v in self.frequency
        }

        recent_window = min(RECENT_WINDOW_DRAWS, self.total)
        self.recent_frequency = {v: 0 for v in self.frequency}
        for numbers in entries[self.total - recent_window :]:
            for value in numbers:
                self.recent_frequency[value] += 1

        fallback_sum = (game.main_min + game.main_max) * game.main_count / 2
        self.mean_sum = mean(sums) if sums else fallback_sum
        # `or` also covers the case where every draw happens to sum to the
        # same value: pstdev() would then legitimately return 0.0, which
        # divides by zero in score() below.
        self.stdev_sum = (len(sums) > 1 and pstdev(sums)) or max(self.mean_sum * 0.15, 1.0)
        self.mean_odd = mean(odd_counts) if odd_counts else game.main_count / 2
        self.mean_low = mean(low_counts) if low_counts else game.main_count / 2

        self.max_frequency = max(self.frequency.values(), default=0) or 1
        self.max_recent_frequency = max(self.recent_frequency.values(), default=0) or 1
        self.max_gap = max(self.gap.values(), default=0) or 1
        self.max_pair = max(self.pair_counts.values(), default=0) or 1
        self.max_triplet = max(self.triplet_counts.values(), default=0) or 1
        self._pair_slots = comb(game.main_count, 2)
        self._triplet_slots = comb(game.main_count, 3)

    def score(self, combo: tuple[int, ...]) -> float:
        # Plain sum()/division throughout (not statistics.mean, which is far
        # slower due to exact-fraction summation) - this runs once per
        # candidate across a large pool, so per-call cost matters.
        n = self.main_count
        w = self.weights

        freq_component = sum(self.frequency[v] for v in combo) / (self.max_frequency * n)
        recent_component = sum(self.recent_frequency[v] for v in combo) / (self.max_recent_frequency * n)
        gap_component = sum(self.gap[v] for v in combo) / (self.max_gap * n)

        pair_component = sum(self.pair_counts.get(p, 0) for p in combinations(combo, 2)) / (
            self.max_pair * self._pair_slots
        )
        triplet_component = sum(self.triplet_counts.get(t, 0) for t in combinations(combo, 3)) / (
            self.max_triplet * self._triplet_slots
        )

        odd_count = sum(1 for v in combo if v % 2 == 1)
        balance_odd_even = 1 - abs(odd_count - self.mean_odd) / n

        low_count = sum(1 for v in combo if v < self.midpoint)
        balance_low_high = 1 - abs(low_count - self.mean_low) / n

        combo_sum = sum(combo)
        sum_component = max(0.0, 1 - abs(combo_sum - self.mean_sum) / (3 * self.stdev_sum))

        decade_spread = len({(v - 1) // 10 for v in combo}) / n

        return (
            w.frequency * freq_component
            + w.recent_frequency * recent_component
            + w.gap * gap_component
            + w.pair * pair_component
            + w.triplet * triplet_component
            + w.odd_even * balance_odd_even
            + w.low_high * balance_low_high
            + w.sum * sum_component
            + w.decade * decade_spread
        )


def _best_candidates(
    game: GameDefinition, profile: _HistoricalProfile, pool_size: int, rng: random.Random
) -> list[ScoredCombination]:
    """Random candidate pool of `pool_size` unique combinations, scored and
    sorted best-first (ties broken by ascending numbers, for determinism)."""
    effective_pool_size = min(pool_size, max_combinations(game))
    pool = range(game.main_min, game.main_max + 1)
    seen: set[tuple[int, ...]] = set()
    scored: list[ScoredCombination] = []
    while len(seen) < effective_pool_size:
        combo = tuple(sorted(rng.sample(pool, game.main_count)))
        if combo in seen:
            continue
        seen.add(combo)
        scored.append(ScoredCombination(combo, profile.score(combo)))
    scored.sort(key=lambda c: (-c.score, c.numbers))
    return scored


def _replay_average_hits(
    game: GameDefinition,
    ordered_entries: list[tuple[int, ...]],
    weights: ScoreWeights,
    *,
    evaluate_last: int,
    pool_size: int,
    seed: int,
) -> float:
    """Average hit count of `weights`' top pick across the last
    `evaluate_last` draws, using only strictly-earlier draws as history for
    each one (the same no-look-ahead rule as app.analysis.backtest) - this
    is a self-contained mini-replay so calibration never touches the
    strategy registry or BacktestService, keeping it invisible to the
    existing Backtesting page/CLI.
    """
    total = len(ordered_entries)
    start = max(0, total - evaluate_last)
    if start >= total:
        return 0.0
    rng = random.Random(seed)
    hits: list[int] = []
    for index in range(start, total):
        profile = _HistoricalProfile(game, ordered_entries[:index], weights)
        top = _best_candidates(game, profile, pool_size, rng)[0]
        actual = set(ordered_entries[index])
        hits.append(len(set(top.numbers) & actual))
    return mean(hits) if hits else 0.0


def calibrate(
    game: GameDefinition,
    ordered_entries: list[tuple[int, ...]],
    *,
    evaluate_last: int = CALIBRATION_DRAWS,
    pool_size: int = CALIBRATION_POOL_SIZE,
    seed: int = CALIBRATION_SEED,
) -> CalibrationResult:
    """Try every candidate weight vector, replaying it against the most
    recent `evaluate_last` draws, and keep whichever maximises average hits.

    All candidates are replayed against the *same* sequence of random
    candidate pools (one fresh ``random.Random(seed)`` per vector, always
    seeded identically) so the comparison reflects the weighting, not
    sampling luck - a common-random-numbers technique.
    """
    vectors = candidate_weight_vectors(seed)
    draws_evaluated = min(evaluate_last, max(0, len(ordered_entries)))
    best_weights = ScoreWeights.default()
    best_avg = -1.0
    for vector in vectors:
        avg_hits = _replay_average_hits(
            game, ordered_entries, vector, evaluate_last=evaluate_last, pool_size=pool_size, seed=seed
        )
        if avg_hits > best_avg:
            best_avg = avg_hits
            best_weights = vector
    return CalibrationResult(
        weights=best_weights,
        average_hits=max(best_avg, 0.0),
        draws_evaluated=draws_evaluated,
        candidates_tried=len(vectors),
        calibrated_at=datetime.now(timezone.utc),
    )


def _data_fingerprint(draws: list[Draw]) -> dict:
    if not draws:
        return {"count": 0, "latest": None}
    latest = max(draws, key=lambda d: (d.draw_date, d.drawing))
    return {"count": len(draws), "latest": f"{latest.draw_number}/{latest.draw_year}#{latest.drawing}"}


def _load_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


class PredictorService:
    """Scores a large calibrated candidate pool and returns the top combinations."""

    def __init__(self, database: Database) -> None:
        self._db = database
        self._log = get_logger("app.predictor")
        self._cache_path = Path(database.path).parent / CALIBRATION_CACHE_FILENAME

    def predict(
        self,
        game_code: str,
        *,
        pool_size: int = DEFAULT_POOL_SIZE,
        alternatives: int = DEFAULT_ALTERNATIVES,
        seed: int | None = DEFAULT_SEED,
    ) -> PredictionResult:
        definition = game_by_code(game_code)
        with self._db.session() as session:
            game = GameRepository(session).by_code(game_code)
            game_name = game.name
            draws = DrawRepository(session).all_for_game(game.id)

        weights, calibration = self._resolve_weights(game_code, definition, draws)

        entries = [main_numbers(d) for d in draws]
        profile = _HistoricalProfile(definition, entries, weights)
        rng = random.Random(seed)
        scored = _best_candidates(definition, profile, pool_size, rng)
        recommended = scored[0]
        alt_list = scored[1 : 1 + alternatives]

        self._log.info(
            "Prediction: %s (%d candidates, seed=%s, weights=%s) -> %s",
            game_code,
            len(scored),
            seed,
            weights.to_dict(),
            recommended.numbers,
        )
        return PredictionResult(
            game_code=game_code,
            game_name=game_name,
            generated_at=datetime.now(timezone.utc),
            pool_size=len(scored),
            seed=seed,
            recommended=recommended,
            alternatives=alt_list,
            calibration=calibration,
        )

    def _resolve_weights(
        self, game_code: str, definition: GameDefinition, draws: list[Draw]
    ) -> tuple[ScoreWeights, CalibrationResult | None]:
        if len(draws) < MIN_CALIBRATION_DRAWS:
            return ScoreWeights.default(), None

        fingerprint = _data_fingerprint(draws)
        cache = _load_cache(self._cache_path)
        entry = cache.get(game_code)
        if entry and entry.get("fingerprint") == fingerprint:
            cached = entry["result"]
            return ScoreWeights.from_dict(cached["weights"]), CalibrationResult(
                weights=ScoreWeights.from_dict(cached["weights"]),
                average_hits=cached["average_hits"],
                draws_evaluated=cached["draws_evaluated"],
                candidates_tried=cached["candidates_tried"],
                calibrated_at=datetime.fromisoformat(cached["calibrated_at"]),
            )

        ordered = sorted(draws, key=lambda d: (d.draw_date, d.drawing))
        entries = [main_numbers(d) for d in ordered]
        # Read the module-level constants at call time (not as calibrate()'s
        # bound-at-import default arguments) so tests can monkeypatch them.
        result = calibrate(
            definition,
            entries,
            evaluate_last=CALIBRATION_DRAWS,
            pool_size=CALIBRATION_POOL_SIZE,
            seed=CALIBRATION_SEED,
        )
        self._log.info(
            "Calibrated %s: avg_hits=%.3f over %d draws (%d weightings tried) -> %s",
            game_code,
            result.average_hits,
            result.draws_evaluated,
            result.candidates_tried,
            result.weights.to_dict(),
        )
        cache[game_code] = {"fingerprint": fingerprint, "result": result.to_dict()}
        _save_cache(self._cache_path, cache)
        return result.weights, result
