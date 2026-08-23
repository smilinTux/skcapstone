"""skmemory adapter: conformant to the contract, health mapped correctly."""

from __future__ import annotations

from skcapstone.operator_seat import adapter, skmemory_adapter


def test_skmemory_explain_is_contract_conformant():
    assert adapter.validate_explain(skmemory_adapter.skmemory_explain()) == []


def test_skmemory_observe_is_contract_conformant():
    obs = skmemory_adapter.skmemory_observe(
        probe=lambda: {"embed_serving": True, "reconcile_fresh": True}
    )
    assert adapter.validate_observe(obs) == []


def test_healthy_skmemory_is_all_true():
    obs = skmemory_adapter.skmemory_observe(
        probe=lambda: {"embed_serving": True, "reconcile_fresh": True}
    )
    by_type = {c["type"]: c["status"] for c in obs["conditions"]}
    assert by_type["EmbedServing"] == "True"
    assert by_type["ReconcileFresh"] == "True"


def test_unhealthy_embed_serving_fires():
    obs = skmemory_adapter.skmemory_observe(
        probe=lambda: {"embed_serving": False, "reconcile_fresh": True}
    )
    by_type = {c["type"]: c["status"] for c in obs["conditions"]}
    assert by_type["EmbedServing"] == "False"  # health condition fires when False
    assert by_type["ReconcileFresh"] == "True"


def test_stale_reconcile_fires():
    obs = skmemory_adapter.skmemory_observe(
        probe=lambda: {"embed_serving": True, "reconcile_fresh": False}
    )
    by_type = {c["type"]: c["status"] for c in obs["conditions"]}
    assert by_type["EmbedServing"] == "True"
    assert by_type["ReconcileFresh"] == "False"  # health condition fires when False


def test_default_probe_failure_is_unknown(monkeypatch):
    # An unreachable skmemory must not raise or false-alarm.
    def _boom(*a, **k):
        raise OSError("skmemory down")

    monkeypatch.setattr("subprocess.run", _boom)
    st = skmemory_adapter._default_probe()
    assert st["_probe_error"] == "OSError"
    assert all(
        item["status"] == "Unknown"
        for item in skmemory_adapter.skmemory_observe(lambda: st)["conditions"]
    )
