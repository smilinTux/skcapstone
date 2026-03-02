"""Tests for skcapstone.log_config — structured JSON logging."""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from pathlib import Path

import pytest

import skcapstone.log_config as log_config_module
from skcapstone.log_config import JsonFormatter, configure_logging


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_log_config(monkeypatch):
    """Reset the _CONFIGURED flag and remove test-added handlers after each test."""
    monkeypatch.setattr(log_config_module, "_CONFIGURED", False)
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    yield
    # Tear down any handlers added during the test.
    for h in list(root.handlers):
        if h not in handlers_before:
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
    monkeypatch.setattr(log_config_module, "_CONFIGURED", False)


# ---------------------------------------------------------------------------
# JsonFormatter tests
# ---------------------------------------------------------------------------


class TestJsonFormatter:
    def test_outputs_valid_json(self):
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="hello world",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)  # must not raise
        assert isinstance(data, dict)

    def test_required_fields_present(self):
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="skcapstone.daemon",
            level=logging.WARNING,
            pathname="",
            lineno=0,
            msg="something %s",
            args=("bad",),
            exc_info=None,
        )
        data = json.loads(formatter.format(record))
        assert data["level"] == "WARNING"
        assert data["logger"] == "skcapstone.daemon"
        assert data["msg"] == "something bad"
        assert "ts" in data
        # ts should be a parseable ISO-8601 string
        from datetime import datetime
        datetime.fromisoformat(data["ts"])  # raises if malformed

    def test_exception_info_included(self):
        formatter = JsonFormatter()
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="an error",
            args=(),
            exc_info=exc_info,
        )
        data = json.loads(formatter.format(record))
        assert "exc" in data
        assert "RuntimeError" in data["exc"]
        assert "boom" in data["exc"]

    def test_extra_fields_forwarded(self):
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.DEBUG,
            pathname="",
            lineno=0,
            msg="extra test",
            args=(),
            exc_info=None,
        )
        record.request_id = "abc-123"
        data = json.loads(formatter.format(record))
        assert data.get("request_id") == "abc-123"


# ---------------------------------------------------------------------------
# configure_logging tests
# ---------------------------------------------------------------------------


class TestConfigureLogging:
    def test_creates_rotating_file_handler(self, tmp_path):
        log_file = tmp_path / "logs" / "daemon.log"
        configure_logging(log_file)
        root = logging.getLogger()
        rotating = [
            h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        assert len(rotating) == 1
        h = rotating[0]
        assert h.maxBytes == 10 * 1024 * 1024
        assert h.backupCount == 5

    def test_file_handler_uses_json_formatter(self, tmp_path):
        log_file = tmp_path / "daemon.log"
        configure_logging(log_file)
        root = logging.getLogger()
        rotating = [
            h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        assert len(rotating) == 1
        assert isinstance(rotating[0].formatter, JsonFormatter)

    def test_creates_console_handler_at_info(self, tmp_path):
        log_file = tmp_path / "daemon.log"
        configure_logging(log_file)
        root = logging.getLogger()
        stream_handlers = [
            h
            for h in root.handlers
            if isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        assert len(stream_handlers) >= 1
        assert any(h.level == logging.INFO for h in stream_handlers)

    def test_creates_log_parent_directory(self, tmp_path):
        log_file = tmp_path / "nested" / "deep" / "daemon.log"
        assert not log_file.parent.exists()
        configure_logging(log_file)
        assert log_file.parent.exists()

    def test_idempotent_no_duplicate_handlers(self, tmp_path):
        log_file = tmp_path / "daemon.log"
        configure_logging(log_file)
        root = logging.getLogger()
        count_after_first = len(root.handlers)
        # Second call must be a no-op (_CONFIGURED is now True).
        configure_logging(log_file)
        assert len(root.handlers) == count_after_first

    def test_file_handler_log_level_is_debug_by_default(self, tmp_path):
        log_file = tmp_path / "daemon.log"
        configure_logging(log_file)
        root = logging.getLogger()
        rotating = [
            h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        assert rotating[0].level == logging.DEBUG

    def test_custom_max_bytes_and_backup_count(self, tmp_path):
        log_file = tmp_path / "daemon.log"
        configure_logging(log_file, max_bytes=5 * 1024 * 1024, backup_count=3)
        root = logging.getLogger()
        rotating = [
            h for h in root.handlers if isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        assert rotating[0].maxBytes == 5 * 1024 * 1024
        assert rotating[0].backupCount == 3
