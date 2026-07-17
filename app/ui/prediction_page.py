"""Prediction Lab: the simplest possible page - pick a game, click one button.

No statistics, no charts, no advanced settings, no profiles, no
configuration. Deterministic by design (see app/analysis/predictor.py):
the same game always produces the same recommendation.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.analysis.predictor import PredictionResult, PredictorService
from app.database.engine import Database
from app.models.domain import SUPPORTED_GAMES


def _ball_row(numbers: tuple[int, ...]) -> QWidget:
    row = QWidget()
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    layout.addStretch(1)
    for value in numbers:
        ball = QLabel(str(value))
        ball.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ball.setFixedSize(44, 44)
        ball.setStyleSheet(
            "background-color: #2e6fd0; color: #ffffff; border-radius: 22px; "
            "font-size: 16px; font-weight: 600;"
        )
        layout.addWidget(ball)
    layout.addStretch(1)
    return row


class PredictionPage(QWidget):
    """Game selector + one big button. Nothing else."""

    def __init__(self, database: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service = PredictorService(database)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(18)

        title = QLabel("Prediction Lab")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Game:"))
        self._game_box = QComboBox()
        for game in SUPPORTED_GAMES:
            self._game_box.addItem(game.name, game.code)
        controls.addWidget(self._game_box)
        controls.addStretch(1)
        layout.addLayout(controls)

        self._generate_button = QPushButton("Generate Numbers")
        self._generate_button.setMinimumHeight(56)
        self._generate_button.setStyleSheet("font-size: 18px; font-weight: 600;")
        self._generate_button.clicked.connect(self._on_generate)
        layout.addWidget(self._generate_button)

        self._results_layout = QVBoxLayout()
        self._results_layout.setSpacing(16)
        layout.addLayout(self._results_layout)

        self._status_label = QLabel(
            "Uses the imported historical draws to rank a large pool of candidate "
            "combinations. Lottery draws are independent random events; this does "
            "not and cannot predict the actual result."
        )
        self._status_label.setObjectName("pageHint")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)
        layout.addStretch(1)

    def _clear_results(self) -> None:
        while self._results_layout.count():
            item = self._results_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

    def _on_generate(self) -> None:
        game_code = self._game_box.currentData()
        self._generate_button.setEnabled(False)
        self._status_label.setText("Generating...")
        QApplication.processEvents()  # repaint before the (brief, synchronous) scoring pass
        try:
            result = self._service.predict(game_code)
        finally:
            self._generate_button.setEnabled(True)
        self._render(result)

    def _render(self, result: PredictionResult) -> None:
        self._clear_results()

        recommended_label = QLabel("⭐ Recommended Combination")
        recommended_label.setStyleSheet("font-size: 16px; font-weight: 600; color: #ffffff;")
        self._results_layout.addWidget(recommended_label)
        self._results_layout.addWidget(_ball_row(result.recommended.numbers))

        alt_label = QLabel(f"{len(result.alternatives)} Alternative Combinations")
        alt_label.setObjectName("pageHint")
        self._results_layout.addWidget(alt_label)
        for alt in result.alternatives:
            self._results_layout.addWidget(_ball_row(alt.numbers))

        self._status_label.setText(
            f"Scored {result.pool_size:,} candidate combinations from {result.game_name}'s "
            "imported history. Lottery draws are independent random events; this does not "
            "and cannot predict the actual result."
        )
