"""Regression tests for the fixed config loading order (P0.2 behavior).

Order — never change: config.example.yaml -> config.yaml (git-ignored)
-> environment variables override YAML.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from marketscalper.config import load_config


def test_layer1_example_only_defaults(config_dir):
    cfg = load_config(config_dir=config_dir)
    assert cfg.app.log_level == "INFO"
    assert cfg.app.log_dir == "logs"
    assert cfg.symbols == ("BTCUSDT", "ETHUSDT")
    assert cfg.timeframes == ("1m", "5m")
    assert cfg.database.dsn == ""  # secret never ships in the example layer


def test_layer2_local_overrides_example_and_inherits_rest(config_dir):
    (config_dir / "config.yaml").write_text(
        "app:\n  log_level: DEBUG\ndatabase:\n  dsn: postgresql://local\n",
        encoding="utf-8",
    )
    cfg = load_config(config_dir=config_dir)
    assert cfg.app.log_level == "DEBUG"                 # overridden
    assert cfg.database.dsn == "postgresql://local"     # overridden
    assert cfg.symbols == ("BTCUSDT", "ETHUSDT")        # inherited from example
    assert cfg.app.log_dir == "logs"                    # inherited nested key


def test_layer3_env_overrides_both_yaml_layers(config_dir, monkeypatch):
    (config_dir / "config.yaml").write_text(
        "app:\n  log_level: DEBUG\ndatabase:\n  dsn: postgresql://local\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("MARKETSCALPER_LOG_LEVEL", "warning")
    monkeypatch.setenv("MARKETSCALPER_DB_DSN", "postgresql://env-wins")
    cfg = load_config(config_dir=config_dir)
    assert cfg.app.log_level == "WARNING"               # env beats config.yaml (+ uppercased)
    assert cfg.database.dsn == "postgresql://env-wins"  # env beats config.yaml


def test_env_list_variables_parse_comma_separated(config_dir, monkeypatch):
    monkeypatch.setenv("MARKETSCALPER_SYMBOLS", "BTCUSDT")
    monkeypatch.setenv("MARKETSCALPER_TIMEFRAMES", " 1m , 5m ")
    cfg = load_config(config_dir=config_dir)
    assert cfg.symbols == ("BTCUSDT",)
    assert cfg.timeframes == ("1m", "5m")  # whitespace stripped


def test_missing_example_layer_fails_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(config_dir=Path(tmp_path / "nowhere"))
