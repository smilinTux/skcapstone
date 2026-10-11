"""Tests for the telemetry opt-out profile default."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from skcapstone import telemetry_optout
from skcapstone.register import register_all


def test_writes_env_file_and_hyperframes_config(tmp_path: Path) -> None:
    env_dir, home = tmp_path / "env.d", tmp_path / "home"
    changed = telemetry_optout.ensure_telemetry_optout(env_dir=env_dir, home=home)

    env_file = env_dir / telemetry_optout.ENV_FILENAME
    assert "HYPERFRAMES_NO_TELEMETRY=1" in env_file.read_text(encoding="utf-8").splitlines()
    config = json.loads((home / ".hyperframes" / "config.json").read_text(encoding="utf-8"))
    assert config == {"telemetryEnabled": False}
    assert len(changed) == 2


def test_idempotent(tmp_path: Path) -> None:
    kwargs = {"env_dir": tmp_path / "env.d", "home": tmp_path / "home"}
    telemetry_optout.ensure_telemetry_optout(**kwargs)
    assert telemetry_optout.ensure_telemetry_optout(**kwargs) == []


def test_preserves_other_hyperframes_keys(tmp_path: Path) -> None:
    config = tmp_path / "home" / ".hyperframes" / "config.json"
    config.parent.mkdir(parents=True)
    original = {"anonymousId": "abc", "telemetryEnabled": True}
    config.write_text(json.dumps(original), encoding="utf-8")
    telemetry_optout.ensure_telemetry_optout(env_dir=tmp_path / "env.d", home=tmp_path / "home")
    assert json.loads(config.read_text(encoding="utf-8")) == {
        "anonymousId": "abc",
        "telemetryEnabled": False,
    }


def test_unparseable_hyperframes_config_is_left_alone(tmp_path: Path) -> None:
    config = tmp_path / "home" / ".hyperframes" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text("{not json", encoding="utf-8")
    telemetry_optout.ensure_telemetry_optout(env_dir=tmp_path / "env.d", home=tmp_path / "home")
    assert config.read_text(encoding="utf-8") == "{not json"


def test_bashrc_exports() -> None:
    assert "export HYPERFRAMES_NO_TELEMETRY=1\n" in telemetry_optout.bashrc_exports()


def _register(tmp_path: Path) -> dict:
    with (
        patch("skcapstone.register._build_package_registry", return_value=[]),
        patch("skcapstone.register._discover_plugin_servers", return_value=[]),
    ):
        return register_all(workspace=tmp_path, environments=[], dry_run=False)


def test_register_all_applies_profile_defaults(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("skskills.agent_links")
    monkeypatch.setenv("SKCAPSTONE_PROFILE_DEFAULTS", "1")
    monkeypatch.setenv("SKSKILLS_AGENT_LINKS", "1")
    calls = {}
    monkeypatch.setattr(
        telemetry_optout, "ensure_telemetry_optout", lambda: calls.setdefault("optout", ["x"])
    )
    with patch("skskills.agent_links.sync", return_value={"linked": 3}) as sync:
        results = _register(tmp_path)
    assert results["telemetry_optout"] == {"action": "updated", "paths": ["x"]}
    assert results["agent_skill_links"] == {"linked": 3}
    sync.assert_called_once()


def test_register_all_skips_profile_defaults_when_off(tmp_path: Path) -> None:
    # conftest sets SKCAPSTONE_PROFILE_DEFAULTS=0 for every test.
    results = _register(tmp_path)
    assert results["telemetry_optout"]["action"] == "skip"
    assert results["agent_skill_links"]["action"] == "skip"
