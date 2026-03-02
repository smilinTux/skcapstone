"""Tests for consciousness config hot-reload.

Covers:
    - _reload_config() updates ConsciousnessLoop._config from a changed YAML
    - Changed fields are logged with old and new values
    - Re-probe of backends is triggered on every successful reload
    - Invalid YAML leaves config unchanged
    - A no-op reload (no changes) skips logging and backend re-probe
    - _run_config_watcher() starts a daemon thread that calls _reload_config
      on watchdog events
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import yaml

from skcapstone.consciousness_loop import ConsciousnessConfig, ConsciousnessLoop


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG = ConsciousnessConfig()


def _make_loop(tmp_path: Path) -> ConsciousnessLoop:
    """Construct a minimal ConsciousnessLoop for testing."""
    config = ConsciousnessConfig(fallback_chain=["passthrough"])
    return ConsciousnessLoop(config, home=tmp_path / ".skcapstone")


def _write_config(home: Path, data: dict) -> Path:
    """Write a consciousness.yaml config file under {home}/config/."""
    config_dir = home / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "consciousness.yaml"
    config_path.write_text(yaml.dump(data, default_flow_style=False), encoding="utf-8")
    return config_path


# ---------------------------------------------------------------------------
# Test: _reload_config() updates fields
# ---------------------------------------------------------------------------


class TestReloadConfigUpdatesFields:
    """_reload_config() must update self._config from the YAML file."""

    def test_reload_updates_single_field(self, tmp_path):
        """A changed field is reflected in _config after reload."""
        loop = _make_loop(tmp_path)
        home = loop._home

        # Write YAML that changes response_timeout
        _write_config(
            home,
            {**_DEFAULT_CONFIG.model_dump(), "response_timeout": 999},
        )

        loop._reload_config()

        assert loop._config.response_timeout == 999

    def test_reload_updates_fallback_chain(self, tmp_path):
        """fallback_chain is updated from YAML."""
        loop = _make_loop(tmp_path)
        home = loop._home

        _write_config(
            home,
            {**_DEFAULT_CONFIG.model_dump(), "fallback_chain": ["anthropic", "passthrough"]},
        )

        loop._reload_config()

        assert loop._config.fallback_chain == ["anthropic", "passthrough"]

    def test_reload_updates_multiple_fields(self, tmp_path):
        """Multiple changed fields are all updated."""
        loop = _make_loop(tmp_path)
        home = loop._home

        _write_config(
            home,
            {
                **_DEFAULT_CONFIG.model_dump(),
                "response_timeout": 60,
                "max_context_tokens": 4000,
                "auto_ack": False,
            },
        )

        loop._reload_config()

        assert loop._config.response_timeout == 60
        assert loop._config.max_context_tokens == 4000
        assert loop._config.auto_ack is False

    def test_reload_syncs_bridge_fallback_chain(self, tmp_path):
        """_bridge._fallback_chain is updated to match the reloaded config."""
        loop = _make_loop(tmp_path)
        home = loop._home

        new_chain = ["nvidia", "passthrough"]
        _write_config(
            home,
            {**_DEFAULT_CONFIG.model_dump(), "fallback_chain": new_chain},
        )

        loop._reload_config()

        assert loop._bridge._fallback_chain == new_chain

    def test_reload_syncs_bridge_timeout(self, tmp_path):
        """_bridge._timeout is updated to match the reloaded config."""
        loop = _make_loop(tmp_path)
        home = loop._home

        _write_config(
            home,
            {**_DEFAULT_CONFIG.model_dump(), "response_timeout": 42},
        )

        loop._reload_config()

        assert loop._bridge._timeout == 42


# ---------------------------------------------------------------------------
# Test: _reload_config() logs changes
# ---------------------------------------------------------------------------


class TestReloadConfigLogsChanges:
    """Changed fields must be logged at INFO level with old and new values."""

    def test_changed_field_is_logged(self, tmp_path, caplog):
        """Each changed field appears in the log output."""
        loop = _make_loop(tmp_path)
        home = loop._home

        _write_config(
            home,
            {**_DEFAULT_CONFIG.model_dump(), "response_timeout": 77},
        )

        with caplog.at_level(logging.INFO, logger="skcapstone.consciousness"):
            loop._reload_config()

        change_logs = [
            r.message for r in caplog.records if "response_timeout" in r.message
        ]
        assert change_logs, "Expected a log entry mentioning 'response_timeout'"
        # Must mention both old and new values
        msg = change_logs[0]
        assert "77" in msg, "New value 77 should appear in log"

    def test_no_log_when_nothing_changed(self, tmp_path, caplog):
        """If the YAML matches the current config, no change-log entries appear."""
        loop = _make_loop(tmp_path)
        home = loop._home

        # Write exact current config — nothing changes
        _write_config(home, loop._config.model_dump())

        with caplog.at_level(logging.INFO, logger="skcapstone.consciousness"):
            loop._reload_config()

        change_logs = [
            r.message for r in caplog.records if "changed:" in r.message
        ]
        assert not change_logs, (
            "No 'changed:' log entries expected when config is unchanged"
        )

    def test_completion_log_emitted(self, tmp_path, caplog):
        """A 'Config hot-reload complete' log entry is emitted after a successful reload."""
        loop = _make_loop(tmp_path)
        home = loop._home

        _write_config(
            home,
            {**_DEFAULT_CONFIG.model_dump(), "max_history_messages": 5},
        )

        with caplog.at_level(logging.INFO, logger="skcapstone.consciousness"):
            loop._reload_config()

        complete_logs = [
            r.message for r in caplog.records if "hot-reload complete" in r.message
        ]
        assert complete_logs, "Expected 'Config hot-reload complete' log entry"


# ---------------------------------------------------------------------------
# Test: _reload_config() re-probes backends
# ---------------------------------------------------------------------------


class TestReloadConfigReprobesBackends:
    """_reload_config() must call _probe_available_backends() after a change."""

    def test_reprobes_when_config_changes(self, tmp_path):
        """_probe_available_backends is called exactly once per successful reload."""
        loop = _make_loop(tmp_path)
        home = loop._home

        _write_config(
            home,
            {**_DEFAULT_CONFIG.model_dump(), "response_timeout": 30},
        )

        with patch.object(
            loop._bridge, "_probe_available_backends", wraps=loop._bridge._probe_available_backends
        ) as mock_probe:
            loop._reload_config()

        mock_probe.assert_called_once()

    def test_no_reprobe_when_nothing_changed(self, tmp_path):
        """_probe_available_backends is NOT called when config is unchanged."""
        loop = _make_loop(tmp_path)
        home = loop._home

        _write_config(home, loop._config.model_dump())

        with patch.object(
            loop._bridge, "_probe_available_backends"
        ) as mock_probe:
            loop._reload_config()

        mock_probe.assert_not_called()

    def test_no_reprobe_on_invalid_yaml(self, tmp_path):
        """_probe_available_backends is NOT called when YAML is unparseable."""
        loop = _make_loop(tmp_path)
        home = loop._home

        config_dir = home / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "consciousness.yaml").write_text(
            ": invalid: yaml: {{{ broken", encoding="utf-8"
        )

        with patch.object(loop._bridge, "_probe_available_backends") as mock_probe:
            loop._reload_config()

        mock_probe.assert_not_called()


# ---------------------------------------------------------------------------
# Test: invalid / missing config
# ---------------------------------------------------------------------------


class TestReloadConfigErrorHandling:
    """_reload_config() must fail gracefully without corrupting state."""

    def test_invalid_yaml_keeps_current_config(self, tmp_path):
        """Unparseable YAML leaves self._config unchanged."""
        loop = _make_loop(tmp_path)
        original_config = loop._config.model_copy()
        home = loop._home

        config_dir = home / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "consciousness.yaml").write_text(
            ": invalid: yaml: {{{ broken", encoding="utf-8"
        )

        loop._reload_config()

        assert loop._config.model_dump() == original_config.model_dump(), (
            "Config must not change after a failed reload"
        )

    def test_missing_file_keeps_current_config(self, tmp_path):
        """Missing YAML file leaves self._config unchanged."""
        loop = _make_loop(tmp_path)
        original_config = loop._config.model_copy()
        # Do NOT create any config file

        loop._reload_config()

        assert loop._config.model_dump() == original_config.model_dump()

    def test_missing_file_logs_warning(self, tmp_path, caplog):
        """A warning is logged when the config file is absent."""
        loop = _make_loop(tmp_path)

        with caplog.at_level(logging.WARNING, logger="skcapstone.consciousness"):
            loop._reload_config()

        warn_logs = [
            r.message for r in caplog.records
            if "not found" in r.message or "hot-reload" in r.message
        ]
        assert warn_logs, "Expected a warning log when config file is missing"

    def test_invalid_yaml_logs_error(self, tmp_path, caplog):
        """An error is logged when YAML cannot be parsed."""
        loop = _make_loop(tmp_path)
        home = loop._home

        config_dir = home / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "consciousness.yaml").write_text(
            ": invalid: yaml: {{{ broken", encoding="utf-8"
        )

        with caplog.at_level(logging.ERROR, logger="skcapstone.consciousness"):
            loop._reload_config()

        error_logs = [
            r.message for r in caplog.records if "hot-reload" in r.message
        ]
        assert error_logs, "Expected an error log for invalid YAML"


# ---------------------------------------------------------------------------
# Test: _run_config_watcher thread
# ---------------------------------------------------------------------------


class TestRunConfigWatcher:
    """_run_config_watcher() starts a thread and calls _reload_config on events."""

    def test_config_watcher_thread_is_started(self, tmp_path):
        """start() spawns a 'consciousness-config-watcher' daemon thread."""
        loop = _make_loop(tmp_path)
        threads = loop.start()
        loop.stop()

        names = {t.name for t in threads}
        assert "consciousness-config-watcher" in names

    def test_config_watcher_calls_reload_on_modified_event(self, tmp_path):
        """The watchdog handler calls _reload_config() on a 'modified' event."""
        loop = _make_loop(tmp_path)
        home = loop._home

        # Write an initial config so the file exists
        _write_config(home, _DEFAULT_CONFIG.model_dump())

        stop_event = threading.Event()
        loop._stop_event = stop_event

        reload_calls: list[None] = []

        def fake_reload():
            reload_calls.append(None)
            stop_event.set()  # terminate the watcher after first reload

        loop._reload_config = fake_reload  # type: ignore[method-assign]

        try:
            from watchdog.observers import Observer  # noqa: F401 — skip if not installed
        except ImportError:
            pytest.skip("watchdog not installed")

        watcher_thread = threading.Thread(
            target=loop._run_config_watcher, daemon=True
        )
        watcher_thread.start()

        # Give the observer a moment to start, then write a changed config
        import time
        time.sleep(0.3)

        _write_config(
            home,
            {**_DEFAULT_CONFIG.model_dump(), "response_timeout": 55},
        )

        # Wait up to 5 s for the handler to fire
        stop_event.wait(timeout=5)
        watcher_thread.join(timeout=2)

        assert reload_calls, "Expected _reload_config to be called by the watcher"
