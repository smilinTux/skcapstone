"""Only a qualifying node that is also a worker destination is graded on the sandbox."""

import json
from pathlib import Path

import pytest

from skcapstone.fleet import rollout_artifacts, rollout_drift
from skcapstone.fleet.paths import paths_for_home
from skcapstone.fleet.qualified_runtime import is_qualification_host, qualification_applies

QUOTAS = {"chiap01": {}, "chiap02": {}, "chiwk12": {}}


def _policy(home: Path, body) -> None:
    root = paths_for_home(home).root
    root.mkdir(parents=True, exist_ok=True)
    (root / "production.json").write_text(body if isinstance(body, str) else json.dumps(body))


def _role(home: Path, node: str, role: str | None) -> None:
    path = paths_for_home(home).spec_path("node", "node-" + node)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"spec": {"role": role}}))


def test_listed_worker_destination_is_a_destination(tmp_path):
    _policy(tmp_path, {"node_quotas": QUOTAS, "worker_destinations": ["chiap01", "chiap02"]})
    assert is_qualification_host(tmp_path, "chiap01")
    assert is_qualification_host(tmp_path, "node-chiap02")


def test_quota_host_outside_worker_destinations_is_not(tmp_path):
    _policy(tmp_path, {"node_quotas": QUOTAS, "worker_destinations": ["chiap01", "chiap02"]})
    assert not is_qualification_host(tmp_path, "chiwk12")


def test_absent_destinations_default_to_quotas(tmp_path):
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


def test_applies_needs_a_qualifying_role_and_a_destination(tmp_path):
    _policy(tmp_path, {"node_quotas": QUOTAS, "worker_destinations": ["chiap01"]})
    _role(tmp_path, "chiap01", "builder-standby")
    _role(tmp_path, "chiwk12", "builder-standby")  # right role, not a destination
    assert qualification_applies(tmp_path, "chiap01")
    assert not qualification_applies(tmp_path, "chiwk12")
    assert not qualification_applies(tmp_path, "chiap10")  # no admitted spec: no qualified work


def test_non_qualifying_role_is_exempt_even_in_the_destination_list(tmp_path):
    _policy(tmp_path, {"node_quotas": QUOTAS})
    _role(tmp_path, "chiap01", "observer")
    assert not qualification_applies(tmp_path, "chiap01")


def test_spec_store_failure_fails_closed(tmp_path, monkeypatch):
    from skcapstone.fleet import store

    _policy(tmp_path, {"node_quotas": QUOTAS})

    def boom(*args, **kwargs):
        raise OSError("unreadable")

    monkeypatch.setattr(store, "read_spec", boom)
    assert qualification_applies(tmp_path, "chiap01")


def test_sandbox_profile_is_not_installed_on_an_exempt_host(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "systemd/apparmor").mkdir(parents=True)
    (repo / "systemd/apparmor/skfleet-bwrap").write_text("profile\n")
    home = tmp_path / "home"
    _policy(home, {"node_quotas": QUOTAS, "worker_destinations": ["chiap01"]})
    _role(home, "chiap01", "builder-standby")
    _role(home, "chiwk12", "builder-standby")
    calls = []
    monkeypatch.setattr(rollout_artifacts.subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(rollout_artifacts, "self_node_name", lambda: "chiwk12")
    rollout_artifacts.install_sandbox_profile(repo, home)
    assert calls == []
    monkeypatch.setattr(rollout_artifacts, "self_node_name", lambda: "node-chiap01")
    try:
        rollout_artifacts.install_sandbox_profile(repo, home)
    except OSError:
        pass  # the real target path is not writable here; the attempt is the point
    assert calls  # a destination still tries the install


def test_drift_skips_sandbox_checks_on_an_exempt_host(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _policy(home, {"node_quotas": QUOTAS, "worker_destinations": ["chiap01"]})
    _role(home, "chiwk12", "builder-standby")
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
    _role(home, "chiap01", "builder-standby")
    monkeypatch.setattr(rollout_drift, "self_node_name", lambda: "chiap01")
    try:
        rollout_drift.detect_drift(
            {"git_sha": "x", "units": [], "required_env": []}, home, tmp_path
        )
    except Exception:
        pass
    assert seen  # a destination is still graded
