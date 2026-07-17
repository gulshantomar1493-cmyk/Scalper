"""Logging setup (roadmap P0.2).

One place configures logging for the whole single-process app:
console + rotating file, UTC timestamps everywhere (candle discipline is UTC).
No module may configure its own handlers or print() — always
`logging.getLogger(__name__)`.
"""

from __future__ import annotations

import logging
import logging.handlers
import time
from pathlib import Path

LOG_FILE = "marketscalper.log"
_FORMAT = "%(asctime)s.%(msecs)03dZ %(levelname)-8s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S"


def setup_logging(level: str = "INFO", log_dir: str = "logs") -> None:
    """Configure the root logger. Idempotent: replaces existing handlers."""
    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)
    formatter.converter = time.gmtime  # UTC, always

    console = logging.StreamHandler()
    console.setFormatter(formatter)

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path / LOG_FILE, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)
