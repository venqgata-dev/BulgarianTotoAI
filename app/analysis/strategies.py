"""Prediction strategy framework.

Every strategy implements :meth:`PredictionStrategy.predict`, which receives
a :class:`History` containing only draws strictly before the target draw
(no look-ahead) and returns a :class:`Prediction`. The backtesting engine
(``app.analysis.backtest``) only ever calls this interface through the
:data:`STRATEGY_REGISTRY` - it has no knowledge of any concrete strategy,
so adding a new one (subclass + ``@register_strategy``) never requires
touching the engine.
"""

from __future__ import annotations

import json
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from app.models.domain import GameDefinition


@dataclass(slots=True)
class HistoryEntry:
    """One past draw, as seen by a strategy - decoupled from the ORM."""

    ref: str
    draw_date: date
    numbers: tuple[int, ...]


@dataclass(slots=True)
class History:
    """Everything a strategy is allowed to see: draws strictly before the target.

    ``target_date``/``target_ref`` identify *which* draw is being predicted
    for (so a prediction can be timestamped/labelled) without revealing its
    result - the winning numbers of the target draw are never included in
    ``entries``.
    """

    game: GameDefinition
    entries: list[HistoryEntry]  # chronological order, oldest first
    target_date: date
    target_ref: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.entries


@dataclass(slots=True)
class Prediction:
    """One strategy's output for one target draw."""

    game_code: str
    prediction_date: date
    numbers: tuple[int, ...]
    strategy_name: str
    metadata: dict[str, object] = field(default_factory=dict)
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "game_code": self.game_code,
            "prediction_date": self.prediction_date.isoformat(),
            "numbers": list(self.numbers),
            "strategy_name": self.strategy_name,
            "metadata": self.metadata,
            "generated_at": self.generated_at.isoformat(),
        }

    @staticmethod
    def from_dict(data: dict) -> "Prediction":
        return Prediction(
            game_code=data["game_code"],
            prediction_date=date.fromisoformat(data["prediction_date"]),
            numbers=tuple(data["numbers"]),
            strategy_name=data["strategy_name"],
            metadata=dict(data.get("metadata") or {}),
            generated_at=datetime.fromisoformat(data["generated_at"]),
        )


def save_predictions(predictions: list[Prediction], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [p.to_dict() for p in predictions]
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_predictions(path: Path) -> list[Prediction]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Prediction.from_dict(item) for item in data]


class PredictionStrategy(ABC):
    """Base class for all prediction strategies.

    Subclasses set class-level ``name``/``description`` and implement
    :meth:`predict` using only ``history``. Constructor keyword arguments are
    captured in :attr:`parameters` for introspection, export and the
    strategy-comparison report.
    """

    name: str = "base"
    description: str = ""

    def __init__(self, **params: object) -> None:
        self.parameters: dict[str, object] = dict(params)

    @abstractmethod
    def predict(self, history: History) -> Prediction: ...

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.parameters})"


STRATEGY_REGISTRY: dict[str, type[PredictionStrategy]] = {}


def register_strategy(cls: type[PredictionStrategy]) -> type[PredictionStrategy]:
    """Class decorator: makes a strategy discoverable by name.

    This is the only integration point future strategies need - the
    backtesting engine and CLI both resolve strategies through
    :data:`STRATEGY_REGISTRY` / :func:`create_strategy`.
    """
    STRATEGY_REGISTRY[cls.name] = cls
    return cls


def create_strategy(name: str, **params: object) -> PredictionStrategy:
    try:
        cls = STRATEGY_REGISTRY[name]
    except KeyError as exc:
        raise KeyError(f"Unknown strategy {name!r}; available: {sorted(STRATEGY_REGISTRY)}") from exc
    return cls(**params)


# -- shared helpers for the built-in strategies --------------------------------------------------


def _windowed(entries: list[HistoryEntry], window: int | None) -> list[HistoryEntry]:
    return entries[-window:] if window else entries


def _frequency_and_gap(game: GameDefinition, entries: list[HistoryEntry]) -> tuple[dict[int, int], dict[int, int]]:
    """Return (frequency, current_gap) per number in the game's range.

    ``current_gap`` is draws-since-last-appearance (the full window length
    if the number never appeared), matching the "current missing streak"
    definition used by ``app.analysis.statistics``.
    """
    total = len(entries)
    frequency = {value: 0 for value in range(game.main_min, game.main_max + 1)}
    last_index = {value: -1 for value in frequency}
    for index, entry in enumerate(entries):
        for value in entry.numbers:
            frequency[value] += 1
            last_index[value] = index
    gap = {value: (total - 1 - last_index[value]) if last_index[value] >= 0 else total for value in frequency}
    return frequency, gap


def _top_n(scores: dict[int, int], n: int, *, descending: bool) -> tuple[int, ...]:
    ordered = sorted(scores, key=lambda v: (-scores[v] if descending else scores[v], v))
    return tuple(sorted(ordered[:n]))


def _make_prediction(history: History, numbers: tuple[int, ...], strategy_name: str, metadata: dict) -> Prediction:
    return Prediction(
        game_code=history.game.code,
        prediction_date=history.target_date,
        numbers=numbers,
        strategy_name=strategy_name,
        metadata=metadata,
    )


# -- built-in strategies -------------------------------------------------------------------------


@register_strategy
class RandomStrategy(PredictionStrategy):
    """Uniformly random numbers from the game's valid range.

    Seeded with a private :class:`random.Random` at construction time, so a
    full backtest run (many sequential ``predict`` calls) is reproducible
    end-to-end when the same seed is supplied, while still drawing a
    different sample for every target draw within that run.
    """

    name = "random"
    description = "Uniformly random numbers from the game's valid range (seeded for reproducibility)."

    def __init__(self, seed: int | None = None) -> None:
        super().__init__(seed=seed)
        self._rng = random.Random(seed)

    def predict(self, history: History) -> Prediction:
        game = history.game
        pool = range(game.main_min, game.main_max + 1)
        numbers = tuple(sorted(self._rng.sample(pool, game.main_count)))
        return _make_prediction(history, numbers, self.name, {"seed": self.parameters.get("seed")})


@register_strategy
class HotNumbersStrategy(PredictionStrategy):
    """Picks the most frequently drawn numbers in the available history."""

    name = "hot"
    description = "Picks the numbers that have appeared most often so far."

    def __init__(self, window: int | None = None) -> None:
        super().__init__(window=window)
        self._window = window

    def predict(self, history: History) -> Prediction:
        entries = _windowed(history.entries, self._window)
        frequency, _ = _frequency_and_gap(history.game, entries)
        numbers = _top_n(frequency, history.game.main_count, descending=True)
        return _make_prediction(history, numbers, self.name, {"window": self._window})


@register_strategy
class ColdNumbersStrategy(PredictionStrategy):
    """Picks the least frequently drawn numbers in the available history."""

    name = "cold"
    description = "Picks the numbers that have appeared least often so far."

    def __init__(self, window: int | None = None) -> None:
        super().__init__(window=window)
        self._window = window

    def predict(self, history: History) -> Prediction:
        entries = _windowed(history.entries, self._window)
        frequency, _ = _frequency_and_gap(history.game, entries)
        numbers = _top_n(frequency, history.game.main_count, descending=False)
        return _make_prediction(history, numbers, self.name, {"window": self._window})


@register_strategy
class GapStrategy(PredictionStrategy):
    """Picks the most "overdue" numbers - the longest current absence streak."""

    name = "gap"
    description = "Picks the numbers that have gone the longest without appearing."

    def __init__(self, window: int | None = None) -> None:
        super().__init__(window=window)
        self._window = window

    def predict(self, history: History) -> Prediction:
        entries = _windowed(history.entries, self._window)
        _, gap = _frequency_and_gap(history.game, entries)
        numbers = _top_n(gap, history.game.main_count, descending=True)
        return _make_prediction(history, numbers, self.name, {"window": self._window})


@register_strategy
class BalancedStrategy(PredictionStrategy):
    """Targets a configurable low/high split, filling each half by frequency."""

    name = "balanced"
    description = "Targets a configurable low/high number split, filled by descending frequency within each half."

    def __init__(self, low_high_split: float = 0.5) -> None:
        super().__init__(low_high_split=low_high_split)
        self._low_high_split = low_high_split

    def predict(self, history: History) -> Prediction:
        game = history.game
        frequency, _ = _frequency_and_gap(game, history.entries)
        midpoint = (game.main_min + game.main_max + 1) // 2
        low_target = min(game.main_count, max(0, round(game.main_count * self._low_high_split)))
        high_target = game.main_count - low_target

        low_pool = sorted((v for v in frequency if v < midpoint), key=lambda v: (-frequency[v], v))
        high_pool = sorted((v for v in frequency if v >= midpoint), key=lambda v: (-frequency[v], v))

        chosen = low_pool[:low_target] + high_pool[:high_target]
        if len(chosen) < game.main_count:
            # Only possible for pathologically small games; top up deterministically.
            remaining = [v for v in sorted(frequency, key=lambda v: (-frequency[v], v)) if v not in chosen]
            chosen += remaining[: game.main_count - len(chosen)]
        numbers = tuple(sorted(chosen))
        return _make_prediction(history, numbers, self.name, {"low_high_split": self._low_high_split})


@register_strategy
class HybridStrategy(PredictionStrategy):
    """Blends hot numbers and overdue (gap) numbers using a configurable weight."""

    name = "hybrid"
    description = "Blends hot numbers and overdue (gap) numbers using a configurable weight."

    def __init__(self, hot_weight: float = 0.5) -> None:
        super().__init__(hot_weight=hot_weight)
        self._hot_weight = hot_weight

    def predict(self, history: History) -> Prediction:
        game = history.game
        frequency, gap = _frequency_and_gap(game, history.entries)
        hot_count = min(game.main_count, max(0, round(game.main_count * self._hot_weight)))

        hot_ranked = sorted(frequency, key=lambda v: (-frequency[v], v))
        gap_ranked = sorted(gap, key=lambda v: (-gap[v], v))

        chosen: list[int] = list(hot_ranked[:hot_count])
        for value in gap_ranked:
            if len(chosen) >= game.main_count:
                break
            if value not in chosen:
                chosen.append(value)
        if len(chosen) < game.main_count:
            for value in hot_ranked:
                if len(chosen) >= game.main_count:
                    break
                if value not in chosen:
                    chosen.append(value)
        numbers = tuple(sorted(chosen[: game.main_count]))
        return _make_prediction(history, numbers, self.name, {"hot_weight": self._hot_weight})
