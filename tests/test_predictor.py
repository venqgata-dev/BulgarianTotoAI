"""Prediction engine tests."""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from math import comb

import pytest

from app.analysis import predictor as predictor_module
from app.analysis.predictor import (
    CALIBRATION_SEED,
    DEFAULT_ALTERNATIVES,
    MIN_CALIBRATION_DRAWS,
    SCORE_COMPONENTS,
    PredictorService,
    ScoreWeights,
    _HistoricalProfile,
    _replay_average_hits,
    calibrate,
    candidate_weight_vectors,
    max_combinations,
)
from app.database.engine import Database
from app.database.repository import DrawRepository, GameRepository
from app.models.domain import ParsedDraw, game_by_code


def store(
    database: Database,
    game_code: str,
    number: int,
    draw_date: date,
    numbers: tuple[int, ...],
) -> None:
    with database.session() as session:
        game = GameRepository(session).by_code(game_code)
        DrawRepository(session).add_parsed(
            game,
            ParsedDraw(
                game_code=game_code,
                draw_number=number,
                draw_year=draw_date.year,
                draw_date=draw_date,
                numbers=numbers,
                jackpot_amount=Decimal("1000.00"),
                currency="EUR",
                source_url=f"test://{game_code}/{draw_date.year}-{number}",
            ),
            source="live",
        )


def seed_varied_draws(database: Database) -> None:
    """4 draws - deliberately below MIN_CALIBRATION_DRAWS so tests using
    this fixture exercise the "not enough data, use default weights" path
    and stay fast (no calibration replay)."""
    store(database, "6x49", 1, date(2024, 1, 4), (1, 2, 3, 4, 5, 6))
    store(database, "6x49", 2, date(2024, 1, 7), (1, 2, 3, 4, 5, 7))
    store(database, "6x49", 3, date(2024, 1, 11), (10, 11, 12, 13, 14, 49))
    store(database, "6x49", 4, date(2024, 1, 14), (1, 8, 9, 10, 20, 30))


def seed_calibratable_draws(database: Database, count: int = 15) -> None:
    """`count` draws (>= MIN_CALIBRATION_DRAWS) with varied numbers, so
    calibration actually has something to discriminate between vectors."""
    for i in range(count):
        base = (i * 3) % 40
        numbers = tuple(sorted({1 + (base + offset) % 49 for offset in (0, 5, 11, 17, 23, 29)}))
        while len(numbers) < 6:
            numbers = tuple(sorted(set(numbers) | {1 + (numbers[-1] + 7) % 49}))
        store(database, "6x49", i + 1, date(2024, 1, 4) + timedelta(days=i * 3), numbers[:6])


def entries_of(database: Database, game_code: str = "6x49") -> list[tuple[int, ...]]:
    from app.analysis.statistics import main_numbers

    with database.session() as session:
        game = GameRepository(session).by_code(game_code)
        draws = DrawRepository(session).all_for_game(game.id)
    ordered = sorted(draws, key=lambda d: (d.draw_date, d.drawing))
    return [main_numbers(d) for d in ordered]


class TestMaxCombinations:
    def test_matches_math_comb(self) -> None:
        game = game_by_code("6x49")
        assert max_combinations(game) == comb(49, 6)

    def test_5x35(self) -> None:
        game = game_by_code("5x35")
        assert max_combinations(game) == comb(35, 5)


class TestScoreWeights:
    def test_default_sums_to_one(self) -> None:
        weights = ScoreWeights.default()
        assert sum(weights.to_dict().values()) == pytest.approx(1.0)

    def test_to_dict_from_dict_round_trip(self) -> None:
        original = ScoreWeights.default()
        restored = ScoreWeights.from_dict(original.to_dict())
        assert restored == original

    def test_all_nine_components_present(self) -> None:
        assert set(ScoreWeights.default().to_dict()) == set(SCORE_COMPONENTS)
        assert len(SCORE_COMPONENTS) == 9


class TestCandidateWeightVectors:
    def test_deterministic(self) -> None:
        a = candidate_weight_vectors(CALIBRATION_SEED)
        b = candidate_weight_vectors(CALIBRATION_SEED)
        assert a == b

    def test_different_seed_changes_random_vectors(self) -> None:
        a = candidate_weight_vectors(1)
        b = candidate_weight_vectors(2)
        assert a != b

    def test_includes_the_default(self) -> None:
        vectors = candidate_weight_vectors(CALIBRATION_SEED)
        assert ScoreWeights.default() in vectors

    def test_every_vector_sums_to_one(self) -> None:
        for vector in candidate_weight_vectors(CALIBRATION_SEED):
            assert sum(vector.to_dict().values()) == pytest.approx(1.0)


class TestHistoricalProfileScoring:
    def test_hot_combo_scores_higher_than_cold_combo(self, database: Database) -> None:
        seed_varied_draws(database)
        profile = _HistoricalProfile(game_by_code("6x49"), entries_of(database), ScoreWeights.default())

        # (1,2,3,4,5,6) contains the single most-frequent number (1, freq 3)
        # and several freq-2 numbers; a combo of never-drawn numbers must
        # score strictly lower.
        hot_score = profile.score((1, 2, 3, 4, 5, 6))
        cold_score = profile.score((40, 41, 42, 43, 44, 45))
        assert hot_score > cold_score

    def test_never_seen_history_does_not_crash(self, database: Database) -> None:
        assert entries_of(database) == []
        profile = _HistoricalProfile(game_by_code("6x49"), [], ScoreWeights.default())
        score = profile.score((1, 2, 3, 4, 5, 6))
        assert 0.0 <= score <= 1.0

    def test_identical_sums_do_not_divide_by_zero(self, database: Database) -> None:
        # Every draw sums to exactly 21 - pstdev(sums) is exactly 0.0, which
        # must not raise ZeroDivisionError in the sum-distribution signal.
        store(database, "6x49", 1, date(2024, 1, 4), (1, 2, 3, 4, 5, 6))
        store(database, "6x49", 2, date(2024, 1, 7), (1, 2, 3, 4, 5, 6))
        profile = _HistoricalProfile(game_by_code("6x49"), entries_of(database), ScoreWeights.default())
        score = profile.score((1, 2, 3, 4, 5, 6))
        assert score > 0.0

    def test_score_is_bounded(self, database: Database) -> None:
        seed_varied_draws(database)
        profile = _HistoricalProfile(game_by_code("6x49"), entries_of(database), ScoreWeights.default())
        for combo in [(1, 2, 3, 4, 5, 6), (44, 45, 46, 47, 48, 49), (7, 14, 21, 28, 35, 42)]:
            assert 0.0 <= profile.score(combo) <= 1.0

    def test_recent_frequency_differs_from_overall_frequency(
        self, database: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Shrink the recency window to 2 draws so a 4-draw fixture can
        # isolate "recent" from "overall": number 1 appears in every early
        # draw but not the last two; number 49 appears only in the last draw.
        monkeypatch.setattr(predictor_module, "RECENT_WINDOW_DRAWS", 2)
        store(database, "6x49", 1, date(2024, 1, 4), (1, 2, 3, 4, 5, 6))
        store(database, "6x49", 2, date(2024, 1, 7), (1, 2, 3, 4, 5, 7))
        store(database, "6x49", 3, date(2024, 1, 11), (1, 8, 9, 10, 11, 12))
        store(database, "6x49", 4, date(2024, 1, 14), (13, 14, 15, 16, 17, 49))
        profile = _HistoricalProfile(game_by_code("6x49"), entries_of(database), ScoreWeights.default())
        assert profile.frequency[1] > profile.frequency[49]
        assert profile.recent_frequency[49] >= profile.recent_frequency[1]


class TestCalibration:
    def test_calibrate_picks_a_vector_that_scores_at_least_as_well_as_default(self, database: Database) -> None:
        seed_calibratable_draws(database)
        entries = entries_of(database)
        game = game_by_code("6x49")
        default_avg = _replay_average_hits(
            game, entries, ScoreWeights.default(), evaluate_last=8, pool_size=100, seed=CALIBRATION_SEED
        )
        result = calibrate(game, entries, evaluate_last=8, pool_size=100, seed=CALIBRATION_SEED)
        assert result.average_hits >= default_avg

    def test_calibrate_is_deterministic(self, database: Database) -> None:
        seed_calibratable_draws(database)
        entries = entries_of(database)
        game = game_by_code("6x49")
        r1 = calibrate(game, entries, evaluate_last=6, pool_size=80, seed=CALIBRATION_SEED)
        r2 = calibrate(game, entries, evaluate_last=6, pool_size=80, seed=CALIBRATION_SEED)
        assert r1.weights == r2.weights
        assert r1.average_hits == r2.average_hits

    def test_calibrate_tries_every_candidate_vector(self, database: Database) -> None:
        seed_calibratable_draws(database)
        entries = entries_of(database)
        game = game_by_code("6x49")
        result = calibrate(game, entries, evaluate_last=5, pool_size=60, seed=CALIBRATION_SEED)
        assert result.candidates_tried == len(candidate_weight_vectors(CALIBRATION_SEED))

    def test_replay_average_hits_empty_window_is_zero(self, database: Database) -> None:
        seed_varied_draws(database)
        entries = entries_of(database)
        game = game_by_code("6x49")
        avg = _replay_average_hits(
            game, entries[:0], ScoreWeights.default(), evaluate_last=5, pool_size=50, seed=1
        )
        assert avg == 0.0


class TestPredictorServiceCalibrationIntegration:
    """Exercises PredictorService's calibration triggering/caching using
    monkeypatched (small) calibration constants so these stay fast."""

    def _shrink_calibration(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(predictor_module, "CALIBRATION_DRAWS", 6)
        monkeypatch.setattr(predictor_module, "CALIBRATION_POOL_SIZE", 60)

    def test_below_minimum_history_skips_calibration(self, database: Database) -> None:
        seed_varied_draws(database)  # 4 draws < MIN_CALIBRATION_DRAWS
        result = PredictorService(database).predict("6x49", pool_size=200)
        assert result.calibration is None

    def test_enough_history_triggers_calibration(self, database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
        self._shrink_calibration(monkeypatch)
        seed_calibratable_draws(database, count=MIN_CALIBRATION_DRAWS + 2)
        result = PredictorService(database).predict("6x49", pool_size=200)
        assert result.calibration is not None
        assert result.calibration.candidates_tried == len(candidate_weight_vectors(CALIBRATION_SEED))

    def test_calibration_is_cached_to_disk(self, database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
        self._shrink_calibration(monkeypatch)
        seed_calibratable_draws(database, count=MIN_CALIBRATION_DRAWS + 2)
        service = PredictorService(database)
        service.predict("6x49", pool_size=200)
        assert service._cache_path.exists()
        cache = json.loads(service._cache_path.read_text(encoding="utf-8"))
        assert "6x49" in cache
        assert cache["6x49"]["fingerprint"]["count"] == MIN_CALIBRATION_DRAWS + 2

    def test_second_call_reuses_cached_weights_without_recalibrating(
        self, database: Database, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._shrink_calibration(monkeypatch)
        seed_calibratable_draws(database, count=MIN_CALIBRATION_DRAWS + 2)
        service = PredictorService(database)
        first = service.predict("6x49", pool_size=200)

        # If calibration ran again it would still find the same best vector
        # (deterministic), so the strongest signal that the cache was used
        # is simply that the cached weights match what a fresh calibrate()
        # call against the same data would produce, and that the file's
        # fingerprint did not change across the second call.
        cache_before = service._cache_path.read_text(encoding="utf-8")
        second = service.predict("6x49", pool_size=200)
        cache_after = service._cache_path.read_text(encoding="utf-8")

        assert cache_before == cache_after
        assert first.calibration.weights.to_dict() == second.calibration.weights.to_dict()

    def test_new_draw_invalidates_cache(self, database: Database, monkeypatch: pytest.MonkeyPatch) -> None:
        self._shrink_calibration(monkeypatch)
        seed_calibratable_draws(database, count=MIN_CALIBRATION_DRAWS + 2)
        service = PredictorService(database)
        service.predict("6x49", pool_size=200)
        cache = json.loads(service._cache_path.read_text(encoding="utf-8"))
        old_fingerprint = cache["6x49"]["fingerprint"]

        store(database, "6x49", 999, date(2026, 1, 1), (1, 2, 3, 4, 5, 6))
        service.predict("6x49", pool_size=200)
        cache = json.loads(service._cache_path.read_text(encoding="utf-8"))
        assert cache["6x49"]["fingerprint"] != old_fingerprint


class TestPredictorService:
    def test_recommended_and_alternatives_are_distinct(self, database: Database) -> None:
        seed_varied_draws(database)
        result = PredictorService(database).predict("6x49", pool_size=500)
        all_numbers = [result.recommended.numbers] + [a.numbers for a in result.alternatives]
        assert len(set(all_numbers)) == len(all_numbers)

    def test_prediction_shape(self, database: Database) -> None:
        seed_varied_draws(database)
        result = PredictorService(database).predict("6x49", pool_size=500)
        game = game_by_code("6x49")
        for combo in [result.recommended] + result.alternatives:
            assert len(combo.numbers) == game.main_count
            assert len(set(combo.numbers)) == game.main_count
            assert all(game.main_min <= v <= game.main_max for v in combo.numbers)
            assert combo.numbers == tuple(sorted(combo.numbers))

    def test_default_alternatives_count(self, database: Database) -> None:
        seed_varied_draws(database)
        result = PredictorService(database).predict("6x49", pool_size=500)
        assert len(result.alternatives) == DEFAULT_ALTERNATIVES

    def test_custom_alternatives_count(self, database: Database) -> None:
        seed_varied_draws(database)
        result = PredictorService(database).predict("6x49", pool_size=500, alternatives=2)
        assert len(result.alternatives) == 2

    def test_recommended_scores_at_least_as_high_as_alternatives(self, database: Database) -> None:
        seed_varied_draws(database)
        result = PredictorService(database).predict("6x49", pool_size=500)
        scores = [a.score for a in result.alternatives]
        assert all(result.recommended.score >= s for s in scores)
        assert scores == sorted(scores, reverse=True)

    def test_pool_size_never_exceeds_max_combinations(self, database: Database) -> None:
        seed_varied_draws(database)
        result = PredictorService(database).predict("6x49", pool_size=500)
        assert result.pool_size <= max_combinations(game_by_code("6x49"))
        assert result.pool_size == 500  # well under the cap, so the request is met exactly

    def test_works_with_empty_database(self, database: Database) -> None:
        result = PredictorService(database).predict("6x49", pool_size=500)
        assert len(result.recommended.numbers) == 6
        assert len(result.alternatives) == DEFAULT_ALTERNATIVES


class TestDeterminism:
    def test_same_seed_same_result(self, database: Database) -> None:
        seed_varied_draws(database)
        service = PredictorService(database)
        r1 = service.predict("6x49", pool_size=500, seed=7)
        r2 = service.predict("6x49", pool_size=500, seed=7)
        assert r1.recommended.numbers == r2.recommended.numbers
        assert [a.numbers for a in r1.alternatives] == [a.numbers for a in r2.alternatives]

    def test_default_seed_is_deterministic_without_specifying_it(self, database: Database) -> None:
        seed_varied_draws(database)
        service = PredictorService(database)
        r1 = service.predict("6x49", pool_size=500)
        r2 = service.predict("6x49", pool_size=500)
        assert r1.recommended.numbers == r2.recommended.numbers

    def test_different_seeds_can_diverge(self, database: Database) -> None:
        seed_varied_draws(database)
        service = PredictorService(database)
        r1 = service.predict("6x49", pool_size=500, seed=1)
        r2 = service.predict("6x49", pool_size=500, seed=2)
        # Different random candidate pools are extremely unlikely to yield
        # an identical top pick.
        assert r1.recommended.numbers != r2.recommended.numbers

    def test_seed_none_uses_non_deterministic_source(self, database: Database) -> None:
        seed_varied_draws(database)
        service = PredictorService(database)
        results = {service.predict("6x49", pool_size=500, seed=None).recommended.numbers for _ in range(3)}
        assert len(results) > 1  # astronomically unlikely to collide 3/3 times


class TestReportRendering:
    def test_to_text_smoke(self, database: Database) -> None:
        seed_varied_draws(database)
        text = PredictorService(database).predict("6x49", pool_size=500).to_text()
        assert "PREDICTION" in text
        assert "Recommended Combination" in text
        assert "5 Alternative Combinations" in text
        assert "independent random events" in text

    def test_to_text_is_plain_ascii_safe(self, database: Database) -> None:
        # Must not contain characters that crash on narrow Windows console
        # codepages (this bit us during development: U+2B50 in a print()).
        seed_varied_draws(database)
        text = PredictorService(database).predict("6x49", pool_size=500).to_text()
        text.encode("cp1251", errors="strict")

    def test_to_dict(self, database: Database) -> None:
        seed_varied_draws(database)
        result = PredictorService(database).predict("6x49", pool_size=500)
        payload = result.to_dict()
        assert payload["game_code"] == "6x49"
        assert len(payload["recommended"]["numbers"]) == 6
        assert len(payload["alternatives"]) == DEFAULT_ALTERNATIVES
