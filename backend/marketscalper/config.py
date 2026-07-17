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
class Config:
    app: AppConfig
    database: DatabaseConfig
    symbols: tuple[str, ...]
    timeframes: tuple[str, ...]


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
    return Config(
        app=AppConfig(
            log_level=str(app_raw.get("log_level", "INFO")).upper(),
            log_dir=str(app_raw.get("log_dir", "logs")),
        ),
        database=DatabaseConfig(dsn=str(db_raw.get("dsn", "") or "")),
        symbols=tuple(raw.get("symbols") or ()),
        timeframes=tuple(raw.get("timeframes") or ()),
    )
