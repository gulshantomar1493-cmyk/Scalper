"""Regression tests for the fixed config loading order (P0.2 behavior).

Order — never change: config.example.yaml -> config.yaml (git-ignored)
-> environment variables override YAML.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from marketscalper.config import MomentumConfig, RegimeConfig, load_config


def test_layer1_example_only_defaults(config_dir):
    cfg = load_config(config_dir=config_dir)
    assert cfg.app.log_level == "INFO"
    assert cfg.app.log_dir == "logs"
    assert cfg.symbols == ("BTCUSDT", "ETHUSDT")
    assert cfg.timeframes == ("1m", "5m")
    assert cfg.database.dsn == ""  # secret never ships in the example layer
    # D9 config-plumbing: defaults EQUAL the frozen §4.2 literals
    assert cfg.regime == RegimeConfig(0.6, 1.5, 240)
    assert cfg.momentum == MomentumConfig(0.1)


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


# ------------------------------------------------ D9 regime/momentum plumbing


def test_regime_momentum_from_local_yaml_layer2(config_dir):
    (config_dir / "config.yaml").write_text(
        "regime:\n  compression_ratio: 0.5\n  median_window_bars: 300\n"
        "momentum:\n  shift_accel_atr_ratio: 0.25\n",
        encoding="utf-8",
    )
    cfg = load_config(config_dir=config_dir)
    assert cfg.regime.compression_ratio == 0.5        # overridden
    assert cfg.regime.median_window_bars == 300       # overridden
    assert cfg.regime.expansion_ratio == 1.5          # inherited from example
    assert cfg.momentum.shift_accel_atr_ratio == 0.25


def test_env_overrides_regime_momentum_layer3(config_dir, monkeypatch):
    (config_dir / "config.yaml").write_text(
        "regime:\n  expansion_ratio: 1.9\n", encoding="utf-8")
    monkeypatch.setenv("MARKETSCALPER_REGIME_EXPANSION_RATIO", "2.1")
    monkeypatch.setenv("MARKETSCALPER_REGIME_COMPRESSION_RATIO", "0.55")
    monkeypatch.setenv("MARKETSCALPER_REGIME_MEDIAN_WINDOW_BARS", "180")
    monkeypatch.setenv("MARKETSCALPER_MOMENTUM_SHIFT_ACCEL_ATR_RATIO", "0.3")
    cfg = load_config(config_dir=config_dir)
    assert cfg.regime.expansion_ratio == 2.1          # env beats config.yaml
    assert cfg.regime.compression_ratio == 0.55
    assert cfg.regime.median_window_bars == 180       # int-coerced
    assert cfg.momentum.shift_accel_atr_ratio == 0.3


@pytest.mark.parametrize("var,bad", [
    ("MARKETSCALPER_REGIME_EXPANSION_RATIO", "-1"),
    ("MARKETSCALPER_REGIME_COMPRESSION_RATIO", "0"),
    ("MARKETSCALPER_REGIME_EXPANSION_RATIO", "abc"),
    ("MARKETSCALPER_MOMENTUM_SHIFT_ACCEL_ATR_RATIO", "0"),
    ("MARKETSCALPER_REGIME_MEDIAN_WINDOW_BARS", "-5"),
    ("MARKETSCALPER_REGIME_MEDIAN_WINDOW_BARS", "1.5"),   # non-integer
])
def test_bad_regime_momentum_values_refuse_to_start(config_dir, monkeypatch,
                                                    var, bad):
    monkeypatch.setenv(var, bad)
    with pytest.raises(ValueError):
        load_config(config_dir=config_dir)


def test_config_defaults_equal_engine_defaults_byte_identical():
    # The byte-identical contract: the config-layer D9 defaults must EQUAL
    # the frozen engine defaults, so an absent config == hardcoded engines.
    from marketscalper.engines.momentum import MomentumState
    from marketscalper.engines.momentum import RegimeConfig as EngineRegime
    cfg_regime = RegimeConfig()
    eng_regime = EngineRegime()
    assert (cfg_regime.compression_ratio, cfg_regime.expansion_ratio,
            cfg_regime.median_window_bars) == (
        eng_regime.compression_ratio, eng_regime.expansion_ratio,
        eng_regime.median_window_bars)
    # MomentumState's default ratio is the momentum config default
    import inspect
    sig = inspect.signature(MomentumState.__init__)
    assert (sig.parameters["shift_accel_atr_ratio"].default
            == MomentumConfig().shift_accel_atr_ratio == 0.1)


def test_shipped_example_file_documents_the_d9_keys():
    # Guard against the real example file drifting from the loader — the
    # committed base layer must document every setting (P0.2 doctrine).
    text = (Path(__file__).resolve().parent.parent
            / "backend" / "config.example.yaml").read_text(encoding="utf-8")
    for key in ("regime:", "compression_ratio", "expansion_ratio",
                "median_window_bars", "momentum:", "shift_accel_atr_ratio"):
        assert key in text, key
