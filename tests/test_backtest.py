"""Backtesting engine tests, hand-verified against a small fixed dataset.

Fixture (game 6x49, chronological order):
    d1  1/2024  2024-01-04  (1, 2, 3, 4, 5, 6)
    d2  2/2024  2024-01-07  (1, 2, 3, 4, 5, 7)
    d3  3/2024  2024-01-11  (10, 11, 12, 13, 14, 49)
    d4  4/2024  2024-01-14  (1, 8, 9, 10, 20, 30)

With the ``hot`` strategy (ties broken by ascending value - see
app/analysis/strategies.py) the predicted numbers for each target, using
only strictly-earlier draws as history, are:
    d1: history=[]                 -> (1,2,3,4,5,6)  [all tied at 0]
    d2: history=[d1]                -> (1,2,3,4,5,6)  [d1's 6 tied at 1]
    d3: history=[d1,d2]             -> (1,2,3,4,5,6)  [1-5 tied at 2, 6 is the
                                                        lowest of the freq-1 group]
    d4: history=[d1,d2,d3]          -> (1,2,3,4,5,6)  [same reasoning]

Giving hit counts [6, 5, 0, 1] against the actual draws.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.analysis.backtest import BacktestService, compute_metrics
from app.analysis.strategies import STRATEGY_REGISTRY
from app.database.engine import Database
from app.database.repository import DrawRepository, GameRepository
from app.models.domain import ParsedDraw


def store(
    database: Database,
    game_code: str,
    number: int,
    draw_date: date,
    numbers: tuple[int, ...],
    drawing: int = 1,
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
                drawing=drawing,
                numbers=numbers,
                jackpot_amount=Decimal("1000.00"),
                currency="EUR",
                source_url=f"test://{game_code}/{draw_date.year}-{number}",
            ),
            source="live",
        )


def seed_four_draws(database: Database) -> None:
    store(database, "6x49", 1, date(2024, 1, 4), (1, 2, 3, 4, 5, 6))
    store(database, "6x49", 2, date(2024, 1, 7), (1, 2, 3, 4, 5, 7))
    store(database, "6x49", 3, date(2024, 1, 11), (10, 11, 12, 13, 14, 49))
    store(database, "6x49", 4, date(2024, 1, 14), (1, 8, 9, 10, 20, 30))


class TestRunCorrectness:
    def test_hit_counts_match_hand_computation(self, database: Database) -> None:
        seed_four_draws(database)
        report = BacktestService(database).run("6x49", "hot")
        assert [r.hit_count for r in report.records] == [6, 5, 0, 1]

    def test_first_draw_has_empty_history(self, database: Database) -> None:
        seed_four_draws(database)
        report = BacktestService(database).run("6x49", "hot")
        first = report.records[0]
        assert first.predicted_numbers == (1, 2, 3, 4, 5, 6)
        assert first.actual_numbers == (1, 2, 3, 4, 5, 6)
        assert first.matching_numbers == (1, 2, 3, 4, 5, 6)
        assert first.missed_numbers == ()

    def test_matching_and_missed_numbers(self, database: Database) -> None:
        seed_four_draws(database)
        report = BacktestService(database).run("6x49", "hot")
        second = report.records[1]  # actual (1,2,3,4,5,7), predicted (1,2,3,4,5,6)
        assert second.matching_numbers == (1, 2, 3, 4, 5)
        assert second.missed_numbers == (7,)
        assert second.hit_count == 5

    def test_draw_refs_are_correct(self, database: Database) -> None:
        seed_four_draws(database)
        report = BacktestService(database).run("6x49", "hot")
        assert [r.draw_ref for r in report.records] == ["1/2024", "2/2024", "3/2024", "4/2024"]

    def test_history_never_includes_target_or_future_draws(self, database: Database) -> None:
        # A strategy that just echoes back what it saw would prove this, but
        # simplest here: gap/hot on the first draw must see nothing, meaning
        # ties break to the lowest values, not something derived from d1 itself.
        seed_four_draws(database)
        report = BacktestService(database).run("6x49", "cold")
        assert report.records[0].predicted_numbers == (1, 2, 3, 4, 5, 6)


class TestMetrics:
    def test_metrics_hand_verified(self, database: Database) -> None:
        seed_four_draws(database)
        report = BacktestService(database).run("6x49", "hot")
        m = report.metrics
        assert m.predictions == 4
        assert m.average_hits == pytest.approx(3.0)
        assert m.median_hits == pytest.approx(3.0)
        assert m.max_hits == 6
        assert m.min_hits == 0
        assert m.hit_distribution == {0: 1, 1: 1, 2: 0, 3: 0, 4: 0, 5: 1, 6: 1}
        assert m.hit_percentage_3_plus == pytest.approx(50.0)
        assert m.hit_percentage_4_plus == pytest.approx(50.0)
        assert m.hit_percentage_5_plus == pytest.approx(50.0)
        assert m.hit_percentage_6 == pytest.approx(25.0)
        assert m.longest_winning_streak == 2
        assert m.longest_losing_streak == 2
        assert m.average_score == pytest.approx(50.0)

    def test_compute_metrics_empty_records(self) -> None:
        m = compute_metrics([], main_count=6)
        assert m.predictions == 0
        assert m.average_hits == 0.0
        assert m.hit_distribution == {k: 0 for k in range(7)}
        assert m.longest_winning_streak == 0
        assert m.longest_losing_streak == 0


class TestScope:
    def test_last_n_restricts_targets_but_keeps_full_history(self, database: Database) -> None:
        seed_four_draws(database)
        report = BacktestService(database).run("6x49", "hot", last_n=2)
        # Only d3, d4 are targets, but their history still includes d1/d2.
        assert [r.draw_ref for r in report.records] == ["3/2024", "4/2024"]
        assert [r.hit_count for r in report.records] == [0, 1]

    def test_years_filter(self, database: Database) -> None:
        seed_four_draws(database)
        store(database, "6x49", 1, date(2025, 1, 2), (1, 2, 3, 4, 5, 6))
        report = BacktestService(database).run("6x49", "hot", years=[2025])
        assert len(report.records) == 1
        assert report.records[0].draw_ref == "1/2025"

    def test_date_range_filter(self, database: Database) -> None:
        seed_four_draws(database)
        report = BacktestService(database).run(
            "6x49", "hot", date_from=date(2024, 1, 10), date_to=date(2024, 1, 12)
        )
        assert [r.draw_ref for r in report.records] == ["3/2024"]

    def test_empty_scope_returns_no_predictions(self, database: Database) -> None:
        seed_four_draws(database)
        report = BacktestService(database).run("6x49", "hot", years=[1999])
        assert report.records == []
        assert report.metrics.predictions == 0


class TestDeterminism:
    def test_running_twice_is_identical(self, database: Database) -> None:
        seed_four_draws(database)
        service = BacktestService(database)
        report1 = service.run("6x49", "random", strategy_params={"seed": 7})
        report2 = service.run("6x49", "random", strategy_params={"seed": 7})
        assert [r.predicted_numbers for r in report1.records] == [r.predicted_numbers for r in report2.records]
        assert report1.metrics.to_dict() == report2.metrics.to_dict()

    def test_deterministic_strategies_are_naturally_repeatable(self, database: Database) -> None:
        seed_four_draws(database)
        service = BacktestService(database)
        for name in ("hot", "cold", "gap", "balanced", "hybrid"):
            r1 = service.run("6x49", name)
            r2 = service.run("6x49", name)
            assert [r.predicted_numbers for r in r1.records] == [r.predicted_numbers for r in r2.records]


class TestComparison:
    def test_compare_all_strategies(self, database: Database) -> None:
        seed_four_draws(database)
        service = BacktestService(database)
        report = service.compare("6x49", sorted(STRATEGY_REGISTRY), strategy_params={"random": {"seed": 1}})
        assert {row.strategy_name for row in report.rows} == set(STRATEGY_REGISTRY)
        for row in report.rows:
            assert row.predictions == 4

    def test_compare_matches_individual_runs(self, database: Database) -> None:
        seed_four_draws(database)
        service = BacktestService(database)
        compare_report = service.compare("6x49", ["hot"])
        run_report = service.run("6x49", "hot")
        row = compare_report.rows[0]
        assert row.average_hits == pytest.approx(run_report.metrics.average_hits)
        assert row.max_hits == run_report.metrics.max_hits


class TestReportRendering:
    def test_to_text_smoke(self, database: Database) -> None:
        seed_four_draws(database)
        text = BacktestService(database).run("6x49", "hot").to_text()
        assert "BACKTEST REPORT" in text
        assert "hot" in text
        assert "Hit distribution" in text

    def test_to_text_handles_no_predictions(self, database: Database) -> None:
        seed_four_draws(database)
        text = BacktestService(database).run("6x49", "hot", years=[1999]).to_text()
        assert "No predictions" in text

    def test_comparison_to_text_smoke(self, database: Database) -> None:
        seed_four_draws(database)
        text = BacktestService(database).compare("6x49", ["hot", "cold"]).to_text()
        assert "STRATEGY COMPARISON" in text
        assert "hot" in text and "cold" in text


class TestExport:
    def test_write_json(self, database: Database, tmp_path) -> None:
        import json

        seed_four_draws(database)
        report = BacktestService(database).run("6x49", "hot")
        path = tmp_path / "backtest.json"
        report.write_json(path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["strategy_name"] == "hot"
        assert payload["metrics"]["predictions"] == 4
        assert len(payload["records"]) == 4

    def test_write_csv(self, database: Database, tmp_path) -> None:
        import csv

        seed_four_draws(database)
        report = BacktestService(database).run("6x49", "hot")
        path = tmp_path / "backtest.csv"
        report.write_csv(path)
        with path.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 4
        assert rows[0]["draw_ref"] == "1/2024"
        assert rows[0]["hit_count"] == "6"

    def test_comparison_write_csv(self, database: Database, tmp_path) -> None:
        import csv

        seed_four_draws(database)
        report = BacktestService(database).compare("6x49", ["hot", "cold"])
        path = tmp_path / "comparison.csv"
        report.write_csv(path)
        with path.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert {r["strategy_name"] for r in rows} == {"hot", "cold"}


class TestEdgeCases:
    def test_no_draws_in_database(self, database: Database) -> None:
        report = BacktestService(database).run("6x49", "hot")
        assert report.records == []
        assert report.metrics.predictions == 0

    def test_single_draw_only_target_has_empty_history(self, database: Database) -> None:
        store(database, "6x49", 1, date(2024, 1, 4), (1, 2, 3, 4, 5, 6))
        report = BacktestService(database).run("6x49", "hot")
        assert len(report.records) == 1
        assert report.records[0].predicted_numbers == (1, 2, 3, 4, 5, 6)

    def test_unknown_strategy_raises(self, database: Database) -> None:
        seed_four_draws(database)
        with pytest.raises(KeyError):
            BacktestService(database).run("6x49", "nonexistent")
