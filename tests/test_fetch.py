"""Fetcher helpers: bot-challenge detection and rate limiting."""

from __future__ import annotations

import time
from pathlib import Path

from app.scraper.fetch import RateLimiter, looks_like_bot_challenge

FIXTURES = Path(__file__).parent / "fixtures"


def test_challenge_page_detected() -> None:
    challenge = (FIXTURES / "radware_challenge.html").read_text(encoding="utf-8")
    assert looks_like_bot_challenge(challenge)


def test_legitimate_page_not_flagged(live_list_html: str, wayback_draw_html: str) -> None:
    # Radware injects its script into real pages too; they must NOT be
    # classified as challenges.
    assert not looks_like_bot_challenge(live_list_html)
    assert not looks_like_bot_challenge(wayback_draw_html)


def test_rate_limiter_enforces_interval() -> None:
    limiter = RateLimiter(0.2)
    limiter.wait()
    start = time.monotonic()
    limiter.wait()
    assert time.monotonic() - start >= 0.19
