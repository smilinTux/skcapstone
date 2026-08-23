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
    import skmemory.operator_probe as smp

    def _boom(*a, **k):
        raise OSError("skmemory down")

    monkeypatch.setattr(smp, "observe", _boom)
    st = skmemory_adapter._default_probe()
    assert st["_probe_error"] == "OSError"
    assert all(
        item["status"] == "Unknown"
        for item in skmemory_adapter.skmemory_observe(lambda: st)["conditions"]
    )


def test_default_probe_delegates_to_skmemory_operator_probe(monkeypatch):
    # Card 504d0046: the seat's default probe must be ONE real signal with two
    # callers (this in-process seat, the out-of-process `skmemory operator
    # observe` cli), not a second, independently-drifting signal reader. The
    # old default probe shelled out to `skmemory daemon status`, a subcommand
    # that does not exist, so it always read confidently WRONG regardless of
    # the real embed-backend/index-freshness state. Assert the delegation
    # actually happens: flipping skmemory's OWN probe output flips this
    # adapter's output identically.
    import skmemory.operator_probe as smp

    monkeypatch.setattr(
        smp,
        "observe",
        lambda: {
            "conditions": [
                {"type": "EmbedServing", "status": "False", "object": "embed-service"},
                {"type": "ReconcileFresh", "status": "True", "object": "reconciler"},
            ]
        },
    )
    st = skmemory_adapter._default_probe()
    assert st == {"embed_serving": False, "reconcile_fresh": True}
    obs = skmemory_adapter.skmemory_observe(lambda: st)
    by_type = {c["type"]: c["status"] for c in obs["conditions"]}
    assert by_type["EmbedServing"] == "False"
    assert by_type["ReconcileFresh"] == "True"
