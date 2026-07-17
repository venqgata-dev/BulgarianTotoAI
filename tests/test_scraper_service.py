"""Scraper orchestration tests with a fake fetcher (no network access)."""

from __future__ import annotations

import pytest

from app.database.engine import Database
from app.database.repository import DrawRepository, GameRepository
from app.scraper.fetch import FetchError
from app.scraper.service import ImportStats, ScraperService


class FakeFetcher:
    """Serves canned HTML per URL and counts requests."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requests: list[str] = []

    def fetch(self, url: str) -> str:
        self.requests.append(url)
        if url not in self.pages:
            raise FetchError(f"HTTP 404 for {url}")
        return self.pages[url]


@pytest.fixture()
def live_pages(live_list_html: str) -> dict[str, str]:
    pages = {"https://info.toto.bg/results/6x49": live_list_html}
    # Serve the same full page for each linked draw URL: the parser reads the
    # embedded tir_result block, so every URL yields draw 55/2026. Only the
    # first stored one is imported; the rest are duplicate-detected.
    for number in range(30, 56):
        pages[f"https://info.toto.bg/results/6x49/2026-{number}"] = live_list_html
    return pages


def make_service(database: Database, fetcher: FakeFetcher) -> ScraperService:
    return ScraperService(database, live_fetcher=fetcher, archive_fetcher=fetcher)


def test_live_import_stores_draws_and_checkpoints(
    database: Database, live_pages: dict[str, str]
) -> None:
    fetcher = FakeFetcher(live_pages)
    stats = make_service(database, fetcher).import_live(["6x49"])

    assert stats.draws_imported == 1  # all pages carry the same draw content
    assert stats.failures == 0
    with database.session() as session:
        game = GameRepository(session).by_code("6x49")
        assert DrawRepository(session).count(game.id) == 1


def test_live_import_resumes_without_refetching(
    database: Database, live_pages: dict[str, str]
) -> None:
    fetcher = FakeFetcher(live_pages)
    service = make_service(database, fetcher)
    service.import_live(["6x49"])
    first_run_requests = len(fetcher.requests)

    stats = make_service(database, fetcher).import_live(["6x49"])
    # Second run only refetches the list page; all draw URLs are checkpointed
    # (or resolved as existing) without hitting the network.
    assert len(fetcher.requests) == first_run_requests + 1
    assert stats.draws_imported == 0
    assert stats.skipped_checkpointed + stats.skipped_existing >= 26


def test_fetch_failures_are_recorded_not_fatal(database: Database, live_list_html: str) -> None:
    pages = {"https://info.toto.bg/results/6x49": live_list_html}  # draw URLs all 404
    fetcher = FakeFetcher(pages)
    stats = make_service(database, fetcher).import_live(["6x49"])

    assert stats.failures > 0
    assert stats.errors
    # The latest draw from the list page itself is still imported.
    with database.session() as session:
        game = GameRepository(session).by_code("6x49")
        assert DrawRepository(session).count(game.id) == 1


_TWO_DRAWING_HTML = """
<div class="tir_result">
  <h2 class="tir_title"> Тираж 12 - 03.03.2016 </h2>
  <div class="tir_numbers">
    <div class="col-xs-12"><span class="win-numbers">1 Теглене</span></div>
    <div class="col-xs-12">
      <span class="ball-white">2</span>
      <span class="ball-white">13</span>
      <span class="ball-white">30</span>
      <span class="ball-white">31</span>
      <span class="ball-white">33</span>
    </div>
    <div class="col-xs-12"><span class="win-numbers">2 Теглене</span></div>
    <div class="col-xs-12">
      <span class="ball-white">2</span>
      <span class="ball-white">9</span>
      <span class="ball-white">24</span>
      <span class="ball-white">33</span>
      <span class="ball-white">34</span>
    </div>
  </div>
</div>
"""


def test_two_drawing_page_stores_both_drawings(database: Database) -> None:
    url = "https://info.toto.bg/results/5x35/2016-12"
    fetcher = FakeFetcher({url: _TWO_DRAWING_HTML})
    service = make_service(database, fetcher)

    stats = ImportStats()
    service._import_draw_url("5x35", url, source="wayback", stats=stats)

    with database.session() as session:
        game = GameRepository(session).by_code("5x35")
        draws = DrawRepository(session)
        assert draws.count(game.id) == 2
        first = draws.get(game.id, 2016, 12, drawing=1)
        second = draws.get(game.id, 2016, 12, drawing=2)
        assert first is not None and second is not None
        assert [n.value for n in first.numbers] == [2, 13, 30, 31, 33]
        assert [n.value for n in second.numbers] == [2, 9, 24, 33, 34]
    assert stats.draws_imported == 2

    # Re-running against the same URL must not duplicate either drawing.
    stats2 = ImportStats()
    service._import_draw_url("5x35", url, source="wayback", stats=stats2)
    with database.session() as session:
        game = GameRepository(session).by_code("5x35")
        assert DrawRepository(session).count(game.id) == 2


def test_failed_segments_retried_on_next_run(database: Database, live_list_html: str) -> None:
    incomplete = {"https://info.toto.bg/results/6x49": live_list_html}
    fetcher = FakeFetcher(incomplete)
    service = make_service(database, fetcher)
    stats = service.import_live(["6x49"])
    # 25 failures: draw 55 came from the list page itself, so its detail URL
    # is duplicate-skipped before any fetch; the other 25 URLs 404.
    assert stats.failures == 25

    # Now the pages become available: failed checkpoints must be retried.
    for number in range(30, 56):
        fetcher.pages[f"https://info.toto.bg/results/6x49/2026-{number}"] = live_list_html
    stats = make_service(database, fetcher).import_live(["6x49"])
    assert stats.failures == 0
    assert stats.pages_fetched > 1
