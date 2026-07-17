"""Central logging configuration.

Four log files are produced under the configured log directory:

    app.log       - everything logged under ``totoai.app``
    scraper.log   - everything logged under ``totoai.scraper``
    database.log  - everything logged under ``totoai.database``
    errors.log    - ERROR+ records from the whole ``totoai`` tree

Loggers are hierarchical (``totoai.scraper`` propagates to ``totoai``), so
the error file and console handler live on the root ``totoai`` logger.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

ROOT_LOGGER = "totoai"

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_MAX_BYTES = 2 * 1024 * 1024
_BACKUPS = 5


def _file_handler(path: Path, level: int) -> logging.Handler:
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_FORMAT))
    return handler


def setup_logging(log_dir: Path, level: str = "INFO", console: bool = True) -> None:
    """Configure the ``totoai`` logger tree. Safe to call more than once."""
    log_dir.mkdir(parents=True, exist_ok=True)
    numeric_level = logging.getLevelName(level.upper())
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO

    root = logging.getLogger(ROOT_LOGGER)
    root.setLevel(numeric_level)
    # Reset handlers so repeated setup (tests, restarts) does not duplicate output.
    for logger_name in (ROOT_LOGGER, f"{ROOT_LOGGER}.app", f"{ROOT_LOGGER}.scraper", f"{ROOT_LOGGER}.database"):
        logging.getLogger(logger_name).handlers.clear()

    root.addHandler(_file_handler(log_dir / "errors.log", logging.ERROR))
    if console:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(numeric_level)
        console_handler.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(console_handler)

    logging.getLogger(f"{ROOT_LOGGER}.app").addHandler(_file_handler(log_dir / "app.log", numeric_level))
    logging.getLogger(f"{ROOT_LOGGER}.scraper").addHandler(_file_handler(log_dir / "scraper.log", numeric_level))
    logging.getLogger(f"{ROOT_LOGGER}.database").addHandler(_file_handler(log_dir / "database.log", numeric_level))


def get_logger(component: str) -> logging.Logger:
    """Return a logger for ``component`` (``app``, ``scraper``, ``database`` or a dotted child)."""
    return logging.getLogger(f"{ROOT_LOGGER}.{component}")
