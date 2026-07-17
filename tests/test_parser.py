"""Parser tests against real captured pages (live 2026 + Wayback 2024)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.scraper.parser import ParseError, TotoParser


@pytest.fixture()
def parser() -> TotoParser:
    return TotoParser()


class TestLiveListPage:
    def test_latest_draw_parsed(self, parser: TotoParser, live_list_html: str) -> None:
        draw = parser.parse_draw_page(live_list_html, "6x49", "https://info.toto.bg/results/6x49")
        assert draw.draw_number == 55
        assert draw.draw_year == 2026
        assert draw.draw_date == date(2026, 7, 16)
        assert draw.numbers == (5, 10, 17, 20, 42, 47)
        assert draw.bonus_numbers == ()

    def test_sidebar_numbers_do_not_leak(self, parser: TotoParser, live_list_html: str) -> None:
        # The sidebar repeats ball numbers for all games; only the six numbers
        # of the main result block must be extracted.
        draw = parser.parse_draw_page(live_list_html, "6x49")
        assert len(draw.numbers) == 6

    def test_jackpot_in_euro(self, parser: TotoParser, live_list_html: str) -> None:
        draw = parser.parse_draw_page(live_list_html, "6x49")
        assert draw.jackpot_amount == Decimal("2724436.24")
        assert draw.currency == "EUR"

    def test_prize_tiers(self, parser: TotoParser, live_list_html: str) -> None:
        draw = parser.parse_draw_page(live_list_html, "6x49")
        assert [t.match_count for t in draw.prize_tiers] == [6, 5, 4, 3]
        tier5 = draw.prize_tiers[1]
        assert tier5.winners == 19
        assert tier5.prize_amount == Decimal("1281.80")
        assert tier5.currency == "EUR"

    def test_draw_links_extracted(self, parser: TotoParser, live_list_html: str) -> None:
        links = parser.parse_draw_links(live_list_html, "6x49")
        assert len(links) == 26
        assert links[0].endswith("/results/6x49/2026-55")
        assert links[-1].endswith("/results/6x49/2026-30")
        # No links of other games leak in.
        assert all("/6x49/" in link for link in links)


class TestWaybackSnapshot:
    def test_draw_parsed_with_bgn_currency(
        self, parser: TotoParser, wayback_draw_html: str
    ) -> None:
        draw = parser.parse_draw_page(wayback_draw_html, "6x49")
        assert draw.draw_number == 11
        assert draw.draw_year == 2024
        assert draw.draw_date == date(2024, 2, 8)
        assert draw.numbers == (5, 13, 31, 39, 41, 47)
        assert draw.jackpot_amount == Decimal("4239485.33")
        assert draw.currency == "BGN"

    def test_prize_tiers_bgn(self, parser: TotoParser, wayback_draw_html: str) -> None:
        draw = parser.parse_draw_page(wayback_draw_html, "6x49")
        tier5 = next(t for t in draw.prize_tiers if t.match_count == 5)
        assert tier5.winners == 12
        assert tier5.prize_amount == Decimal("3510.40")
        assert tier5.total_amount == Decimal("42124.80")
        assert tier5.currency == "BGN"


class TestErrorHandling:
    def test_page_without_results_raises(self, parser: TotoParser) -> None:
        with pytest.raises(ParseError):
            parser.parse_draw_page("<html><body>404 Page Not Found</body></html>", "6x49")

    def test_malformed_title_raises(self, parser: TotoParser) -> None:
        html = '<div class="tir_result"><h2 class="tir_title">Bad title</h2></div>'
        with pytest.raises(ParseError):
            parser.parse_draw_page(html, "6x49")

    def test_draw_ref_from_url(self, parser: TotoParser) -> None:
        assert parser.draw_ref_from_url("https://info.toto.bg/results/6x49/2026-55") == (2026, 55)
        assert parser.draw_ref_from_url("https://info.toto.bg/results/6x49") is None
