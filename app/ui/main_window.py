"""Minimal application shell: navigation plus placeholder-free status pages.

Per the current milestone, the UI is intentionally thin. The only live data
shown is the imported draw count per game on the Dashboard (a single query,
no business logic).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app import APP_NAME, __version__
from app.database.engine import Database
from app.database.repository import DrawRepository, GameRepository


def _page(title: str, hint: str) -> QWidget:
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(28, 24, 28, 24)
    title_label = QLabel(title)
    title_label.setObjectName("pageTitle")
    hint_label = QLabel(hint)
    hint_label.setObjectName("pageHint")
    hint_label.setWordWrap(True)
    layout.addWidget(title_label)
    layout.addWidget(hint_label)
    layout.addStretch(1)
    return widget


class MainWindow(QMainWindow):
    """Navigation shell with one page per planned application area."""

    _NAV = (
        ("Dashboard", True),
        ("Historical Draws", True),
        ("Statistics", True),
        ("Prediction Lab", False),  # future milestone
        ("Backtesting", False),  # future milestone
        ("Settings", True),
        ("About", True),
    )

    def __init__(self, database: Database) -> None:
        super().__init__()
        self._database = database
        self.setWindowTitle(f"{APP_NAME} v{__version__}")
        self.resize(1100, 720)

        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._nav = QListWidget()
        self._nav.setFixedWidth(220)
        self._pages = QStackedWidget()

        for name, enabled in self._NAV:
            item = QListWidgetItem(name)
            if not enabled:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                item.setText(f"{name}  (soon)")
            self._nav.addItem(item)
            self._pages.addWidget(self._build_page(name, enabled))

        self._nav.currentRowChanged.connect(self._pages.setCurrentIndex)
        self._nav.setCurrentRow(0)

        layout.addWidget(self._nav)
        layout.addWidget(self._pages, stretch=1)
        self.setCentralWidget(container)

    def _build_page(self, name: str, enabled: bool) -> QWidget:
        if name == "Dashboard":
            return self._build_dashboard()
        if name == "About":
            return _page(
                APP_NAME,
                f"Version {__version__}. Historical analysis of Bulgarian Toto draws "
                "(6/49, 6/42, 5/35). Data source: official results at info.toto.bg "
                "plus Internet Archive snapshots of the same pages.",
            )
        if not enabled:
            return _page(name, "This area is planned for a future milestone.")
        return _page(name, "Content arrives in the next milestone; the pipeline behind it is ready.")

    def _build_dashboard(self) -> QWidget:
        lines: list[str] = []
        try:
            with self._database.session() as session:
                draws = DrawRepository(session)
                for game in GameRepository(session).all_games():
                    lines.append(f"{game.name}: {draws.count(game.id)} draws imported")
        except Exception as exc:  # pragma: no cover - defensive UI guard
            lines.append(f"Database unavailable: {exc}")
        return _page("Dashboard", "\n".join(lines) or "No games seeded yet.")
