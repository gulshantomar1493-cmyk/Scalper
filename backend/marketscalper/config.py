"""Layered configuration loader (roadmap P0.2).

Loading order — fixed by owner decision, never change:

    1. backend/config.example.yaml   (committed; documents every setting)
    2. backend/config.yaml           (git-ignored; local overrides, optional)
    3. Environment variables         (override everything)

Secrets (e.g. the database DSN) are NEVER committed to git: the example file
ships an empty value and real values come from config.yaml or environment.

Env override map (explicit, no magic):

    MARKETSCALPER_LOG_LEVEL   -> app.log_level
    MARKETSCALPER_LOG_DIR     -> app.log_dir
    MARKETSCALPER_DB_DSN      -> database.dsn
    MARKETSCALPER_SYMBOLS     -> symbols      (comma-separated)
    MARKETSCALPER_TIMEFRAMES  -> timeframes   (comma-separated)
    MARKETSCALPER_CONFIG_DIR  -> directory containing the YAML files
                                 (defaults to the backend/ directory)

    MARKETSCALPER_REGIME_COMPRESSION_RATIO        -> regime.compression_ratio
    MARKETSCALPER_REGIME_EXPANSION_RATIO          -> regime.expansion_ratio
    MARKETSCALPER_REGIME_MEDIAN_WINDOW_BARS       -> regime.median_window_bars
    MARKETSCALPER_MOMENTUM_SHIFT_ACCEL_ATR_RATIO  -> momentum.shift_accel_atr_ratio

The regime.* / momentum.* keys are the D9 (P1.3) constants — the config
plumbing the decision recorded as owed. Their defaults EQUAL the frozen
§4.2 literals, so an absent config is byte-identical to the engines'
hardcoded defaults (RegimeConfig() / MomentumState's default ratio); they
exist so the P5.3 calibration sweep can vary them. They are explicitly
UNCALIBRATED (D9) — do not tune them outside a P5 campaign.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

EXAMPLE_FILE = "config.example.yaml"
LOCAL_FILE = "config.yaml"  # git-ignored


@dataclass(frozen=True)
class AppConfig:
    log_level: str = "INFO"
    log_dir: str = "logs"


@dataclass(frozen=True)
class DatabaseConfig:
    dsn: str = ""  # secret — env/local config only, never committed


@dataclass(frozen=True)
class RegimeConfig:
    """D9 (P1.3) regime constants — defaults = the frozen §4.2 literals."""

    compression_ratio: float = 0.6
    expansion_ratio: float = 1.5
    median_window_bars: int = 240


@dataclass(frozen=True)
class MomentumConfig:
    """D9 (P1.3) momentum-shift constant — default = the frozen §4.2 literal."""

    shift_accel_atr_ratio: float = 0.1


@dataclass(frozen=True)
class Config:
    app: AppConfig
    database: DatabaseConfig
    symbols: tuple[str, ...]
    timeframes: tuple[str, ...]
    regime: RegimeConfig
    momentum: MomentumConfig


def _default_config_dir() -> Path:
    """backend/ — the directory that contains this package."""
    return Path(__file__).resolve().parent.parent


def _read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    return data


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursive dict merge — override wins; nested dicts merge key-wise."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _apply_env(raw: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    """Step 3: environment variables override YAML (explicit map, no magic)."""
    out = dict(raw)
    out.setdefault("app", {})
    out.setdefault("database", {})

    if "MARKETSCALPER_LOG_LEVEL" in env:
        out["app"] = {**out["app"], "log_level": env["MARKETSCALPER_LOG_LEVEL"]}
    if "MARKETSCALPER_LOG_DIR" in env:
        out["app"] = {**out["app"], "log_dir": env["MARKETSCALPER_LOG_DIR"]}
    if "MARKETSCALPER_DB_DSN" in env:
        out["database"] = {**out["database"], "dsn": env["MARKETSCALPER_DB_DSN"]}
    if "MARKETSCALPER_SYMBOLS" in env:
        out["symbols"] = [s.strip() for s in env["MARKETSCALPER_SYMBOLS"].split(",") if s.strip()]
    if "MARKETSCALPER_TIMEFRAMES" in env:
        out["timeframes"] = [t.strip() for t in env["MARKETSCALPER_TIMEFRAMES"].split(",") if t.strip()]

    out.setdefault("regime", {})
    out.setdefault("momentum", {})
    for env_key, section, key in (
        ("MARKETSCALPER_REGIME_COMPRESSION_RATIO", "regime", "compression_ratio"),
        ("MARKETSCALPER_REGIME_EXPANSION_RATIO", "regime", "expansion_ratio"),
        ("MARKETSCALPER_REGIME_MEDIAN_WINDOW_BARS", "regime", "median_window_bars"),
        ("MARKETSCALPER_MOMENTUM_SHIFT_ACCEL_ATR_RATIO", "momentum",
         "shift_accel_atr_ratio"),
    ):
        if env_key in env:
            out[section] = {**out[section], key: env[env_key]}
    return out


def _pos_float(value: Any, name: str) -> float:
    """Coerce to a strictly-positive float or refuse to start (D9 constants
    are ratios — zero/negative/non-numeric is a misconfiguration)."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {value!r}")
    if not (out > 0.0) or out != out or out in (float("inf"), float("-inf")):
        raise ValueError(f"{name} must be finite and > 0, got {out}")
    return out


def _pos_int(value: Any, name: str) -> int:
    """Coerce to a strictly-positive int or refuse to start."""
    try:
        out = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be an integer, got {value!r}")
    if out <= 0:
        raise ValueError(f"{name} must be > 0, got {out}")
    return out


def load_config(config_dir: Path | None = None) -> Config:
    """Load configuration in the fixed order: example -> local -> env."""
    env = dict(os.environ)
    if config_dir is None:
        config_dir = Path(env.get("MARKETSCALPER_CONFIG_DIR", _default_config_dir()))

    example_path = config_dir / EXAMPLE_FILE
    if not example_path.is_file():
        raise FileNotFoundError(
            f"{example_path} missing — config.example.yaml is the committed base layer"
        )
    raw = _read_yaml(example_path)                      # layer 1

    local_path = config_dir / LOCAL_FILE
    if local_path.is_file():
        raw = _merge(raw, _read_yaml(local_path))       # layer 2 (optional)

    raw = _apply_env(raw, env)                          # layer 3

    app_raw = raw.get("app") or {}
    db_raw = raw.get("database") or {}
    regime_raw = raw.get("regime") or {}
    momentum_raw = raw.get("momentum") or {}
    regime_defaults = RegimeConfig()
    momentum_defaults = MomentumConfig()
    return Config(
        app=AppConfig(
            log_level=str(app_raw.get("log_level", "INFO")).upper(),
            log_dir=str(app_raw.get("log_dir", "logs")),
        ),
        database=DatabaseConfig(dsn=str(db_raw.get("dsn", "") or "")),
        symbols=tuple(raw.get("symbols") or ()),
        timeframes=tuple(raw.get("timeframes") or ()),
        regime=RegimeConfig(
            compression_ratio=_pos_float(
                regime_raw.get("compression_ratio",
                               regime_defaults.compression_ratio),
                "regime.compression_ratio"),
            expansion_ratio=_pos_float(
                regime_raw.get("expansion_ratio",
                               regime_defaults.expansion_ratio),
                "regime.expansion_ratio"),
            median_window_bars=_pos_int(
                regime_raw.get("median_window_bars",
                               regime_defaults.median_window_bars),
                "regime.median_window_bars"),
        ),
        momentum=MomentumConfig(
            shift_accel_atr_ratio=_pos_float(
                momentum_raw.get("shift_accel_atr_ratio",
                                 momentum_defaults.shift_accel_atr_ratio),
                "momentum.shift_accel_atr_ratio"),
        ),
    )
