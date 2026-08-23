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
    def _boom(*a, **k):
        raise OSError("skcomms down")

    monkeypatch.setattr("subprocess.run", _boom)
    st = skcomms_adapter._default_probe()
    assert st["_probe_error"] == "OSError"
    assert all(
        item["status"] == "Unknown"
        for item in skcomms_adapter.skcomms_observe(lambda: st)["conditions"]
    )
