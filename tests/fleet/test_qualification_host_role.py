"""Only a worker destination is graded on the qualification sandbox."""

import json
from pathlib import Path

import pytest

from skcapstone.fleet import qualified_runtime, rollout_artifacts
from skcapstone.fleet.paths import paths_for_home
from skcapstone.fleet.qualified_runtime import is_qualification_host


def _policy(home: Path, body) -> None:
    root = paths_for_home(home).root
    root.mkdir(parents=True, exist_ok=True)
    (root / "production.json").write_text(body if isinstance(body, str) else json.dumps(body))


QUOTAS = {"chiap01": {}, "chiap02": {}, "chiwk12": {}}


def test_listed_worker_destination_is_checked(tmp_path):
    _policy(tmp_path, {"node_quotas": QUOTAS, "worker_destinations": ["chiap01", "chiap02"]})
    assert is_qualification_host(tmp_path, "chiap01")


def test_quota_host_outside_worker_destinations_is_exempt(tmp_path):
    _policy(tmp_path, {"node_quotas": QUOTAS, "worker_destinations": ["chiap01", "chiap02"]})
    assert not is_qualification_host(tmp_path, "chiwk12")


def test_host_without_quota_is_exempt_and_absent_destinations_default_to_quotas(tmp_path):
    _policy(tmp_path, {"node_quotas": QUOTAS})
    assert is_qualification_host(tmp_path, "chiap01")
    assert not is_qualification_host(tmp_path, "chiap10")


@pytest.mark.parametrize(
    "body", ["{}", "not json", "[]", '{"node_quotas": []}', '{"node_quotas": 3}']
)
def test_unrecognisable_policy_fails_closed(tmp_path, body):
    _policy(tmp_path, body)
    assert is_qualification_host(tmp_path, "chiap10")


def test_missing_policy_fails_closed(tmp_path):
    assert is_qualification_host(tmp_path, "chiap10")


def test_sandbox_profile_is_not_installed_on_an_exempt_host(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "systemd/apparmor").mkdir(parents=True)
    (repo / "systemd/apparmor/skfleet-bwrap").write_text("profile\n")
    home = tmp_path / "home"
    _policy(home, {"node_quotas": QUOTAS, "worker_destinations": ["chiap01"]})
    calls = []
    monkeypatch.setattr(rollout_artifacts.subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(rollout_artifacts, "self_node_name", lambda: "chiwk12")
    rollout_artifacts.install_sandbox_profile(repo, home)
    assert calls == []
    monkeypatch.setattr(rollout_artifacts, "self_node_name", lambda: "chiap01")
    rollout_artifacts.install_sandbox_profile(repo, home)
    assert calls  # a destination still installs it


def test_drift_skips_sandbox_checks_on_an_exempt_host(tmp_path, monkeypatch):
    from skcapstone.fleet import rollout_drift

    home = tmp_path / "home"
    _policy(home, {"node_quotas": QUOTAS, "worker_destinations": ["chiap01"]})
    seen = []
    monkeypatch.setattr(rollout_drift, "missing_dependencies", lambda p: seen.append(p) or [])
    monkeypatch.setattr(rollout_drift, "self_node_name", lambda: "chiwk12")
    try:
        rollout_drift.detect_drift(
            {"git_sha": "x", "units": [], "required_env": []}, home, tmp_path
        )
    except Exception:
        pass  # later checks need a full checkout; only the gate under test matters
    assert seen == []
    assert qualified_runtime.is_qualification_host(home, "chiap01")
