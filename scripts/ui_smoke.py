"""UI smoke check: launch the shell, render for two seconds, save a screenshot."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ui.main_window import MainWindow
from app.ui.theme import DARK_QSS
from main import bootstrap


def run(screenshot_path: Path) -> int:
    config, database = bootstrap()
    app = QApplication(sys.argv[:1])
    app.setStyleSheet(DARK_QSS)
    window = MainWindow(database)
    window.show()

    def capture_and_quit() -> None:
        window.grab().save(str(screenshot_path))
        app.quit()

    QTimer.singleShot(2000, capture_and_quit)
    code = app.exec()
    database.dispose()
    print(f"UI OK, screenshot at {screenshot_path}")
    return code


if __name__ == "__main__":
    raise SystemExit(run(Path(sys.argv[1] if len(sys.argv) > 1 else "ui_smoke.png")))
