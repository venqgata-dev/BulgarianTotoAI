"""Statistics engine tests, hand-verified against a small fixed dataset."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.analysis.statistics import StatisticsService, is_prime
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
                source_url=f"test://{game_code}/{draw_date.year}-{number}#{drawing}",
            ),
            source="live",
        )


def seed_four_draws(database: Database) -> None:
    # Chronological order: draw1, draw2, draw3, draw4 (dates verified Thu/Sun).
    store(database, "6x49", 1, date(2024, 1, 4), (1, 2, 3, 4, 5, 6))
    store(database, "6x49", 2, date(2024, 1, 7), (1, 2, 3, 4, 5, 7))
    store(database, "6x49", 3, date(2024, 1, 11), (10, 11, 12, 13, 14, 49))
    store(database, "6x49", 4, date(2024, 1, 14), (1, 8, 9, 10, 20, 30))


class TestIsPrime:
    def test_known_values(self) -> None:
        assert [n for n in range(2, 20) if is_prime(n)] == [2, 3, 5, 7, 11, 13, 17, 19]
        assert not is_prime(1)
        assert not is_prime(0)
        assert not is_prime(-5)
        assert not is_prime(49)  # 7*7


class TestNumberStats:
    def test_frequency_and_streaks(self, database: Database) -> None:
        seed_four_draws(database)
        report = StatisticsService(database).analyze("6x49")
        by_value = {n.value: n for n in report.numbers}

        one = by_value[1]
        assert one.frequency == 3
        assert one.current_streak == 0  # appeared in the most recent draw
        assert one.longest_streak == 2
        assert one.average_gap == pytest.approx(1.5)

        two = by_value[2]
        assert two.frequency == 2
        assert two.current_streak == 2
        assert two.longest_streak == 2
        assert two.average_gap == pytest.approx(1.0)

        forty_nine = by_value[49]
        assert forty_nine.frequency == 1
        assert forty_nine.current_streak == 1
        assert forty_nine.longest_streak == 2
        assert forty_nine.average_gap is None
        assert forty_nine.first_seen_ref == "3/2024"

        never_drawn = by_value[25]
        assert never_drawn.frequency == 0
        assert never_drawn.current_streak == 4
        assert never_drawn.longest_streak == 4
        assert never_drawn.average_gap is None
        assert never_drawn.percentage == 0.0

    def test_percentage(self, database: Database) -> None:
        seed_four_draws(database)
        report = StatisticsService(database).analyze("6x49")
        by_value = {n.value: n for n in report.numbers}
        assert by_value[1].percentage == pytest.approx(75.0)  # 3 of 4 draws

    def test_hottest_and_coldest(self, database: Database) -> None:
        seed_four_draws(database)
        report = StatisticsService(database).analyze("6x49", top_n=1)
        assert report.hottest[0].value == 1
        assert report.hottest[0].frequency == 3
        # coldest: many numbers tie at 0; lowest value wins the tie-break.
        assert report.coldest[0].frequency == 0

    def test_all_49_numbers_present_even_if_never_drawn(self, database: Database) -> None:
        seed_four_draws(database)
        report = StatisticsService(database).analyze("6x49")
        assert [n.value for n in report.numbers] == list(range(1, 50))


class TestDrawStats:
    def test_sums_and_ratios(self, database: Database) -> None:
        seed_four_draws(database)
        report = StatisticsService(database).analyze("6x49")
        draw1, draw2, draw3, draw4 = report.draws

        assert draw1.total_sum == 21
        assert draw1.odd_count == 3 and draw1.even_count == 3
        assert draw1.low_count == 6 and draw1.high_count == 0  # midpoint is 25
        assert draw1.consecutive_count == 5  # 1-2-3-4-5-6 all adjacent
        assert draw1.repeated_from_previous is None  # no previous draw

        assert draw2.repeated_from_previous == 5  # shares 1,2,3,4,5 with draw1
        assert draw3.repeated_from_previous == 0
        assert draw4.repeated_from_previous == 1  # shares only 10

    def test_high_low_split(self, database: Database) -> None:
        seed_four_draws(database)
        report = StatisticsService(database).analyze("6x49")
        draw3 = report.draws[2]  # (10, 11, 12, 13, 14, 49)
        assert draw3.low_count == 5  # 10..14 < 25
        assert draw3.high_count == 1  # 49 >= 25


class TestComboStats:
    def test_pair_frequency(self, database: Database) -> None:
        seed_four_draws(database)
        report = StatisticsService(database).analyze("6x49", top_n=100)
        pairs = {c.numbers: c.frequency for c in report.most_common_pairs}
        assert pairs[(1, 2)] == 2  # co-occurs in draw1 and draw2
        assert pairs[(1, 3)] == 2

    def test_triplet_frequency(self, database: Database) -> None:
        seed_four_draws(database)
        report = StatisticsService(database).analyze("6x49", top_n=100)
        triplets = {c.numbers: c.frequency for c in report.most_common_triplets}
        assert triplets[(1, 2, 3)] == 2

    def test_least_common_excludes_never_occurred(self, database: Database) -> None:
        seed_four_draws(database)
        report = StatisticsService(database).analyze("6x49", top_n=5)
        # Every returned "least common" combo must have actually occurred at least once.
        assert all(c.frequency >= 1 for c in report.least_common_pairs)
        assert all(c.frequency >= 1 for c in report.least_common_triplets)


class TestDistribution:
    def test_even_odd_and_prime_counts(self, database: Database) -> None:
        seed_four_draws(database)
        report = StatisticsService(database).analyze("6x49")
        d = report.distribution
        assert d.even_count == 12
        assert d.odd_count == 12
        assert d.prime_count == 9
        assert d.non_prime_count == 15

    def test_decade_and_last_digit_totals(self, database: Database) -> None:
        seed_four_draws(database)
        report = StatisticsService(database).analyze("6x49")
        d = report.distribution
        assert sum(d.decade.values()) == 24
        assert sum(d.last_digit.values()) == 24
        # 1-10 bucket: draw1 all 6, draw2 all 6, draw3 just "10", draw4 has 1,8,9,10.
        assert d.decade["1-10"] == 17

    def test_heatmap_matches_frequency(self, database: Database) -> None:
        seed_four_draws(database)
        report = StatisticsService(database).analyze("6x49")
        by_value = {n.value: n.frequency for n in report.numbers}
        for value, freq in report.distribution.heatmap.items():
            assert freq == by_value[value]


class TestScope:
    def test_last_n_limits_observations(self, database: Database) -> None:
        seed_four_draws(database)
        report = StatisticsService(database).analyze("6x49", last_n=2)
        assert report.draw_count == 2
        assert report.draws[0].ref == "3/2024"
        assert report.draws[1].ref == "4/2024"
        assert "last 2 draws" in report.scope

    def test_years_filters(self, database: Database) -> None:
        seed_four_draws(database)
        store(database, "6x49", 1, date(2025, 1, 2), (1, 2, 3, 4, 5, 6))
        report = StatisticsService(database).analyze("6x49", years=[2025])
        assert report.draw_count == 1
        assert "2025" in report.scope

    def test_empty_scope_does_not_crash(self, database: Database) -> None:
        seed_four_draws(database)
        report = StatisticsService(database).analyze("6x49", years=[1999])
        assert report.draw_count == 0
        assert report.draws == []
        assert all(n.frequency == 0 for n in report.hottest)
        assert report.distribution.even_count == 0
        by_value = {n.value: n for n in report.numbers}
        assert by_value[1].current_streak == 0
        assert by_value[1].percentage == 0.0


class TestReportRendering:
    def test_to_text_smoke(self, database: Database) -> None:
        seed_four_draws(database)
        text = StatisticsService(database).analyze("6x49").to_text()
        assert "STATISTICS REPORT" in text
        assert "Hot numbers" in text
        assert "Most common pairs" in text
        assert "Distribution" in text

    def test_write_json_and_csv(self, database: Database, tmp_path) -> None:
        import csv
        import json

        seed_four_draws(database)
        report = StatisticsService(database).analyze("6x49")

        json_path = tmp_path / "stats.json"
        report.write_json(json_path)
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        assert payload["game_code"] == "6x49"
        assert len(payload["numbers"]) == 49

        csv_path = tmp_path / "stats.csv"
        report.write_csv(csv_path)
        with csv_path.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == 49
        assert {r["value"] for r in rows} == {str(v) for v in range(1, 50)}
