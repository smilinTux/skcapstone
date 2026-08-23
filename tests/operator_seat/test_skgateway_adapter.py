"""skgateway adapter: conformant to the contract, health mapped correctly."""

from __future__ import annotations

from skcapstone.operator_seat import adapter
from skcapstone.operator_seat import skgateway_adapter as ad


def test_skgateway_explain_is_contract_conformant():
    assert adapter.validate_explain(ad.skgateway_explain()) == []


def test_skgateway_observe_is_contract_conformant():
    obs = ad.observe()
    assert adapter.validate_observe(obs) == []


def test_skgateway_healthy_all_true():
    obs = ad.skgateway_observe(
        probe=lambda: {
            k: True
            for k in ("upstream_serving", "pool_healthy", "scheduler_alive", "gtd_draining")
        }
    )
    assert all(c["status"] == "True" for c in obs["conditions"])


def test_skgateway_default_probe_failure_is_unknown(monkeypatch):
    def _boom(*a, **k):
        raise OSError("down")

    monkeypatch.setattr("subprocess.run", _boom, raising=False)
    monkeypatch.setattr("urllib.request.urlopen", _boom, raising=False)
    st = ad._default_probe()
    assert st["_probe_error"] == "OSError"
    assert all(
        item["status"] == "Unknown" for item in ad.skgateway_observe(lambda: st)["conditions"]
    )
