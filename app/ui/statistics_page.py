"""Statistics page: number/draw/combination/distribution analytics.

A game + scope (years / last-N-draws) picker drives a tabbed view -
Overview, Hot Numbers, Cold Numbers, Number Frequency Table, Pair
Statistics, Triplet Statistics, Distribution Charts, Recent Trends and the
Historical Browser (reused as its own tab). Charts use pyqtgraph, themed to
match the app's existing dark palette.
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app.analysis.statistics import ComboStat, NumberStat, StatisticsReport, StatisticsService
from app.database.engine import Database
from app.models.domain import SUPPORTED_GAMES, game_by_code
from app.ui.browser_widget import HistoricalBrowserWidget

pg.setConfigOption("background", "#1e1f24")
pg.setConfigOption("foreground", "#d8dae2")

_ACCENT = "#2e6fd0"
_ACCENT_WARM = "#d0762e"


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


def _number_table(numbers: list[NumberStat]) -> QTableWidget:
    headers = [
        "Number",
        "Frequency",
        "%",
        "First seen",
        "Last seen",
        "Current streak",
        "Longest streak",
        "Avg gap",
    ]
    table = QTableWidget(len(numbers), len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    for row, stat in enumerate(numbers):
        values = [
            str(stat.value),
            str(stat.frequency),
            f"{stat.percentage:.1f}",
            f"{stat.first_seen_ref} ({stat.first_seen_date})" if stat.first_seen_date else "never",
            f"{stat.last_seen_ref} ({stat.last_seen_date})" if stat.last_seen_date else "never",
            str(stat.current_streak),
            str(stat.longest_streak),
            f"{stat.average_gap:.2f}" if stat.average_gap is not None else "n/a",
        ]
        for col, value in enumerate(values):
            table.setItem(row, col, QTableWidgetItem(value))
    table.resizeColumnsToContents()
    return table


def _combo_table(combos: list[ComboStat]) -> QTableWidget:
    table = QTableWidget(len(combos), 2)
    table.setHorizontalHeaderLabels(["Numbers", "Frequency"])
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    for row, combo in enumerate(combos):
        table.setItem(row, 0, QTableWidgetItem(", ".join(map(str, combo.numbers))))
        table.setItem(row, 1, QTableWidgetItem(str(combo.frequency)))
    table.resizeColumnsToContents()
    return table


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


def _heatmap(heatmap: dict[int, int], main_max: int) -> pg.PlotWidget:
    columns = 7
    rows = (main_max + columns - 1) // columns
    grid = np.zeros((rows, columns))
    for value, frequency in heatmap.items():
        row, col = divmod(value - 1, columns)
        grid[row, col] = frequency

    cell_size = 60
    plot = pg.PlotWidget()
    plot.setBackground("#1e1f24")
    plot.setFixedSize(columns * cell_size, rows * cell_size)
    image = pg.ImageItem(grid.T)
    image.setColorMap(pg.colormap.get("viridis"))
    plot.addItem(image)
    plot.setRange(xRange=(0, columns), yRange=(0, rows), padding=0)
    plot.invertY(True)
    plot.getAxis("bottom").hide()
    plot.getAxis("left").hide()
    plot.setMouseEnabled(x=False, y=False)
    plot.setAspectLocked(True)

    font = pg.Qt.QtGui.QFont()
    font.setPointSize(10)
    for value in heatmap:
        row, col = divmod(value - 1, columns)
        text = pg.TextItem(str(value), anchor=(0.5, 0.5), color="#ffffff")
        text.setFont(font)
        text.setPos(col + 0.5, row + 0.5)
        plot.addItem(text)
    return plot


def _line_chart(values: list[float], color: str = _ACCENT) -> pg.PlotWidget:
    plot = pg.PlotWidget()
    plot.setBackground("#1e1f24")
    plot.showGrid(x=False, y=True, alpha=0.15)
    plot.plot(list(range(len(values))), values, pen=pg.mkPen(color, width=2), symbol="o", symbolSize=5)
    plot.setMouseEnabled(x=False, y=False)
    return plot


class StatisticsPage(QWidget):
    """The Statistics nav page: game/scope controls plus a tabbed report view."""

    def __init__(self, database: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._database = database
        self._service = StatisticsService(database)
        self._report: StatisticsReport | None = None
        self._build_ui()
        self._refresh()

    # -- layout --------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        title = QLabel("Statistics")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        controls = QHBoxLayout()
        self._game_box = QComboBox()
        for game in SUPPORTED_GAMES:
            self._game_box.addItem(game.name, game.code)

        self._years_edit = QLineEdit()
        self._years_edit.setPlaceholderText("e.g. 2024,2025 (blank = all years)")
        self._years_edit.setFixedWidth(220)

        self._last_n_box = QSpinBox()
        self._last_n_box.setRange(0, 100000)
        self._last_n_box.setSpecialValueText("All")

        apply_button = QPushButton("Apply")
        apply_button.clicked.connect(self._refresh)

        export_csv_button = QPushButton("Export CSV")
        export_csv_button.clicked.connect(self._export_csv)
        export_json_button = QPushButton("Export JSON")
        export_json_button.clicked.connect(self._export_json)

        controls.addWidget(QLabel("Game:"))
        controls.addWidget(self._game_box)
        controls.addWidget(QLabel("Years:"))
        controls.addWidget(self._years_edit)
        controls.addWidget(QLabel("Last N draws:"))
        controls.addWidget(self._last_n_box)
        controls.addWidget(apply_button)
        controls.addStretch(1)
        controls.addWidget(export_csv_button)
        controls.addWidget(export_json_button)
        layout.addLayout(controls)

        self._scope_label = QLabel("")
        self._scope_label.setObjectName("pageHint")
        layout.addWidget(self._scope_label)

        self._tabs = QTabWidget()
        layout.addWidget(self._tabs, stretch=1)

    # -- data --------------------------------------------------------------

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

    def _refresh(self) -> None:
        game_code = self._game_box.currentData()
        years = self._selected_years()
        last_n = self._last_n_box.value() or None
        self._report = self._service.analyze(game_code, years=years, last_n=last_n)
        self._scope_label.setText(f"Scope: {self._report.scope}  |  {self._report.draw_count} draws analyzed")
        self._rebuild_tabs()

    def _rebuild_tabs(self) -> None:
        current_index = self._tabs.currentIndex()
        self._tabs.clear()
        report = self._report
        if report is None:
            return
        self._tabs.addTab(self._build_overview_tab(report), "Overview")
        self._tabs.addTab(self._build_hot_tab(report), "Hot Numbers")
        self._tabs.addTab(self._build_cold_tab(report), "Cold Numbers")
        self._tabs.addTab(self._build_frequency_tab(report), "Number Frequency Table")
        self._tabs.addTab(self._build_pairs_tab(report), "Pair Statistics")
        self._tabs.addTab(self._build_triplets_tab(report), "Triplet Statistics")
        self._tabs.addTab(self._build_distribution_tab(report), "Distribution Charts")
        self._tabs.addTab(self._build_trends_tab(report), "Recent Trends")
        self._tabs.addTab(HistoricalBrowserWidget(self._database), "Historical Browser")
        if 0 <= current_index < self._tabs.count():
            self._tabs.setCurrentIndex(current_index)

    # -- tabs --------------------------------------------------------------

    @staticmethod
    def _build_overview_tab(report: StatisticsReport) -> QWidget:
        widget = QWidget()
        layout = QGridLayout(widget)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        hottest = report.hottest[0] if report.hottest else None
        coldest = report.coldest[0] if report.coldest else None
        total_sum = sum(d.total_sum for d in report.draws)
        avg_sum = total_sum / len(report.draws) if report.draws else 0.0
        top_pair = report.most_common_pairs[0] if report.most_common_pairs else None
        top_triplet = report.most_common_triplets[0] if report.most_common_triplets else None
        d = report.distribution

        cards = [
            ("Draws analyzed", str(report.draw_count)),
            ("Hottest number", f"{hottest.value} ({hottest.frequency}x)" if hottest else "n/a"),
            ("Coldest number", f"{coldest.value} ({coldest.frequency}x)" if coldest else "n/a"),
            ("Average sum per draw", f"{avg_sum:.1f}"),
            ("Most common pair", f"{top_pair.numbers} ({top_pair.frequency}x)" if top_pair else "n/a"),
            ("Most common triplet", f"{top_triplet.numbers} ({top_triplet.frequency}x)" if top_triplet else "n/a"),
            ("Even / Odd", f"{d.even_count} / {d.odd_count}" if d else "n/a"),
            ("Prime / Non-prime", f"{d.prime_count} / {d.non_prime_count}" if d else "n/a"),
        ]
        for index, (title, value) in enumerate(cards):
            layout.addWidget(_stat_card(title, value), index // 3, index % 3)
        return widget

    @staticmethod
    def _build_hot_tab(report: StatisticsReport) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        values = [n.frequency for n in report.hottest]
        labels = [str(n.value) for n in report.hottest]
        if values:
            layout.addWidget(_bar_chart(labels, values, _ACCENT_WARM))
        layout.addWidget(_number_table(report.hottest))
        return widget

    @staticmethod
    def _build_cold_tab(report: StatisticsReport) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        values = [n.frequency for n in report.coldest]
        labels = [str(n.value) for n in report.coldest]
        if values:
            layout.addWidget(_bar_chart(labels, values, _ACCENT))
        layout.addWidget(_number_table(report.coldest))
        return widget

    @staticmethod
    def _build_frequency_tab(report: StatisticsReport) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.addWidget(_number_table(report.numbers))
        return widget

    @staticmethod
    def _build_pairs_tab(report: StatisticsReport) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        most = QVBoxLayout()
        most.addWidget(QLabel("Most common pairs"))
        most.addWidget(_combo_table(report.most_common_pairs))
        least = QVBoxLayout()
        least.addWidget(QLabel("Least common pairs"))
        least.addWidget(_combo_table(report.least_common_pairs))
        layout.addLayout(most)
        layout.addLayout(least)
        return widget

    @staticmethod
    def _build_triplets_tab(report: StatisticsReport) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        most = QVBoxLayout()
        most.addWidget(QLabel("Most common triplets"))
        most.addWidget(_combo_table(report.most_common_triplets))
        least = QVBoxLayout()
        least.addWidget(QLabel("Least common triplets"))
        least.addWidget(_combo_table(report.least_common_triplets))
        layout.addLayout(most)
        layout.addLayout(least)
        return widget

    @staticmethod
    def _build_distribution_tab(report: StatisticsReport) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        layout = QVBoxLayout(inner)
        d = report.distribution
        if d is None:
            scroll.setWidget(inner)
            return scroll

        definition = game_by_code(report.game_code)

        layout.addWidget(QLabel("Number heatmap"))
        layout.addWidget(_heatmap(d.heatmap, definition.main_max))

        layout.addWidget(QLabel("Last digit frequency"))
        digits = sorted(d.last_digit.items())
        layout.addWidget(_bar_chart([str(k) for k, _ in digits], [v for _, v in digits]))

        layout.addWidget(QLabel("Decade distribution"))
        decades = sorted(d.decade.items(), key=lambda kv: int(kv[0].split("-")[0]))
        layout.addWidget(_bar_chart([k for k, _ in decades], [v for _, v in decades], _ACCENT_WARM))

        layout.addWidget(QLabel("Even / Odd and Prime / Non-prime"))
        layout.addWidget(
            _bar_chart(
                ["Even", "Odd", "Prime", "Non-prime"],
                [d.even_count, d.odd_count, d.prime_count, d.non_prime_count],
            )
        )
        scroll.setWidget(inner)
        return scroll

    @staticmethod
    def _build_trends_tab(report: StatisticsReport) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        recent = report.draws[-30:]
        if recent:
            layout.addWidget(QLabel(f"Sum per draw (last {len(recent)} draws)"))
            layout.addWidget(_line_chart([d.total_sum for d in recent]))
            layout.addWidget(QLabel(f"Odd count per draw (last {len(recent)} draws)"))
            layout.addWidget(_line_chart([d.odd_count for d in recent], _ACCENT_WARM))

        table = QTableWidget(len(recent), 7)
        table.setHorizontalHeaderLabels(
            ["Draw", "Date", "Sum", "Odd/Even", "Low/High", "Consecutive", "Repeated from previous"]
        )
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for row, stat in enumerate(reversed(recent)):
            values = [
                stat.ref,
                stat.draw_date.isoformat(),
                str(stat.total_sum),
                f"{stat.odd_count}/{stat.even_count}",
                f"{stat.low_count}/{stat.high_count}",
                str(stat.consecutive_count),
                str(stat.repeated_from_previous) if stat.repeated_from_previous is not None else "n/a",
            ]
            for col, value in enumerate(values):
                table.setItem(row, col, QTableWidgetItem(value))
        table.resizeColumnsToContents()
        layout.addWidget(table)
        return widget

    # -- export --------------------------------------------------------------

    def _export_csv(self) -> None:
        if not self._report:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export statistics CSV", "statistics.csv", "CSV files (*.csv)")
        if not path:
            return
        from pathlib import Path

        self._report.write_csv(Path(path))
        QMessageBox.information(self, "Export complete", f"Number frequency table written to {path}")

    def _export_json(self) -> None:
        if not self._report:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export statistics JSON", "statistics.json", "JSON files (*.json)"
        )
        if not path:
            return
        from pathlib import Path

        self._report.write_json(Path(path))
        QMessageBox.information(self, "Export complete", f"Full statistics report written to {path}")
