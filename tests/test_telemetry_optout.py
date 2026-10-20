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
    assert config == {"telemetryEnabled": False, telemetry_optout.MARKER_KEY: True}
    assert len(changed) == 2


def test_idempotent(tmp_path: Path) -> None:
    kwargs = {"env_dir": tmp_path / "env.d", "home": tmp_path / "home"}
    telemetry_optout.ensure_telemetry_optout(**kwargs)
    assert telemetry_optout.ensure_telemetry_optout(**kwargs) == []


def test_explicit_opt_in_is_respected_and_nothing_is_written(tmp_path: Path) -> None:
    config = tmp_path / "home" / ".hyperframes" / "config.json"
    config.parent.mkdir(parents=True)
    original = {"anonymousId": "abc", "telemetryEnabled": True}
    config.write_text(json.dumps(original), encoding="utf-8")
    report = telemetry_optout.ensure_telemetry_optout_report(
        env_dir=tmp_path / "env.d", home=tmp_path / "home"
    )
    assert json.loads(config.read_text(encoding="utf-8")) == original
    assert not (tmp_path / "env.d").exists()
    assert report["changed"] == [] and "respected" in report["skipped"][0]["reason"]


def test_absent_key_is_set_marked_and_other_keys_preserved(tmp_path: Path) -> None:
    config = tmp_path / "home" / ".hyperframes" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"anonymousId": "abc"}), encoding="utf-8")
    telemetry_optout.ensure_telemetry_optout(env_dir=tmp_path / "env.d", home=tmp_path / "home")
    assert json.loads(config.read_text(encoding="utf-8")) == {
        "anonymousId": "abc",
        "telemetryEnabled": False,
        telemetry_optout.MARKER_KEY: True,
    }


def test_explicit_false_is_left_alone_and_not_marked(tmp_path: Path) -> None:
    config = tmp_path / "home" / ".hyperframes" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"telemetryEnabled": False}), encoding="utf-8")
    telemetry_optout.ensure_telemetry_optout(env_dir=tmp_path / "env.d", home=tmp_path / "home")
    assert json.loads(config.read_text(encoding="utf-8")) == {"telemetryEnabled": False}


def test_env_file_symlink_is_not_written_through(tmp_path: Path) -> None:
    env_dir = tmp_path / "env.d"
    env_dir.mkdir()
    target = tmp_path / "keep.conf"
    target.write_text("KEEP=1\n", encoding="utf-8")
    (env_dir / telemetry_optout.ENV_FILENAME).symlink_to(target)
    report = telemetry_optout.ensure_telemetry_optout_report(
        env_dir=env_dir, home=tmp_path / "home"
    )
    assert target.read_text(encoding="utf-8") == "KEEP=1\n"
    assert any(s["reason"] == "symlink" for s in report["skipped"])


def test_config_symlink_and_symlinked_parent_are_not_written_through(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"x": 1}), encoding="utf-8")
    home = tmp_path / "home"
    (home / ".hyperframes").mkdir(parents=True)
    (home / ".hyperframes" / "config.json").symlink_to(real)
    telemetry_optout.ensure_telemetry_optout(env_dir=tmp_path / "env.d", home=home)
    assert json.loads(real.read_text(encoding="utf-8")) == {"x": 1}

    home2 = tmp_path / "home2"
    home2.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (home2 / ".hyperframes").symlink_to(elsewhere)
    telemetry_optout.ensure_telemetry_optout(env_dir=tmp_path / "env2.d", home=home2)
    assert list(elsewhere.iterdir()) == []


def test_undo_removes_only_what_we_wrote(tmp_path: Path) -> None:
    kwargs = {"env_dir": tmp_path / "env.d", "home": tmp_path / "home"}
    config = tmp_path / "home" / ".hyperframes" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"anonymousId": "abc"}), encoding="utf-8")
    telemetry_optout.ensure_telemetry_optout(**kwargs)
    changed = telemetry_optout.undo_telemetry_optout(**kwargs)
    assert len(changed) == 2
    assert not (tmp_path / "env.d" / telemetry_optout.ENV_FILENAME).exists()
    assert json.loads(config.read_text(encoding="utf-8")) == {"anonymousId": "abc"}
    assert telemetry_optout.undo_telemetry_optout(**kwargs) == []


def test_undo_keeps_a_users_own_false_and_foreign_env_file(tmp_path: Path) -> None:
    env_dir = tmp_path / "env.d"
    env_dir.mkdir()
    foreign = env_dir / telemetry_optout.ENV_FILENAME
    foreign.write_text("MINE=1\n", encoding="utf-8")
    config = tmp_path / "home" / ".hyperframes" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"telemetryEnabled": False}), encoding="utf-8")
    assert telemetry_optout.undo_telemetry_optout(env_dir=env_dir, home=tmp_path / "home") == []
    assert foreign.read_text(encoding="utf-8") == "MINE=1\n"
    assert json.loads(config.read_text(encoding="utf-8")) == {"telemetryEnabled": False}


def test_onboard_update_rewrites_the_profile_block_once(tmp_path: Path, monkeypatch) -> None:
    from skcapstone import onboard

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("rich.prompt.Confirm.ask", lambda *a, **k: True)
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text("# mine\nalias x=y\n", encoding="utf-8")
    for agent in ("one", "two", "three"):
        onboard._step_shell_profile(tmp_path / "agents" / agent, agent, agent)
    text = bashrc.read_text(encoding="utf-8")
    marker = "# --- SKCapstone profile ---"
    assert text.count(marker) == 2  # one block: open + close
    assert text.count("HYPERFRAMES_NO_TELEMETRY") == 1
    assert 'SKCAPSTONE_AGENT="three"' in text and '"one"' not in text
    assert text.startswith("# mine\nalias x=y\n")


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
        telemetry_optout,
        "ensure_telemetry_optout_report",
        lambda: {"changed": calls.setdefault("optout", ["x"]), "skipped": []},
    )
    seen = []
    _fake_agent_links(monkeypatch, lambda registry: seen.append(registry) or {"linked": 3})
    results = _register(tmp_path)
    assert results["telemetry_optout"] == {"action": "updated", "paths": ["x"], "skipped": []}
    assert results["agent_skill_links"] == {"linked": 3}
    assert len(seen) == 1


def test_register_all_skips_links_when_skskills_lacks_agent_links(
    tmp_path: Path, monkeypatch
) -> None:
    import sys

    monkeypatch.setenv("SKCAPSTONE_PROFILE_DEFAULTS", "1")
    monkeypatch.setattr(
        telemetry_optout, "ensure_telemetry_optout_report", lambda: {"changed": [], "skipped": []}
    )
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


def test_existing_non_bool_value_is_left_alone(tmp_path: Path) -> None:
    config = tmp_path / "home" / ".hyperframes" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"telemetryEnabled": "false"}), encoding="utf-8")
    telemetry_optout.ensure_telemetry_optout(env_dir=tmp_path / "env.d", home=tmp_path / "home")
    assert json.loads(config.read_text(encoding="utf-8"))["telemetryEnabled"] == "false"


def test_optout_never_sets_a_collecting_value() -> None:
    assert all(v == "1" for v in telemetry_optout.OPTOUT_ENV.values())
    assert all("NO_TELEMETRY" in k for k in telemetry_optout.OPTOUT_ENV)
