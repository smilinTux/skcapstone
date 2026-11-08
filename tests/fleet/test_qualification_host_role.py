"""A host is exempt from the qualification sandbox only if no dispatch path can reach it."""

import json
from pathlib import Path

import pytest

from skcapstone.fleet import production_policy, qualified_runtime, rollout_artifacts, rollout_drift
from skcapstone.fleet.paths import paths_for_home
from skcapstone.fleet.production_policy import can_receive_qualified_work, require_destination
from skcapstone.fleet.qualified_runtime import qualification_applies

# chiap10 and chiwk12 appear nowhere; chiap01 is a destination; chiap02 holds a quota only.
POLICY = {
    "authority_host": "chiap08",
    "node_quotas": {"chiap01": {}, "chiap02": {}, "chiap08": {}},
    "worker_destinations": ["chiap01", "chiap08"],
    "remote_review": {"enabled": True, "destinations": ["chiap01"], "card_ids": None},
}


def _policy(home: Path, body) -> None:
    root = paths_for_home(home).root
    root.mkdir(parents=True, exist_ok=True)
    (root / "production.json").write_text(body if isinstance(body, str) else json.dumps(body))


def _role(home: Path, node: str, role: str | None) -> None:
    path = paths_for_home(home).spec_path("node", "node-" + node)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"spec": {"role": role}}))


@pytest.fixture
def as_host(monkeypatch):
    """Pin the dispatch host resolution and the SKFLEET_NODE-style name together."""

    def pin(name: str) -> None:
        monkeypatch.setattr(qualified_runtime, "_dispatch_host", lambda: name)
        monkeypatch.setattr(rollout_drift, "self_node_name", lambda: name)
        monkeypatch.setattr(rollout_artifacts, "self_node_name", lambda: name)

    return pin


@pytest.mark.parametrize("host", ["chiap01", "chiap02", "chiap08", "Chiap01", "node-chiap02"])
def test_any_placement_list_or_loose_spelling_means_can_receive(host):
    assert can_receive_qualified_work(POLICY, host)


@pytest.mark.parametrize("host", ["chiap10", "chiwk12"])
def test_hosts_absent_everywhere_cannot_receive(host):
    assert not can_receive_qualified_work(POLICY, host)


def test_case_variant_quota_key_is_not_exempt():
    policy = {"node_quotas": {"Chiap01": {}}}
    assert can_receive_qualified_work(policy, "chiap01")
    with pytest.raises(ValueError):  # exact-match dispatch refuses it, the gate still checks
        require_destination(policy, "chiap01")


def test_review_destination_and_authority_host_count():
    assert can_receive_qualified_work(
        {"remote_review": {"destinations": ["chiap09"]}, "node_quotas": {}}, "chiap09"
    )
    assert can_receive_qualified_work({"authority_host": "chiap09"}, "chiap09")


@pytest.mark.parametrize(
    "policy", [None, [], "x", {"node_quotas": []}, {"worker_destinations": 3}]
)
def test_malformed_policy_fails_closed(policy):
    assert can_receive_qualified_work(policy, "chiap10")


def test_every_host_dispatch_accepts_is_covered():
    """The exemption can never drift narrower than the dispatch checks it mirrors."""
    for host in ("chiap01", "chiap02", "chiap08", "chiap09", "ziowk01"):
        policy = {**POLICY, "node_quotas": {**POLICY["node_quotas"], "ziowk01": {}}}
        try:
            require_destination(policy, host)
        except ValueError:
            continue
        assert can_receive_qualified_work(policy, host)
    assert production_policy.can_receive_qualified_work is can_receive_qualified_work


def test_worker_gpu_destination_without_role_is_not_exempt(tmp_path, as_host):
    _policy(tmp_path, POLICY)
    _role(tmp_path, "chiap01", "worker-gpu")
    as_host("chiap01")
    assert qualification_applies(tmp_path, "chiap01")


def test_destination_with_no_admitted_spec_is_not_exempt(tmp_path, as_host):
    _policy(tmp_path, POLICY)
    as_host("chiap01")
    assert qualification_applies(tmp_path, "chiap01")


def test_case_variant_quota_key_is_not_exempt_for_the_gate(tmp_path, as_host):
    _policy(tmp_path, {"node_quotas": {"Chiap01": {}}})
    as_host("chiap01")
    assert qualification_applies(tmp_path, "chiap01")


@pytest.mark.parametrize("host", ["chiap10", "chiwk12"])
def test_hosts_absent_everywhere_are_exempt(tmp_path, as_host, host):
    _policy(tmp_path, POLICY)
    as_host(host)
    assert not qualification_applies(tmp_path, host)
    _role(tmp_path, host, "observer")
    assert not qualification_applies(tmp_path, host)


def test_qualifying_role_requires_the_sandbox_even_if_unplaced(tmp_path, as_host):
    _policy(tmp_path, POLICY)
    _role(tmp_path, "chiwk12", "builder-standby")
    as_host("chiwk12")
    assert qualification_applies(tmp_path, "chiwk12")


@pytest.mark.parametrize("body", ["{}", "not json", "[]", '{"node_quotas": []}'])
def test_unparseable_or_unrecognisable_policy_fails_closed(tmp_path, as_host, body):
    _policy(tmp_path, body)
    as_host("chiap10")
    assert qualification_applies(tmp_path, "chiap10")


def test_no_production_policy_leaves_only_role(tmp_path, as_host):
    as_host("chiap10")
    assert not qualification_applies(tmp_path, "chiap10")
    _role(tmp_path, "chiap10", "control")
    assert qualification_applies(tmp_path, "chiap10")


def test_spec_store_failure_fails_closed(tmp_path, monkeypatch, as_host):
    from skcapstone.fleet import store

    _policy(tmp_path, POLICY)
    as_host("chiap10")

    def boom(*args, **kwargs):
        raise OSError("unreadable")

    monkeypatch.setattr(store, "read_spec", boom)
    assert qualification_applies(tmp_path, "chiap10")


def test_sandbox_profile_install_skips_exempt_and_runs_for_destination(
    tmp_path, monkeypatch, as_host
):
    repo = tmp_path / "repo"
    (repo / "systemd/apparmor").mkdir(parents=True)
    (repo / "systemd/apparmor/skfleet-bwrap").write_text("profile\n")
    target = tmp_path / "etc/skfleet-bwrap"
    target.parent.mkdir()
    monkeypatch.setattr(rollout_artifacts, "SANDBOX_PROFILE_TARGET", target)
    home = tmp_path / "home"
    _policy(home, POLICY)
    calls = []
    monkeypatch.setattr(rollout_artifacts.subprocess, "run", lambda args, **kw: calls.append(args))
    as_host("chiwk12")
    rollout_artifacts.install_sandbox_profile(repo, home)
    assert calls == []
    as_host("chiap01")
    rollout_artifacts.install_sandbox_profile(repo, home)
    assert len(calls) == 2
    assert calls[0][2] == "/usr/bin/install"
    assert calls[1][2] == "/usr/sbin/apparmor_parser"


def _graded(home, monkeypatch):
    seen = []
    monkeypatch.setattr(
        rollout_drift, "missing_dependencies", lambda prefix: seen.append(prefix) or []
    )
    monkeypatch.setattr(rollout_drift, "_script_files", lambda _: [])
    monkeypatch.setattr(
        rollout_drift,
        "_load_readiness_module",
        lambda _: type(
            "R",
            (),
            {
                "unit_in_scope": staticmethod(lambda _: (False, None, "fixture")),
                "_systemctl_show_value": staticmethod(lambda *a: ("inactive", None)),
            },
        ),
    )
    rollout_drift.detect_drift({"units": []}, home, home)
    return seen


def test_drift_grades_a_destination_and_skips_an_unplaced_host(tmp_path, monkeypatch, as_host):
    home = tmp_path / "home"
    _policy(home, POLICY)
    as_host("chiwk12")
    assert _graded(home, monkeypatch) == []
    as_host("chiap01")
    assert len(_graded(home, monkeypatch)) == 1
