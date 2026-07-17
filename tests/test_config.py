"""Configuration system tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config.settings import AppConfig, ConfigError, ConfigManager


def test_defaults_when_file_missing(tmp_path: Path) -> None:
    config = ConfigManager(tmp_path / "missing.json").load()
    assert config.log_level == "INFO"
    assert config.theme == "dark"
    assert config.retry_count >= 1


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    manager = ConfigManager(tmp_path / "config.json")
    config = AppConfig(log_level="DEBUG", rate_limit_seconds=3.5, theme="light")
    manager.save(config)
    loaded = manager.load()
    assert loaded.log_level == "DEBUG"
    assert loaded.rate_limit_seconds == 3.5
    assert loaded.theme == "light"


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"log_level": "ERROR", "from_the_future": 1}), encoding="utf-8")
    assert ConfigManager(path).load().log_level == "ERROR"


@pytest.mark.parametrize(
    "overrides",
    [
        {"log_level": "LOUD"},
        {"theme": "solarized"},
        {"request_timeout_seconds": 0},
        {"retry_count": -1},
        {"update_frequency_hours": 0},
    ],
)
def test_invalid_values_rejected(tmp_path: Path, overrides: dict) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(overrides), encoding="utf-8")
    with pytest.raises(ConfigError):
        ConfigManager(path).load()


def test_corrupt_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        ConfigManager(path).load()
