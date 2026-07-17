"""Dark theme stylesheet (kept intentionally small in this milestone)."""

DARK_QSS = """
QMainWindow, QWidget { background-color: #1e1f24; color: #d8dae2; font-size: 14px; }
QListWidget {
    background-color: #17181c; border: none; padding-top: 8px; outline: 0;
}
QListWidget::item { padding: 10px 16px; border-radius: 6px; margin: 2px 8px; }
QListWidget::item:selected { background-color: #2e6fd0; color: #ffffff; }
QListWidget::item:disabled { color: #5a5d68; }
QLabel#pageTitle { font-size: 22px; font-weight: 600; color: #ffffff; }
QLabel#pageHint { color: #8a8d99; }
QTableWidget { background-color: #17181c; gridline-color: #2a2c33; }
QHeaderView::section { background-color: #22242b; color: #d8dae2; border: none; padding: 6px; }
"""
