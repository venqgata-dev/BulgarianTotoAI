"""Historical Draw Browser widget.

Lets the user browse imported draws by game/year/draw number, step to the
previous/next drawing, and search by date or draw number. Reused both as
the standalone "Historical Draws" page and as a tab inside the Statistics
page.
"""

from __future__ import annotations

from datetime import date as date_cls

from PySide6.QtCore import QDate, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.database.engine import Database
from app.models.domain import SUPPORTED_GAMES
from app.services.browser import DrawDetail, HistoricalBrowserService


class HistoricalBrowserWidget(QWidget):
    """Self-contained browse/search UI for one game's imported history."""

    def __init__(self, database: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service = HistoricalBrowserService(database)
        self._current: DrawDetail | None = None
        self._build_ui()
        self._on_game_changed()

    # -- layout --------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        browse_row = QHBoxLayout()
        self._game_box = QComboBox()
        for game in SUPPORTED_GAMES:
            self._game_box.addItem(game.name, game.code)
        self._game_box.currentIndexChanged.connect(self._on_game_changed)

        self._year_box = QComboBox()
        self._year_box.currentIndexChanged.connect(self._on_year_changed)
        self._number_box = QComboBox()

        go_button = QPushButton("Go")
        go_button.clicked.connect(self._on_go)
        prev_button = QPushButton("< Prev")
        prev_button.clicked.connect(lambda: self._navigate("prev"))
        next_button = QPushButton("Next >")
        next_button.clicked.connect(lambda: self._navigate("next"))

        for label_text, widget in (
            ("Game:", self._game_box),
            ("Year:", self._year_box),
            ("Draw #:", self._number_box),
        ):
            browse_row.addWidget(QLabel(label_text))
            browse_row.addWidget(widget)
        browse_row.addWidget(go_button)
        browse_row.addStretch(1)
        browse_row.addWidget(prev_button)
        browse_row.addWidget(next_button)
        layout.addLayout(browse_row)

        search_row = QHBoxLayout()
        self._date_edit = QDateEdit()
        self._date_edit.setCalendarPopup(True)
        self._date_edit.setDate(QDate.currentDate())
        search_date_button = QPushButton("Search by date")
        search_date_button.clicked.connect(self._on_search_by_date)

        self._find_number_box = QSpinBox()
        self._find_number_box.setRange(1, 999)
        search_number_button = QPushButton("Search by draw number")
        search_number_button.clicked.connect(self._on_search_by_number)

        search_row.addWidget(QLabel("Date:"))
        search_row.addWidget(self._date_edit)
        search_row.addWidget(search_date_button)
        search_row.addSpacing(24)
        search_row.addWidget(QLabel("Draw #:"))
        search_row.addWidget(self._find_number_box)
        search_row.addWidget(search_number_button)
        search_row.addStretch(1)
        layout.addLayout(search_row)

        self._results_list = QListWidget()
        self._results_list.setMaximumHeight(90)
        self._results_list.itemClicked.connect(self._on_result_selected)
        self._results_list.hide()
        layout.addWidget(self._results_list)

        self._detail_frame = QFrame()
        detail_layout = QVBoxLayout(self._detail_frame)
        detail_layout.setContentsMargins(0, 8, 0, 0)
        self._ref_label = QLabel("-")
        self._ref_label.setObjectName("pageTitle")
        self._meta_label = QLabel("-")
        self._meta_label.setObjectName("pageHint")
        self._numbers_label = QLabel("-")
        self._numbers_label.setWordWrap(True)
        self._numbers_label.setStyleSheet("font-size: 20px; font-weight: 600;")
        self._jackpot_label = QLabel("-")
        self._provenance_label = QLabel("-")
        self._provenance_label.setObjectName("pageHint")
        self._provenance_label.setWordWrap(True)

        self._tiers_table = QTableWidget(0, 5)
        self._tiers_table.setHorizontalHeaderLabels(["Tier", "Winners", "Prize", "Total", "Currency"])
        self._tiers_table.verticalHeader().setVisible(False)
        self._tiers_table.setMaximumHeight(160)

        detail_layout.addWidget(self._ref_label)
        detail_layout.addWidget(self._meta_label)
        detail_layout.addWidget(self._numbers_label)
        detail_layout.addWidget(self._jackpot_label)
        detail_layout.addWidget(self._tiers_table)
        detail_layout.addWidget(self._provenance_label)
        layout.addWidget(self._detail_frame)

        self._status_label = QLabel("")
        self._status_label.setObjectName("pageHint")
        layout.addWidget(self._status_label)
        layout.addStretch(1)

    # -- data population ------------------------------------------------------

    def _on_game_changed(self) -> None:
        game_code = self._game_box.currentData()
        years = self._service.list_years(game_code)
        self._year_box.blockSignals(True)
        self._year_box.clear()
        for year in years:
            self._year_box.addItem(str(year), year)
        self._year_box.blockSignals(False)
        if years:
            self._on_year_changed()
        else:
            self._number_box.clear()
        self._show_latest()

    def _on_year_changed(self) -> None:
        game_code = self._game_box.currentData()
        year = self._year_box.currentData()
        self._number_box.clear()
        if year is None:
            return
        for number in self._service.list_draw_numbers(game_code, year):
            self._number_box.addItem(str(number), number)

    def _show_latest(self) -> None:
        game_code = self._game_box.currentData()
        detail = self._service.latest(game_code)
        if detail:
            self._select_year_number(detail.draw_year, detail.draw_number)
        self._display(detail)

    def _select_year_number(self, year: int, number: int) -> None:
        year_index = self._year_box.findData(year)
        if year_index >= 0:
            self._year_box.blockSignals(True)
            self._year_box.setCurrentIndex(year_index)
            self._year_box.blockSignals(False)
            self._on_year_changed()
        number_index = self._number_box.findData(number)
        if number_index >= 0:
            self._number_box.setCurrentIndex(number_index)

    # -- actions ---------------------------------------------------------------

    def _on_go(self) -> None:
        game_code = self._game_box.currentData()
        year = self._year_box.currentData()
        number = self._number_box.currentData()
        if year is None or number is None:
            return
        detail = self._service.get(game_code, year, number, 1)
        self._results_list.hide()
        self._display(detail)

    def _navigate(self, direction: str) -> None:
        if self._current is None:
            return
        detail = self._service.navigate(
            self._current.game_code,
            self._current.draw_year,
            self._current.draw_number,
            self._current.drawing,
            direction,
        )
        if detail is None:
            self._status_label.setText(f"No {'earlier' if direction == 'prev' else 'later'} draw.")
            return
        self._results_list.hide()
        self._select_year_number(detail.draw_year, detail.draw_number)
        self._display(detail)

    def _on_search_by_date(self) -> None:
        game_code = self._game_box.currentData()
        qdate = self._date_edit.date()
        target = date_cls(qdate.year(), qdate.month(), qdate.day())
        matches = self._service.search_by_date(game_code, target)
        self._show_matches(matches, f"No draws found on {target.isoformat()}.")

    def _on_search_by_number(self) -> None:
        game_code = self._game_box.currentData()
        number = self._find_number_box.value()
        matches = self._service.search_by_draw_number(game_code, number)
        self._show_matches(matches, f"No draws found with draw number {number}.")

    def _show_matches(self, matches: list[DrawDetail], empty_message: str) -> None:
        self._results_list.clear()
        if not matches:
            self._status_label.setText(empty_message)
            self._results_list.hide()
            self._display(None)
            return
        self._status_label.setText(f"{len(matches)} match(es) found.")
        for detail in matches:
            item = QListWidgetItem(f"{detail.ref} - {detail.draw_date}")
            item.setData(Qt.ItemDataRole.UserRole, detail)
            self._results_list.addItem(item)
        self._results_list.setVisible(len(matches) > 1)
        self._select_year_number(matches[0].draw_year, matches[0].draw_number)
        self._display(matches[0])

    def _on_result_selected(self, item: QListWidgetItem) -> None:
        detail = item.data(Qt.ItemDataRole.UserRole)
        self._select_year_number(detail.draw_year, detail.draw_number)
        self._display(detail)

    # -- rendering ---------------------------------------------------------------

    def _display(self, detail: DrawDetail | None) -> None:
        self._current = detail
        if detail is None:
            self._ref_label.setText("No draws imported for this game yet.")
            self._meta_label.setText("")
            self._numbers_label.setText("")
            self._jackpot_label.setText("")
            self._provenance_label.setText("")
            self._tiers_table.setRowCount(0)
            return

        self._ref_label.setText(f"{detail.game_name} - {detail.ref}")
        drawing_note = "  (historical second drawing)" if detail.is_second_drawing else ""
        self._meta_label.setText(f"{detail.draw_date}   |   Drawing {detail.drawing}{drawing_note}")

        numbers_text = "   ".join(str(n) for n in detail.numbers)
        if detail.bonus_numbers:
            numbers_text += "    Bonus: " + "   ".join(str(n) for n in detail.bonus_numbers)
        self._numbers_label.setText(numbers_text)

        if detail.jackpot_amount is not None:
            self._jackpot_label.setText(f"Jackpot: {detail.jackpot_amount:,.2f} {detail.currency or ''}")
        else:
            self._jackpot_label.setText("Jackpot: n/a")

        self._tiers_table.setRowCount(len(detail.prize_tiers))
        for row, tier in enumerate(detail.prize_tiers):
            values = [
                tier.label,
                str(tier.winners) if tier.winners is not None else "-",
                f"{tier.prize_amount:,.2f}" if tier.prize_amount is not None else "-",
                f"{tier.total_amount:,.2f}" if tier.total_amount is not None else "-",
                tier.currency or "-",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._tiers_table.setItem(row, col, item)

        provenance = f"Source: {detail.source}"
        if detail.source_url:
            provenance += f"  ({detail.source_url})"
        provenance += f"   |   Validation: {detail.validation_status}"
        self._provenance_label.setText(provenance)
        self._status_label.setText("")
