"""CMDB adapter CONDITIONS table (card ff97f567).

Before this card ``cmdb_adapter`` listed its conditions inline in ``cmdb_explain``
and had no module-level ``CONDITIONS``. ``loop.CONDITION_SCHEMAS`` is built from
that attribute, so the cmdb schema was empty, and ``adapter.normalize_observe``
emits only the conditions a schema declares: the loop never saw a cmdb condition.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from skcapstone.fleet import store
from skcapstone.fleet.paths import FleetPaths
from skcapstone.operator_seat import adapter, cmdb_adapter, loop, registration

NOW = "2026-11-14T12:00:00Z"
EXPECTED = ["CmdbReconcileFresh", "CmdbLastScanComplete", "CmdbAuditClean"]


def _clock():
    base = datetime.fromisoformat(NOW.replace("Z", "+00:00"))
    return lambda: base + timedelta(seconds=10)


def _paths(tmp_path, monkeypatch):
    monkeypatch.setenv("SKFLEET_ROOT", str(tmp_path / "fleet"))
    monkeypatch.setenv("SKFLEET_NODE", "node-test")
    paths = FleetPaths(root=tmp_path / "fleet")
    chef = store.Writer(role="operator", node="node-test", identity="chef")
    store.set_frozen(paths, False, writer=chef, reason="test provisioning")
    return paths, chef


def _register_cmdb(paths, chef, ratified):
    spec = registration.derive_operatorapp_spec("cmdb", cmdb_adapter.cmdb_explain())
    spec["ratifiedStandardActions"] = list(ratified)
    store.write_spec(paths, "operatorapp", "cmdb", spec, writer=chef)


def _observe(status="False"):
    def observe(paths=None, now_iso=None):
        return {
            "source_observed_at": "2026-11-14T11:59:30Z",
            "conditions": [{"type": "CmdbReconcileFresh", "status": status, "object": "cmdb"}],
        }

    return observe


def _proposal(condition="CmdbReconcileFresh", action="run-cmdb-shadow"):
    return {"app": "cmdb", "condition": condition, "action": action, "object": "cmdb"}


def _run(paths, proposal, monkeypatch, **kwargs):
    monkeypatch.setitem(loop.ADAPTERS, "cmdb", _observe())
    return loop.run_once(
        paths,
        now_iso=NOW,
        clock=_clock(),
        propose=lambda brief, route: [proposal],
        explain=cmdb_adapter.cmdb_explain(),
        require_verified_actions=True,
        emit=lambda _s: None,
        **kwargs,
    )


def test_conditions_table_is_exported_and_is_what_explain_declares():
    assert cmdb_adapter.CONDITIONS == EXPECTED
    assert "CONDITIONS" in cmdb_adapter.__all__
    assert cmdb_adapter.cmdb_explain()["conditions"] == EXPECTED
    assert adapter.validate_explain(cmdb_adapter.cmdb_explain()) == []


def test_loop_schema_for_cmdb_is_non_empty_and_health_polarity():
    schema = loop.CONDITION_SCHEMAS["cmdb"]
    assert list(schema) == EXPECTED
    assert {v["polarity"] for v in schema.values()} == {"problem_when_false"}
    assert cmdb_adapter.PROBLEM_WHEN_TRUE == frozenset()


def test_loop_brief_sees_a_firing_cmdb_condition(tmp_path, monkeypatch):
    """With the empty schema the observation was dropped and the brief had no cmdb row."""
    paths, _ = _paths(tmp_path, monkeypatch)
    res = _run(paths, _proposal(), monkeypatch)
    firing = [c for c in res["brief"]["firing"] if c["app"] == "cmdb"]
    assert [c["type"] for c in firing] == ["CmdbReconcileFresh"]
    # The conditions the stub did not report are Unknown (stale), not dropped.
    stale = {c["type"] for c in res["brief"]["stale"] if c["app"] == "cmdb"}
    assert stale == {"CmdbLastScanComplete", "CmdbAuditClean"}


def test_every_action_declares_its_eligible_conditions():
    names = {a["name"] for a in cmdb_adapter.cmdb_explain()["actions"]}
    assert set(cmdb_adapter.ACTION_CONDITIONS) == names
    for conds in cmdb_adapter.ACTION_CONDITIONS.values():
        assert set(conds) <= set(cmdb_adapter.CONDITIONS)
    assert cmdb_adapter.ACTION_CONDITIONS["run-cmdb-shadow"] == (
        "CmdbReconcileFresh",
        "CmdbLastScanComplete",
    )
    # Non-standard, irreversible: never eligible for an autonomous lane.
    assert cmdb_adapter.ACTION_CONDITIONS["apply-cmdb-reconcile"] == ()


def test_condition_outside_the_table_is_refused_by_the_gate(tmp_path, monkeypatch):
    paths, chef = _paths(tmp_path, monkeypatch)
    _register_cmdb(paths, chef, ["run-cmdb-shadow"])
    assert loop._operatorapp_allows(paths, _proposal(condition="CmdbMadeUp")) is False
    res = _run(paths, _proposal(condition="CmdbMadeUp"), monkeypatch)
    assert res["planned"][0]["binding_denied"] is True
    assert res["planned"][0]["disposition"] == "escalate"


def test_shadow_for_firing_reconcile_fresh_passes_the_target_action_gate(tmp_path, monkeypatch):
    paths, chef = _paths(tmp_path, monkeypatch)
    _register_cmdb(paths, chef, ["run-cmdb-shadow"])
    assert loop._operatorapp_allows(paths, _proposal()) is True
    res = _run(paths, _proposal(), monkeypatch)
    planned = res["planned"][0]
    assert planned["binding_denied"] is False
    assert planned["unresolved_target"] is False
    assert planned["disposition"] == "auto"


def test_unratified_shadow_is_still_refused(tmp_path, monkeypatch):
    paths, chef = _paths(tmp_path, monkeypatch)
    _register_cmdb(paths, chef, [])
    res = _run(paths, _proposal(), monkeypatch)
    assert res["planned"][0]["binding_denied"] is True


def test_apply_is_never_auto_even_when_ratified(tmp_path, monkeypatch):
    paths, chef = _paths(tmp_path, monkeypatch)
    _register_cmdb(paths, chef, ["apply-cmdb-reconcile"])
    res = _run(paths, _proposal(action="apply-cmdb-reconcile"), monkeypatch)
    assert res["planned"][0]["disposition"] == "escalate"
