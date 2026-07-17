"""Engine and session management.

The :class:`Database` object is created once at application start and passed
to every component that needs persistence (dependency injection, no globals).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.database.models import Base
from app.services.logging_service import get_logger


class Database:
    """Owns the SQLAlchemy engine and session factory for one SQLite file."""

    def __init__(self, database_path: Path, echo: bool = False) -> None:
        self._path = database_path
        self._log = get_logger("database")
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._engine: Engine = create_engine(
            f"sqlite:///{database_path}", echo=echo, future=True
        )
        # SQLite needs both pragmas per-connection.
        event.listen(self._engine, "connect", self._configure_connection)
        self._session_factory = sessionmaker(
            bind=self._engine, expire_on_commit=False, future=True
        )

    @staticmethod
    def _configure_connection(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def engine(self) -> Engine:
        return self._engine

    def create_schema(self) -> None:
        """Create all tables that do not exist yet."""
        Base.metadata.create_all(self._engine)
        self._log.info("Schema ensured at %s", self._path)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Transactional session scope: commits on success, rolls back on error."""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self._engine.dispose()
