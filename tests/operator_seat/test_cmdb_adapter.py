"""ATLAS CMDB operator-facet tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from skcapstone.operator_seat import cmdb_adapter


class _CleanManager:
    def __init__(self, _home):
        pass

    def audit_relationships(self):
        return []


def _artifact(
    home: Path,
    *,
    name="run-1",
    complete: bool = True,
    ended_at="2026-08-20T19:00:00Z",
    scope="scope-1",
):
    directory = home / "cmdb" / "reconcile-runs"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    payload = json.dumps(
        {
            "scan_id": name,
            "ended_at": ended_at,
            "scope_fingerprint": scope,
            "completeness": {"complete": complete},
        },
        sort_keys=True,
    ).encode()
    path.write_bytes(payload)
    path.with_suffix(".sha256").write_text(hashlib.sha256(payload).hexdigest())


def test_explain_keeps_apply_nonstandard_and_irreversible():
    actions = {item["name"]: item for item in cmdb_adapter.cmdb_explain()["actions"]}
    assert actions["run-cmdb-shadow"]["standard"] is True
    assert actions["apply-cmdb-reconcile"]["standard"] is False
    assert actions["apply-cmdb-reconcile"]["reversible"] is False


def test_observe_uses_only_verified_complete_fresh_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    home = tmp_path / ".skcapstone"
    _artifact(home)
    result = cmdb_adapter.observe(
        now_iso="2026-08-20T20:00:00Z", manager_factory=_CleanManager
    )
    statuses = {item["type"]: item["status"] for item in result["conditions"]}
    assert statuses == {
        "CmdbReconcileFresh": "True",
        "CmdbLastScanComplete": "True",
        "CmdbAuditClean": "True",
    }


def test_observe_rejects_bad_checksum_and_reports_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    home = tmp_path / ".skcapstone"
    _artifact(home)
    (home / "cmdb" / "reconcile-runs" / "run-1.sha256").write_text("0" * 64)
    result = cmdb_adapter.observe(manager_factory=_CleanManager)
    statuses = {item["type"]: item["status"] for item in result["conditions"]}
    assert statuses["CmdbReconcileFresh"] == "Unknown"
    assert statuses["CmdbLastScanComplete"] == "Unknown"


def test_act_freeze_wins(tmp_path):
    from skcapstone.fleet.paths import FleetPaths
    from skcapstone.fleet.store import Writer, set_frozen

    paths = FleetPaths(tmp_path)
    set_frozen(
        paths,
        True,
        writer=Writer(role="operator", node="cli", identity="chef"),
        reason="test",
    )
    called = []
    result = cmdb_adapter.cmdb_act(paths, "run-cmdb-shadow", runner=called.append)
    assert result == {"performed": False, "reason": "frozen", "action": "run-cmdb-shadow"}
    assert called == []


def test_shadow_act_starts_only_the_shadow_oneshot(tmp_path):
    from skcapstone.fleet.paths import FleetPaths

    calls = []

    def runner(argv):
        calls.append(argv)
        return True

    result = cmdb_adapter.cmdb_act(
        FleetPaths(tmp_path), "run-cmdb-shadow", runner=runner
    )
    assert result["performed"] is True
    assert calls == [
        ["systemctl", "--user", "start", "skcapstone-cmdb-reconcile-shadow.service"]
    ]


class _Value:
    def __init__(self, value):
        self.value = value


class _Change:
    id = "chg-approved"
    status = _Value("approved")


class _Vote:
    def __init__(
        self,
        agent="human",
        decision="approved",
        subject_role="",
        subject_fingerprint="",
        authorization_id="",
    ):
        self.agent = agent
        self.decision = _Value(decision)
        self.subject_role = subject_role
        self.subject_fingerprint = subject_fingerprint
        self.authorization_id = authorization_id


class _ITIL:
    def __init__(self, _home, *, status="approved", voter="human"):
        self.change = _Change()
        self.change.status = _Value(status)
        self.voter = voter

    def list_changes(self):
        return [self.change]

    def get_cab_votes(self, _change_id):
        return [_Vote(self.voter)]


def _three_artifacts(home, *, scopes=("scope-1",) * 3):
    for index, scope in enumerate(scopes, 1):
        _artifact(home, name=f"run-{index}", scope=scope)


def test_apply_refuses_without_change_id(tmp_path):
    called = []
    result = cmdb_adapter.cmdb_act(
        __import__("skcapstone.fleet.paths", fromlist=["FleetPaths"]).FleetPaths(tmp_path),
        "apply-cmdb-reconcile",
        runner=called.append,
    )
    assert result["performed"] is False
    assert "change-id" in result["reason"]
    assert called == []


def test_apply_requires_canonical_approval_and_human_vote(tmp_path):
    from skcapstone.fleet.paths import FleetPaths

    _three_artifacts(tmp_path)
    for factory, reason in (
        (lambda home: _ITIL(home, status="reviewing"), "not approved"),
        (lambda home: _ITIL(home, voter="lumina"), "human CAB"),
    ):
        result = cmdb_adapter.cmdb_act(
            FleetPaths(tmp_path),
            "apply-cmdb-reconcile",
            change_id="chg-approved",
            itil_factory=factory,
            manager_factory=_CleanManager,
        )
        assert result["performed"] is False
        assert reason in result["reason"]


def test_apply_accepts_authenticated_named_owner_vote(tmp_path):
    """A signed Chef vote need not corrupt its identity into literal human."""
    from skcapstone.fleet.paths import FleetPaths

    _three_artifacts(tmp_path)

    class NamedOwnerITIL(_ITIL):
        def get_cab_votes(self, _change_id):
            return [
                _Vote(
                    "chef",
                    subject_role="owner",
                    subject_fingerprint="A" * 40,
                    authorization_id="authz-123",
                )
            ]

    result = cmdb_adapter.cmdb_act(
        FleetPaths(tmp_path),
        "apply-cmdb-reconcile",
        change_id="chg-approved",
        runner=lambda _argv: True,
        itil_factory=NamedOwnerITIL,
        manager_factory=_CleanManager,
    )
    assert result["performed"] is True


def test_apply_requires_three_distinct_same_scope_artifacts(tmp_path):
    from skcapstone.fleet.paths import FleetPaths

    _three_artifacts(tmp_path, scopes=("scope-1", "scope-1", "scope-2"))
    result = cmdb_adapter.cmdb_act(
        FleetPaths(tmp_path),
        "apply-cmdb-reconcile",
        change_id="chg-approved",
        itil_factory=_ITIL,
        manager_factory=_CleanManager,
    )
    assert result["performed"] is False
    assert "one non-empty scope" in result["reason"]


def test_apply_starts_distinct_network_unit_after_all_gates(tmp_path):
    from skcapstone.fleet.paths import FleetPaths

    _three_artifacts(tmp_path)
    calls = []
    result = cmdb_adapter.cmdb_act(
        FleetPaths(tmp_path),
        "apply-cmdb-reconcile",
        change_id="chg-approved",
        runner=lambda argv: calls.append(argv) or True,
        itil_factory=_ITIL,
        manager_factory=_CleanManager,
    )
    assert result["performed"] is True
    assert calls == [
        ["systemctl", "--user", "start", "skcapstone-cmdb-reconcile-network.service"]
    ]


def test_apply_rechecks_freeze_immediately_before_start(tmp_path):
    from skcapstone.fleet.paths import FleetPaths
    from skcapstone.fleet.store import Writer, set_frozen

    paths = FleetPaths(tmp_path)
    _three_artifacts(tmp_path)

    def freeze():
        set_frozen(
            paths,
            True,
            writer=Writer(role="operator", node="cli", identity="chef"),
            reason="race",
        )

    called = []
    result = cmdb_adapter.cmdb_act(
        paths,
        "apply-cmdb-reconcile",
        change_id="chg-approved",
        runner=called.append,
        itil_factory=_ITIL,
        manager_factory=_CleanManager,
        before_start=freeze,
    )
    assert result == {"performed": False, "reason": "frozen", "action": "apply-cmdb-reconcile"}
    assert called == []
