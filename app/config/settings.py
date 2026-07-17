"""Application configuration.

Configuration is stored as a JSON file so users can edit it by hand or
through the Settings page. Every value has a safe default; unknown keys in
the file are ignored so newer config files stay loadable by older builds.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
VALID_THEMES = ("dark", "light")


class ConfigError(Exception):
    """Raised when configuration cannot be loaded or contains invalid values."""


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


@dataclass(slots=True)
class AppConfig:
    """All user-configurable settings with their defaults."""

    database_path: str = str(_project_root() / "data" / "toto.db")
    log_dir: str = str(_project_root() / "logs")
    log_level: str = "INFO"
    theme: str = "dark"

    # Scraper behaviour
    request_timeout_seconds: float = 30.0
    retry_count: int = 4
    retry_backoff_seconds: float = 2.0
    rate_limit_seconds: float = 1.5
    update_frequency_hours: int = 24
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
    toto_base_url: str = "https://toto.bg"
    # DevTools endpoint of a locally running Chrome used as fetch fallback
    # when toto.bg's bot protection blocks plain HTTP clients.
    chrome_debug_url: str = "http://localhost:9222"

    def validate(self) -> None:
        """Raise :class:`ConfigError` for out-of-range values."""
        if self.log_level not in VALID_LOG_LEVELS:
            raise ConfigError(f"log_level must be one of {VALID_LOG_LEVELS}, got {self.log_level!r}")
        if self.theme not in VALID_THEMES:
            raise ConfigError(f"theme must be one of {VALID_THEMES}, got {self.theme!r}")
        if self.request_timeout_seconds <= 0:
            raise ConfigError("request_timeout_seconds must be positive")
        if self.retry_count < 0:
            raise ConfigError("retry_count must be >= 0")
        if self.rate_limit_seconds < 0:
            raise ConfigError("rate_limit_seconds must be >= 0")
        if self.update_frequency_hours < 1:
            raise ConfigError("update_frequency_hours must be >= 1")


class ConfigManager:
    """Loads, validates and persists :class:`AppConfig` as JSON."""

    def __init__(self, config_path: Path) -> None:
        self._config_path = config_path

    @property
    def config_path(self) -> Path:
        return self._config_path

    def load(self) -> AppConfig:
        """Return config from disk, falling back to defaults if the file is absent."""
        if not self._config_path.exists():
            config = AppConfig()
            config.validate()
            return config

        try:
            raw: dict[str, Any] = json.loads(self._config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"Cannot read config file {self._config_path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"Config file {self._config_path} must contain a JSON object")

        known = {f.name: f for f in fields(AppConfig)}
        kwargs = {name: value for name, value in raw.items() if name in known}
        config = AppConfig(**kwargs)
        config.validate()
        return config

    def save(self, config: AppConfig) -> None:
        """Validate and write the config to disk (pretty-printed JSON)."""
        config.validate()
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(
            json.dumps(asdict(config), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def default_config_path() -> Path:
    """Location of the user config file inside the project tree."""
    return _project_root() / "config" / "user_config.json"
