"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.database.engine import Database
from app.database.seed import seed_games
from app.services.logging_service import setup_logging

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session", autouse=True)
def _logging(tmp_path_factory: pytest.TempPathFactory) -> None:
    setup_logging(tmp_path_factory.mktemp("logs"), "WARNING", console=False)


@pytest.fixture()
def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "test.db")
    db.create_schema()
    with db.session() as session:
        seed_games(session)
    yield db
    db.dispose()


@pytest.fixture()
def live_list_html() -> str:
    return (FIXTURES / "live_6x49_list_2026.html").read_text(encoding="utf-8")


@pytest.fixture()
def wayback_draw_html() -> str:
    return (FIXTURES / "wayback_6x49_2024-11.html").read_text(encoding="utf-8")
