"""Tests for logging setup (P0.2 behavior): file creation, UTC, idempotency."""

from __future__ import annotations

import logging
import re
import time

from marketscalper.logging_setup import LOG_FILE, setup_logging


def test_creates_log_dir_and_writes_utc_formatted_lines(tmp_path):
    log_dir = tmp_path / "logs"
    setup_logging(level="INFO", log_dir=str(log_dir))

    logging.getLogger("test").info("hello candle discipline")
    for handler in logging.getLogger().handlers:
        handler.flush()

    log_file = log_dir / LOG_FILE
    assert log_file.is_file()
    line = log_file.read_text(encoding="utf-8").strip().splitlines()[-1]
    # 2026-07-14T19:45:12.559Z INFO     test: hello candle discipline
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z ", line)
    assert line.endswith("hello candle discipline")


def test_timestamps_use_utc_converter(tmp_path):
    setup_logging(level="INFO", log_dir=str(tmp_path / "logs"))
    for handler in logging.getLogger().handlers:
        assert handler.formatter is not None
        assert handler.formatter.converter is time.gmtime  # UTC, always


def test_setup_is_idempotent_no_duplicate_handlers(tmp_path):
    setup_logging(level="INFO", log_dir=str(tmp_path / "logs"))
    setup_logging(level="DEBUG", log_dir=str(tmp_path / "logs"))
    root = logging.getLogger()
    assert len(root.handlers) == 2  # exactly console + rotating file
    assert root.level == logging.DEBUG  # second call's level applied
