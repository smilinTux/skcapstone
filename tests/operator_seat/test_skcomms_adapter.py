"""skcomms adapter: conformant to the contract, health mapped correctly."""

from __future__ import annotations

from skcapstone.operator_seat import adapter, skcomms_adapter


def test_skcomms_explain_is_contract_conformant():
    assert adapter.validate_explain(skcomms_adapter.skcomms_explain()) == []


def test_skcomms_observe_is_contract_conformant():
    obs = skcomms_adapter.skcomms_observe(
        probe=lambda: {"path_healthy": True, "queue_depth": 0, "queue_limit": 1000}
    )
    assert adapter.validate_observe(obs) == []


def test_healthy_skcomms_is_all_true():
    obs = skcomms_adapter.skcomms_observe(
        probe=lambda: {"path_healthy": True, "queue_depth": 5, "queue_limit": 1000}
    )
    by_type = {c["type"]: c["status"] for c in obs["conditions"]}
    assert by_type["PathHealthy"] == "True"
    assert by_type["QueueDrained"] == "True"


def test_unhealthy_path_and_overfull_queue_fire():
    obs = skcomms_adapter.skcomms_observe(
        probe=lambda: {"path_healthy": False, "queue_depth": 5000, "queue_limit": 1000}
    )
    by_type = {c["type"]: c["status"] for c in obs["conditions"]}
    assert by_type["PathHealthy"] == "False"  # health condition fires when False
    assert by_type["QueueDrained"] == "False"  # over the bound


def test_default_probe_failure_is_unknown(monkeypatch):
    # An unreachable skcomms must not raise or false-alarm.
    import skcomms.operator_probe as scp

    def _boom(*a, **k):
        raise OSError("skcomms down")

    monkeypatch.setattr(scp, "observe", _boom)
    st = skcomms_adapter._default_probe()
    assert st["_probe_error"] == "OSError"
    assert all(
        item["status"] == "Unknown"
        for item in skcomms_adapter.skcomms_observe(lambda: st)["conditions"]
    )


def test_default_probe_delegates_to_skcomms_operator_probe(monkeypatch):
    # Card 504d0046: the seat's default probe must be ONE real signal with two
    # callers (this in-process seat, the out-of-process `skcomms operator
    # observe` cli), not a second, independently-drifting signal reader. The
    # old default probe shelled out to `skcomms daemon status` (a subcommand
    # that no longer exists) and hardcoded queue_depth=0, so it could never see
    # a real outbox backlog. Assert the delegation actually happens: flipping
    # skcomms' OWN probe output flips this adapter's output identically.
    import skcomms.operator_probe as scp

    monkeypatch.setattr(
        scp,
        "observe",
        lambda: {"conditions": [{"type": "PathHealthy", "status": "False", "object": "x"}]},
    )
    monkeypatch.setattr(scp, "queue_depth", lambda: 5000)
    st = skcomms_adapter._default_probe()
    assert st == {
        "path_healthy": False,
        "queue_depth": 5000,
        "queue_limit": skcomms_adapter._QUEUE_LIMIT,
    }
    obs = skcomms_adapter.skcomms_observe(lambda: st)
    by_type = {c["type"]: c["status"] for c in obs["conditions"]}
    assert by_type["PathHealthy"] == "False"
    assert by_type["QueueDrained"] == "False"  # 5000 > the 1000 limit
