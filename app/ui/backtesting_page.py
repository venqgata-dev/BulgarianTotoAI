"""Backtesting page: run prediction strategies against historical draws.

A game/strategy/scope picker drives a single :class:`BacktestReport` (one
strategy) or a set of them ("All" = every registered strategy, compared
side by side). Charts use pyqtgraph, themed to match the app's existing
dark palette (see app/ui/statistics_page.py, which established the pattern
this page follows).
"""

from __future__ import annotations

from datetime import date as date_cls
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.analysis.backtest import BacktestReport, BacktestService, StrategyComparisonRow
from app.analysis.strategies import STRATEGY_REGISTRY
from app.database.engine import Database
from app.models.domain import SUPPORTED_GAMES

pg.setConfigOption("background", "#1e1f24")
pg.setConfigOption("foreground", "#d8dae2")

_ACCENT = "#2e6fd0"
_ACCENT_WARM = "#d0762e"
_ACCENT_GREEN = "#3ea56b"


def _stat_card(title: str, value: str) -> QWidget:
    card = QWidget()
    layout = QVBoxLayout(card)
    layout.setContentsMargins(12, 10, 12, 10)
    title_label = QLabel(title)
    title_label.setObjectName("pageHint")
    value_label = QLabel(value)
    value_label.setStyleSheet("font-size: 20px; font-weight: 600; color: #ffffff;")
    layout.addWidget(title_label)
    layout.addWidget(value_label)
    return card


def _bar_chart(labels: list[str], values: list[float], color: str = _ACCENT) -> pg.PlotWidget:
    plot = pg.PlotWidget()
    plot.setBackground("#1e1f24")
    plot.showGrid(x=False, y=True, alpha=0.15)
    xs = list(range(len(values)))
    plot.addItem(pg.BarGraphItem(x=xs, height=values, width=0.7, brush=color))
    axis = plot.getAxis("bottom")
    axis.setTicks([list(zip(xs, labels))])
    plot.setMouseEnabled(x=False, y=False)
    return plot


def _line_chart(values: list[float], color: str = _ACCENT) -> pg.PlotWidget:
    plot = pg.PlotWidget()
    plot.setBackground("#1e1f24")
    plot.showGrid(x=False, y=True, alpha=0.15)
    plot.plot(list(range(len(values))), values, pen=pg.mkPen(color, width=2))
    plot.setMouseEnabled(x=False, y=False)
    return plot


class BacktestingPage(QWidget):
    """The Backtesting nav page: strategy/scope controls plus report tabs."""

    def __init__(self, database: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service = BacktestService(database)
        self._current_report: BacktestReport | None = None
        self._comparison_rows: list[StrategyComparisonRow] = []
        self._build_ui()

    # -- layout --------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        title = QLabel("Backtesting")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        hint = QLabel(
            "Replays every historical draw in scope: each prediction only ever sees draws "
            "strictly before it. Deterministic - the Random strategy uses a seeded RNG."
        )
        hint.setObjectName("pageHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        controls = QGridLayout()
        self._game_box = QComboBox()
        for game in SUPPORTED_GAMES:
            self._game_box.addItem(game.name, game.code)

        self._strategy_box = QComboBox()
        self._strategy_box.addItem("All (compare)", "all")
        for name in sorted(STRATEGY_REGISTRY):
            self._strategy_box.addItem(f"{name} - {STRATEGY_REGISTRY[name].description}", name)
        self._strategy_box.currentIndexChanged.connect(self._on_strategy_changed)

        self._years_edit = QLineEdit()
        self._years_edit.setPlaceholderText("e.g. 2024,2025 (blank = all)")

        self._last_n_box = QSpinBox()
        self._last_n_box.setRange(0, 100000)
        self._last_n_box.setSpecialValueText("All")

        self._date_range_check = QCheckBox("Filter by date range")
        self._date_range_check.toggled.connect(self._on_date_range_toggled)
        self._from_date = QDateEdit()
        self._from_date.setCalendarPopup(True)
        self._from_date.setDate(QDate.currentDate().addYears(-1))
        self._to_date = QDateEdit()
        self._to_date.setCalendarPopup(True)
        self._to_date.setDate(QDate.currentDate())
        self._from_date.setEnabled(False)
        self._to_date.setEnabled(False)

        self._seed_box = QSpinBox()
        self._seed_box.setRange(0, 999999)
        self._seed_box.setSpecialValueText("Random")

        run_button = QPushButton("Run")
        run_button.clicked.connect(self._on_run)
        export_csv_button = QPushButton("Export CSV")
        export_csv_button.clicked.connect(self._export_csv)
        export_json_button = QPushButton("Export JSON")
        export_json_button.clicked.connect(self._export_json)

        controls.addWidget(QLabel("Game:"), 0, 0)
        controls.addWidget(self._game_box, 0, 1)
        controls.addWidget(QLabel("Strategy:"), 0, 2)
        controls.addWidget(self._strategy_box, 0, 3)
        controls.addWidget(QLabel("Years:"), 0, 4)
        controls.addWidget(self._years_edit, 0, 5)
        controls.addWidget(QLabel("Last N:"), 0, 6)
        controls.addWidget(self._last_n_box, 0, 7)

        controls.addWidget(self._date_range_check, 1, 0, 1, 2)
        controls.addWidget(self._from_date, 1, 2)
        controls.addWidget(self._to_date, 1, 3)
        controls.addWidget(QLabel("Random seed:"), 1, 4)
        controls.addWidget(self._seed_box, 1, 5)
        controls.addWidget(run_button, 1, 6)
        controls.addWidget(export_csv_button, 1, 7)
        controls.addWidget(export_json_button, 1, 8)
        layout.addLayout(controls)

        self._status_label = QLabel("Choose a game and strategy, then click Run.")
        self._status_label.setObjectName("pageHint")
        layout.addWidget(self._status_label)

        self._tabs = QTabWidget()
        layout.addWidget(self._tabs, stretch=1)
        self._on_strategy_changed()

    def _on_date_range_toggled(self, checked: bool) -> None:
        self._from_date.setEnabled(checked)
        self._to_date.setEnabled(checked)

    def _on_strategy_changed(self) -> None:
        is_random = self._strategy_box.currentData() == "random"
        self._seed_box.setEnabled(is_random or self._strategy_box.currentData() == "all")

    # -- run -------------------------------------------------------------------

    def _params_for(self, name: str) -> dict:
        if name == "random":
            seed = self._seed_box.value()
            return {"seed": seed or None}
        return {}

    def _selected_years(self) -> list[int] | None:
        text = self._years_edit.text().strip()
        if not text:
            return None
        years: list[int] = []
        for chunk in text.split(","):
            chunk = chunk.strip()
            if chunk:
                try:
                    years.append(int(chunk))
                except ValueError:
                    continue
        return years or None

    def _on_run(self) -> None:
        game_code = self._game_box.currentData()
        strategy_name = self._strategy_box.currentData()
        years = self._selected_years()
        last_n = self._last_n_box.value() or None
        date_from = date_to = None
        if self._date_range_check.isChecked():
            date_from = date_cls(self._from_date.date().year(), self._from_date.date().month(), self._from_date.date().day())
            date_to = date_cls(self._to_date.date().year(), self._to_date.date().month(), self._to_date.date().day())

        names = sorted(STRATEGY_REGISTRY) if strategy_name == "all" else [strategy_name]
        reports = [
            self._service.run(
                game_code,
                name,
                strategy_params=self._params_for(name),
                years=years,
                date_from=date_from,
                date_to=date_to,
                last_n=last_n,
            )
            for name in names
        ]
        self._comparison_rows = [self._row_from_report(r) for r in reports]
        self._current_report = max(
            reports, key=lambda r: r.metrics.average_hits if r.metrics else -1
        )
        self._status_label.setText(
            f"Scope: {self._current_report.scope}  |  "
            f"{'compared ' + str(len(reports)) + ' strategies' if len(reports) > 1 else self._current_report.strategy_name}"
        )
        self._rebuild_tabs()

    @staticmethod
    def _row_from_report(report: BacktestReport) -> StrategyComparisonRow:
        m = report.metrics
        return StrategyComparisonRow(
            strategy_name=report.strategy_name,
            predictions=m.predictions if m else 0,
            average_hits=m.average_hits if m else 0.0,
            max_hits=m.max_hits if m else 0,
            hit_percentage_3_plus=m.hit_percentage_3_plus if m else 0.0,
            hit_percentage_4_plus=m.hit_percentage_4_plus if m else 0.0,
            hit_percentage_5_plus=m.hit_percentage_5_plus if m else 0.0,
            hit_percentage_6=m.hit_percentage_6 if m else 0.0,
            longest_winning_streak=m.longest_winning_streak if m else 0,
            longest_losing_streak=m.longest_losing_streak if m else 0,
            execution_seconds=report.execution_seconds,
        )

    # -- tabs --------------------------------------------------------------

    def _rebuild_tabs(self) -> None:
        current_index = self._tabs.currentIndex()
        self._tabs.clear()
        if self._current_report is None:
            return
        self._tabs.addTab(self._build_summary_tab(), "Summary")
        self._tabs.addTab(self._build_comparison_tab(), "Strategy Comparison")
        self._tabs.addTab(self._build_performance_tab(), "Performance Over Time")
        self._tabs.addTab(self._build_history_tab(), "History Table")
        if 0 <= current_index < self._tabs.count():
            self._tabs.setCurrentIndex(current_index)

    def _build_summary_tab(self) -> QWidget:
        report = self._current_report
        widget = QWidget()
        layout = QVBoxLayout(widget)
        m = report.metrics

        grid = QGridLayout()
        cards = [
            ("Strategy", report.strategy_name),
            ("Predictions", str(m.predictions if m else 0)),
            ("Average hits", f"{m.average_hits:.3f}" if m else "n/a"),
            ("Average score", f"{m.average_score:.1f}%" if m else "n/a"),
            ("3+ hits", f"{m.hit_percentage_3_plus:.1f}%" if m else "n/a"),
            ("4+ hits", f"{m.hit_percentage_4_plus:.1f}%" if m else "n/a"),
            ("5+ hits", f"{m.hit_percentage_5_plus:.1f}%" if m else "n/a"),
            ("6 (perfect)", f"{m.hit_percentage_6:.1f}%" if m else "n/a"),
            ("Longest winning streak", str(m.longest_winning_streak) if m else "0"),
            ("Longest losing streak", str(m.longest_losing_streak) if m else "0"),
            ("Execution time", f"{report.execution_seconds:.3f}s"),
        ]
        for index, (label, value) in enumerate(cards):
            grid.addWidget(_stat_card(label, value), index // 4, index % 4)
        layout.addLayout(grid)

        if m and m.predictions:
            layout.addWidget(QLabel("Hit distribution"))
            counts = sorted(m.hit_distribution.items())
            layout.addWidget(_bar_chart([str(k) for k, _ in counts], [v for _, v in counts], _ACCENT_WARM))
        return widget

    def _build_comparison_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        rows = self._comparison_rows
        if rows:
            layout.addWidget(
                _bar_chart([r.strategy_name for r in rows], [r.average_hits for r in rows], _ACCENT_GREEN)
            )
        headers = ["Strategy", "Predictions", "Avg hits", "Max", "3+", "4+", "5+", "6", "Best streak", "Worst streak", "Time (s)"]
        table = QTableWidget(len(rows), len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for row_index, row in enumerate(rows):
            values = [
                row.strategy_name,
                str(row.predictions),
                f"{row.average_hits:.3f}",
                str(row.max_hits),
                f"{row.hit_percentage_3_plus:.1f}%",
                f"{row.hit_percentage_4_plus:.1f}%",
                f"{row.hit_percentage_5_plus:.1f}%",
                f"{row.hit_percentage_6:.1f}%",
                str(row.longest_winning_streak),
                str(row.longest_losing_streak),
                f"{row.execution_seconds:.3f}",
            ]
            for col, value in enumerate(values):
                table.setItem(row_index, col, QTableWidgetItem(value))
        table.resizeColumnsToContents()
        layout.addWidget(table)
        return widget

    def _build_performance_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        records = self._current_report.records
        if records:
            hits = [r.hit_count for r in records]
            running_avg = []
            total = 0.0
            for index, h in enumerate(hits, start=1):
                total += h
                running_avg.append(total / index)
            layout.addWidget(QLabel("Hits per prediction (performance over time)"))
            layout.addWidget(_line_chart(hits, _ACCENT))
            layout.addWidget(QLabel("Running average hits"))
            layout.addWidget(_line_chart(running_avg, _ACCENT_WARM))
        else:
            layout.addWidget(QLabel("No predictions in scope."))
        return widget

    def _build_history_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        records = self._current_report.records
        headers = ["Draw", "Date", "Predicted", "Actual", "Matched", "Missed", "Hits"]
        table = QTableWidget(len(records), len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for row_index, record in enumerate(reversed(records)):
            values = [
                record.draw_ref,
                record.draw_date.isoformat(),
                ", ".join(map(str, record.predicted_numbers)),
                ", ".join(map(str, record.actual_numbers)),
                ", ".join(map(str, record.matching_numbers)) or "-",
                ", ".join(map(str, record.missed_numbers)) or "-",
                str(record.hit_count),
            ]
            for col, value in enumerate(values):
                table.setItem(row_index, col, QTableWidgetItem(value))
        table.resizeColumnsToContents()
        layout.addWidget(table)
        return widget

    # -- export --------------------------------------------------------------

    def _export_csv(self) -> None:
        if not self._current_report:
            QMessageBox.warning(self, "Nothing to export", "Run a backtest first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export backtest CSV", "backtest.csv", "CSV files (*.csv)")
        if not path:
            return
        self._current_report.write_csv(Path(path))
        QMessageBox.information(self, "Export complete", f"History table written to {path}")

    def _export_json(self) -> None:
        if not self._current_report:
            QMessageBox.warning(self, "Nothing to export", "Run a backtest first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export backtest JSON", "backtest.json", "JSON files (*.json)")
        if not path:
            return
        self._current_report.write_json(Path(path))
        QMessageBox.information(self, "Export complete", f"Full backtest report written to {path}")
