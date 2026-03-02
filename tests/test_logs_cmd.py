"""Tests for ``skcapstone logs`` command.

Covers:
- help text exposes all options
- graceful handling of missing log file
- --lines N limits output to N recent lines
- --level filters by minimum log level
- --peer filters lines containing a peer name substring
- --follow outputs existing lines before entering the tail loop
- --follow combined with --level filter
- helper: _parse_level
- helper: _matches_filters
- helper: _tail
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from skcapstone.cli import main
from skcapstone.cli.logs_cmd import _matches_filters, _parse_level, _tail

runner = CliRunner()

# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

SAMPLE_LOG = (
    "2026-01-01 00:00:01,000 [skcapstone.daemon] DEBUG: debug message\n"
    "2026-01-01 00:00:02,000 [skcapstone.daemon] INFO: started up\n"
    "2026-01-01 00:00:03,000 [skcapstone.sync] WARNING: peer opus slow\n"
    "2026-01-01 00:00:04,000 [skcapstone.daemon] ERROR: transport failed\n"
    "2026-01-01 00:00:05,000 [skcapstone.daemon] INFO: heartbeat ok\n"
)


@pytest.fixture
def log_home(tmp_path: Path) -> Path:
    """Agent home with a pre-populated daemon.log."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "daemon.log").write_text(SAMPLE_LOG)
    return tmp_path


# ---------------------------------------------------------------------------
# Unit tests — _parse_level
# ---------------------------------------------------------------------------

class TestParseLevel:
    def test_info(self):
        assert _parse_level("2026-01-01 [skcapstone.daemon] INFO: hello") == "INFO"

    def test_error(self):
        assert _parse_level("2026-01-01 [transport] ERROR: connection refused") == "ERROR"

    def test_warning(self):
        assert _parse_level("... [x] WARNING: slow") == "WARNING"

    def test_debug(self):
        assert _parse_level("2026-01-01 [y] DEBUG: verbose") == "DEBUG"

    def test_critical(self):
        assert _parse_level("2026-01-01 [z] CRITICAL: crash") == "CRITICAL"

    def test_unparseable_returns_none(self):
        assert _parse_level("not a structured log line") is None

    def test_empty_string(self):
        assert _parse_level("") is None


# ---------------------------------------------------------------------------
# Unit tests — _matches_filters
# ---------------------------------------------------------------------------

class TestMatchesFilters:
    def test_no_filters_accepts_all(self):
        assert _matches_filters("any line whatsoever", None, None) is True

    def test_level_min_warning_accepts_error(self):
        line = "2026-01-01 [x] ERROR: something bad"
        assert _matches_filters(line, "WARNING", None) is True

    def test_level_min_error_rejects_warning(self):
        line = "2026-01-01 [x] WARNING: minor issue"
        assert _matches_filters(line, "ERROR", None) is False

    def test_level_min_error_rejects_info(self):
        line = "2026-01-01 [x] INFO: routine"
        assert _matches_filters(line, "ERROR", None) is False

    def test_level_exact_match(self):
        line = "2026-01-01 [x] INFO: hello"
        assert _matches_filters(line, "INFO", None) is True

    def test_level_unparseable_excluded(self):
        assert _matches_filters("plain text", "INFO", None) is False

    def test_peer_match(self):
        line = "2026-01-01 [x] INFO: message from opus"
        assert _matches_filters(line, None, "opus") is True

    def test_peer_case_insensitive(self):
        line = "2026-01-01 [x] INFO: message from OPUS"
        assert _matches_filters(line, None, "opus") is True

    def test_peer_no_match(self):
        line = "2026-01-01 [x] INFO: message from jarvis"
        assert _matches_filters(line, None, "opus") is False

    def test_combined_both_pass(self):
        line = "2026-01-01 [x] ERROR: opus transport failed"
        assert _matches_filters(line, "WARNING", "opus") is True

    def test_combined_level_fails(self):
        line = "2026-01-01 [x] DEBUG: opus verbose"
        assert _matches_filters(line, "WARNING", "opus") is False

    def test_combined_peer_fails(self):
        line = "2026-01-01 [x] ERROR: jarvis transport failed"
        assert _matches_filters(line, "WARNING", "opus") is False


# ---------------------------------------------------------------------------
# Unit tests — _tail
# ---------------------------------------------------------------------------

class TestTail:
    def test_returns_last_n_lines(self, tmp_path):
        f = tmp_path / "test.log"
        f.write_text("\n".join(f"line{i}" for i in range(20)) + "\n")
        lines = _tail(f, 5)
        assert len(lines) == 5
        assert "line19" in lines[-1]

    def test_fewer_lines_than_n(self, tmp_path):
        f = tmp_path / "test.log"
        f.write_text("a\nb\nc\n")
        lines = _tail(f, 100)
        assert len(lines) == 3

    def test_single_line(self, tmp_path):
        f = tmp_path / "test.log"
        f.write_text("only one line\n")
        lines = _tail(f, 10)
        assert len(lines) == 1
        assert "only one line" in lines[0]


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------

class TestLogsCLI:
    def test_help_shows_all_options(self):
        """``logs --help`` exposes every documented option."""
        result = runner.invoke(main, ["logs", "--help"])
        assert result.exit_code == 0
        assert "--follow" in result.output
        assert "--lines" in result.output
        assert "--level" in result.output
        assert "--peer" in result.output

    def test_missing_log_file_friendly_message(self, tmp_path):
        """Missing daemon.log prints a helpful message and exits 0."""
        result = runner.invoke(main, ["logs", "--home", str(tmp_path)])
        assert result.exit_code == 0
        assert "not found" in result.output.lower() or "log file" in result.output.lower()

    def test_lines_limits_output(self, log_home):
        """``-n 2`` returns only the 2 most recent lines."""
        result = runner.invoke(main, ["logs", "--home", str(log_home), "-n", "2"])
        assert result.exit_code == 0
        # Last 2 lines of fixture
        assert "heartbeat ok" in result.output
        assert "transport failed" in result.output
        # 3rd-to-last must not appear
        assert "peer opus slow" not in result.output

    def test_level_filter_hides_lower_levels(self, log_home):
        """``--level WARNING`` hides DEBUG and INFO lines."""
        result = runner.invoke(
            main, ["logs", "--home", str(log_home), "--level", "WARNING"]
        )
        assert result.exit_code == 0
        assert "debug message" not in result.output
        assert "started up" not in result.output
        assert "heartbeat ok" not in result.output
        assert "peer opus slow" in result.output   # WARNING
        assert "transport failed" in result.output  # ERROR

    def test_level_filter_case_insensitive(self, log_home):
        """``--level warning`` (lowercase) works identically."""
        result = runner.invoke(
            main, ["logs", "--home", str(log_home), "--level", "warning"]
        )
        assert result.exit_code == 0
        assert "peer opus slow" in result.output
        assert "debug message" not in result.output

    def test_peer_filter_only_matching_lines(self, log_home):
        """``--peer opus`` shows only lines containing 'opus'."""
        result = runner.invoke(
            main, ["logs", "--home", str(log_home), "--peer", "opus"]
        )
        assert result.exit_code == 0
        assert "peer opus slow" in result.output
        # Lines without 'opus' must be absent
        assert "heartbeat ok" not in result.output
        assert "transport failed" not in result.output
        assert "debug message" not in result.output

    def test_no_matching_lines_message(self, log_home):
        """Filtering that matches nothing prints a 'no matching' notice."""
        result = runner.invoke(
            main, ["logs", "--home", str(log_home), "--peer", "zz_no_such_peer_zz"]
        )
        assert result.exit_code == 0
        assert "no matching" in result.output.lower()

    def test_follow_shows_initial_lines(self, log_home):
        """``--follow`` outputs the last N historical lines before entering the loop."""

        def fake_sleep(_n: float) -> None:
            raise KeyboardInterrupt()

        with patch("skcapstone.cli.logs_cmd.time.sleep", fake_sleep):
            result = runner.invoke(
                main, ["logs", "--home", str(log_home), "--follow", "-n", "3"]
            )

        assert result.exit_code == 0
        # Last 3 lines of fixture
        assert "heartbeat ok" in result.output
        assert "transport failed" in result.output
        assert "peer opus slow" in result.output
        # First 2 lines must NOT appear (only last 3 requested)
        assert "debug message" not in result.output
        assert "started up" not in result.output

    def test_follow_with_level_filter(self, log_home):
        """``--follow --level ERROR`` only streams lines at ERROR or above."""

        def fake_sleep(_n: float) -> None:
            raise KeyboardInterrupt()

        with patch("skcapstone.cli.logs_cmd.time.sleep", fake_sleep):
            result = runner.invoke(
                main,
                ["logs", "--home", str(log_home), "--follow", "--level", "ERROR"],
            )

        assert result.exit_code == 0
        assert "transport failed" in result.output
        assert "debug message" not in result.output
        assert "started up" not in result.output
        assert "peer opus slow" not in result.output  # WARNING < ERROR

    def test_follow_streams_new_content(self, log_home):
        """``--follow`` reads content appended after the initial seek."""
        log_file = log_home / "logs" / "daemon.log"

        call_count = 0

        def fake_sleep(_n: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Append a new line on first poll
                with open(log_file, "a") as fh:
                    fh.write("2026-01-01 00:01:00,000 [skcapstone.daemon] INFO: new entry\n")
            else:
                raise KeyboardInterrupt()

        with patch("skcapstone.cli.logs_cmd.time.sleep", fake_sleep):
            result = runner.invoke(
                main, ["logs", "--home", str(log_home), "--follow"]
            )

        assert result.exit_code == 0
        assert "new entry" in result.output
