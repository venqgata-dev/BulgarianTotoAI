"""Prediction strategy framework tests."""

from __future__ import annotations

from datetime import date

import pytest

from app.analysis.strategies import (
    STRATEGY_REGISTRY,
    BalancedStrategy,
    ColdNumbersStrategy,
    GapStrategy,
    History,
    HistoryEntry,
    HotNumbersStrategy,
    HybridStrategy,
    Prediction,
    PredictionStrategy,
    RandomStrategy,
    _frequency_and_gap,
    _top_n,
    create_strategy,
    load_predictions,
    save_predictions,
)
from app.models.domain import game_by_code

GAME = game_by_code("6x49")


def make_history(entries: list[HistoryEntry], target_date: date = date(2024, 1, 18)) -> History:
    return History(game=GAME, entries=entries, target_date=target_date, target_ref="5/2024")


def four_draw_history() -> History:
    entries = [
        HistoryEntry("1/2024", date(2024, 1, 4), (1, 2, 3, 4, 5, 6)),
        HistoryEntry("2/2024", date(2024, 1, 7), (1, 2, 3, 4, 5, 7)),
        HistoryEntry("3/2024", date(2024, 1, 11), (10, 11, 12, 13, 14, 49)),
        HistoryEntry("4/2024", date(2024, 1, 14), (1, 8, 9, 10, 20, 30)),
    ]
    return make_history(entries)


class TestRegistry:
    def test_all_six_strategies_registered(self) -> None:
        assert set(STRATEGY_REGISTRY) == {"random", "hot", "cold", "gap", "balanced", "hybrid"}

    def test_create_strategy_returns_correct_type(self) -> None:
        assert isinstance(create_strategy("hot"), HotNumbersStrategy)
        assert isinstance(create_strategy("random", seed=1), RandomStrategy)

    def test_create_unknown_strategy_raises(self) -> None:
        with pytest.raises(KeyError):
            create_strategy("nonexistent")

    def test_every_strategy_exposes_name_and_description(self) -> None:
        for name, cls in STRATEGY_REGISTRY.items():
            instance = cls()
            assert instance.name == name
            assert instance.description
            assert isinstance(instance.parameters, dict)

    def test_cannot_instantiate_abstract_base(self) -> None:
        with pytest.raises(TypeError):
            PredictionStrategy()  # abstract: no predict() implementation


class TestPredictionInvariant:
    """Every strategy must return exactly main_count unique in-range numbers."""

    @pytest.mark.parametrize("name", sorted(STRATEGY_REGISTRY))
    def test_prediction_shape(self, name: str) -> None:
        strategy = create_strategy(name, seed=1) if name == "random" else create_strategy(name)
        prediction = strategy.predict(four_draw_history())
        assert len(prediction.numbers) == GAME.main_count
        assert len(set(prediction.numbers)) == GAME.main_count  # no duplicates
        assert all(GAME.main_min <= v <= GAME.main_max for v in prediction.numbers)
        assert prediction.numbers == tuple(sorted(prediction.numbers))
        assert prediction.strategy_name == name
        assert prediction.game_code == "6x49"
        assert prediction.prediction_date == date(2024, 1, 18)

    @pytest.mark.parametrize("name", sorted(STRATEGY_REGISTRY))
    def test_works_with_empty_history(self, name: str) -> None:
        strategy = create_strategy(name, seed=1) if name == "random" else create_strategy(name)
        empty_history = make_history([])
        assert empty_history.is_empty
        prediction = strategy.predict(empty_history)
        assert len(prediction.numbers) == GAME.main_count


class TestHotAndCold:
    def test_hot_picks_most_frequent(self) -> None:
        prediction = HotNumbersStrategy().predict(four_draw_history())
        # 1 appears 3x; 2,3,4,5,10 each appear 2x (tie broken by value ascending).
        assert prediction.numbers == (1, 2, 3, 4, 5, 10)

    def test_cold_picks_least_frequent(self) -> None:
        prediction = ColdNumbersStrategy().predict(four_draw_history())
        # Everything not drawn has frequency 0; lowest values win the tie, and
        # 20 is excluded (it was drawn once) so the run of zeros skips it.
        assert prediction.numbers == (15, 16, 17, 18, 19, 21)

    def test_window_limits_considered_history(self) -> None:
        # With window=1, only the most recent draw (4/2024) is considered,
        # so its six numbers are exactly the six frequency-1 candidates.
        prediction = HotNumbersStrategy(window=1).predict(four_draw_history())
        assert prediction.numbers == (1, 8, 9, 10, 20, 30)


class TestGap:
    def test_gap_measures_recency_not_just_frequency(self) -> None:
        # Two numbers tied at frequency 1: 40 seen only in the oldest draw,
        # 7 seen only in the most recent - gap must rank 40 as more overdue.
        entries = [
            HistoryEntry("1/2024", date(2024, 1, 4), (40, 1, 2, 3, 4, 6)),
            HistoryEntry("2/2024", date(2024, 1, 7), (10, 11, 12, 13, 14, 15)),
            HistoryEntry("3/2024", date(2024, 1, 11), (7, 20, 21, 22, 23, 24)),
        ]
        frequency, gap = _frequency_and_gap(GAME, entries)
        assert frequency[40] == frequency[7] == 1
        assert gap[40] == 2
        assert gap[7] == 0

    def test_never_seen_number_has_max_gap(self) -> None:
        history = four_draw_history()
        _, gap = _frequency_and_gap(GAME, history.entries)
        assert gap[25] == len(history.entries)


class TestTopN:
    def test_ties_broken_by_ascending_value(self) -> None:
        scores = {40: 1, 7: 1, 3: 5}
        assert _top_n(scores, 2, descending=True) == (3, 7)

    def test_ascending_selection(self) -> None:
        scores = {5: 3, 1: 1, 9: 2}
        assert _top_n(scores, 1, descending=False) == (1,)


class TestBalanced:
    def test_respects_low_high_split(self) -> None:
        prediction = BalancedStrategy(low_high_split=0.5).predict(four_draw_history())
        midpoint = (GAME.main_min + GAME.main_max + 1) // 2
        low_count = sum(1 for v in prediction.numbers if v < midpoint)
        assert low_count == 3  # 6 * 0.5

    def test_custom_split_shifts_low_count(self) -> None:
        prediction = BalancedStrategy(low_high_split=0.0).predict(four_draw_history())
        midpoint = (GAME.main_min + GAME.main_max + 1) // 2
        assert all(v >= midpoint for v in prediction.numbers)


class TestHybrid:
    def test_full_hot_weight_matches_hot_strategy(self) -> None:
        history = four_draw_history()
        hybrid = HybridStrategy(hot_weight=1.0).predict(history)
        hot = HotNumbersStrategy().predict(history)
        assert hybrid.numbers == hot.numbers

    def test_zero_hot_weight_matches_gap_strategy(self) -> None:
        history = four_draw_history()
        hybrid = HybridStrategy(hot_weight=0.0).predict(history)
        gap = GapStrategy().predict(history)
        assert hybrid.numbers == gap.numbers


class TestRandomDeterminism:
    def test_same_seed_same_sequence(self) -> None:
        history = four_draw_history()
        seq1 = [RandomStrategy(seed=99).predict(history).numbers for _ in range(5)]
        seq2 = [RandomStrategy(seed=99).predict(history).numbers for _ in range(5)]
        assert seq1 == seq2

    def test_different_seeds_diverge(self) -> None:
        history = four_draw_history()
        a = [RandomStrategy(seed=1).predict(history).numbers for _ in range(5)]
        b = [RandomStrategy(seed=2).predict(history).numbers for _ in range(5)]
        assert a != b

    def test_sequential_predictions_differ_within_one_run(self) -> None:
        strategy = RandomStrategy(seed=5)
        history = four_draw_history()
        first = strategy.predict(history).numbers
        second = strategy.predict(history).numbers
        assert first != second  # RNG advances between calls

    def test_metadata_records_seed(self) -> None:
        prediction = RandomStrategy(seed=123).predict(four_draw_history())
        assert prediction.metadata["seed"] == 123


class TestPredictionExport:
    def test_to_dict_from_dict_round_trip(self) -> None:
        original = Prediction(
            game_code="6x49",
            prediction_date=date(2026, 7, 20),
            numbers=(1, 2, 3, 4, 5, 6),
            strategy_name="hot",
            metadata={"window": None},
        )
        restored = Prediction.from_dict(original.to_dict())
        assert restored.game_code == original.game_code
        assert restored.prediction_date == original.prediction_date
        assert restored.numbers == original.numbers
        assert restored.strategy_name == original.strategy_name
        assert restored.metadata == original.metadata

    def test_save_and_load_predictions(self, tmp_path) -> None:
        predictions = [
            HotNumbersStrategy().predict(four_draw_history()),
            ColdNumbersStrategy().predict(four_draw_history()),
        ]
        path = tmp_path / "predictions.json"
        save_predictions(predictions, path)
        loaded = load_predictions(path)
        assert len(loaded) == 2
        assert loaded[0].numbers == predictions[0].numbers
        assert loaded[1].strategy_name == "cold"
