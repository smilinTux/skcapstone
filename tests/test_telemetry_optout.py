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


def _fake_agent_links(monkeypatch, sync):
    """Install a stand-in skskills.agent_links so the test never skips on old skskills."""
    import sys
    import types

    import skskills

    fake = types.ModuleType("skskills.agent_links")
    fake.enabled = lambda: True
    fake.sync = sync
    monkeypatch.setitem(sys.modules, "skskills.agent_links", fake)
    monkeypatch.setattr(skskills, "agent_links", fake, raising=False)


def test_register_all_applies_profile_defaults(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SKCAPSTONE_PROFILE_DEFAULTS", "1")
    monkeypatch.setenv("SKSKILLS_AGENT_LINKS", "1")
    calls = {}
    monkeypatch.setattr(
        telemetry_optout, "ensure_telemetry_optout", lambda: calls.setdefault("optout", ["x"])
    )
    seen = []
    _fake_agent_links(monkeypatch, lambda registry: seen.append(registry) or {"linked": 3})
    results = _register(tmp_path)
    assert results["telemetry_optout"] == {"action": "updated", "paths": ["x"]}
    assert results["agent_skill_links"] == {"linked": 3}
    assert len(seen) == 1


def test_register_all_skips_links_when_skskills_lacks_agent_links(
    tmp_path: Path, monkeypatch
) -> None:
    import sys

    monkeypatch.setenv("SKCAPSTONE_PROFILE_DEFAULTS", "1")
    monkeypatch.setattr(telemetry_optout, "ensure_telemetry_optout", lambda: [])
    monkeypatch.setitem(sys.modules, "skskills.agent_links", None)  # forces ImportError
    results = _register(tmp_path)
    assert results["telemetry_optout"]["action"] == "exists"
    assert results["agent_skill_links"]["action"] == "skip"


def test_register_all_skips_profile_defaults_when_off(tmp_path: Path) -> None:
    # conftest sets SKCAPSTONE_PROFILE_DEFAULTS=0 for every test.
    results = _register(tmp_path)
    assert results["telemetry_optout"]["action"] == "skip"
    assert results["agent_skill_links"]["action"] == "skip"


@pytest.mark.parametrize("payload", ["[]", "null", "3", '"telemetryEnabled"'])
def test_non_object_hyperframes_config_is_left_alone(tmp_path: Path, payload: str) -> None:
    config = tmp_path / "home" / ".hyperframes" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(payload, encoding="utf-8")
    telemetry_optout.ensure_telemetry_optout(env_dir=tmp_path / "env.d", home=tmp_path / "home")
    assert config.read_text(encoding="utf-8") == payload


def test_string_false_is_not_treated_as_opted_out(tmp_path: Path) -> None:
    config = tmp_path / "home" / ".hyperframes" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"telemetryEnabled": "false"}), encoding="utf-8")
    telemetry_optout.ensure_telemetry_optout(env_dir=tmp_path / "env.d", home=tmp_path / "home")
    assert json.loads(config.read_text(encoding="utf-8"))["telemetryEnabled"] is False


def test_optout_never_sets_a_collecting_value() -> None:
    assert all(v == "1" for v in telemetry_optout.OPTOUT_ENV.values())
    assert all("NO_TELEMETRY" in k for k in telemetry_optout.OPTOUT_ENV)
