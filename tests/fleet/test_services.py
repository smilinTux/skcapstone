"""Tests for the Service kind model (spec 5.2): defaults, validation, workload."""

from __future__ import annotations

import pytest

from skcapstone.fleet import explain, services


def test_defaults_are_conservative() -> None:
    spec = services.normalize_service_spec({"unit": "skgateway.service"})
    assert spec["runtime"] == "systemd-user"
    assert spec["replicas"] == 1
    assert spec["nodeSelector"] == {}
    assert spec["tolerations"] == []
    assert spec["resources"] == {"cores": 1, "ram_gb": 2.0}
    assert spec["healthCheck"] is None
    assert spec["restartPolicy"] == "on-failure"
    assert spec["failover"] == "manual"  # never auto by default (R4)
    assert spec["paused"] is False
    assert spec["deleted"] is False
    assert spec["compose"] is None


def test_explicit_fields_survive() -> None:
    spec = services.normalize_service_spec(
        {
            "unit": "coturn",
            "runtime": "docker",
            "nodeSelector": {"always-on": "true"},
            "tolerations": [{"key": "dedicated"}],
            "resources": {"cores": 2, "ram_gb": 4.0},
            "healthCheck": {"port": 3478},
            "failover": "auto",
            "paused": True,
            "compose": {"file": "/opt/coturn/compose.yml", "service": "coturn"},
        }
    )
    assert spec["runtime"] == "docker"
    assert spec["healthCheck"] == {"port": 3478}
    assert spec["failover"] == "auto"
    assert spec["paused"] is True
    assert spec["compose"]["service"] == "coturn"


def test_replicas_clamped_to_one_in_v1() -> None:
    spec = services.normalize_service_spec({"unit": "u.service", "replicas": 3})
    assert spec["replicas"] == 1


def test_validation_errors() -> None:
    with pytest.raises(services.ServiceSpecError):
        services.normalize_service_spec({})  # no unit
    with pytest.raises(services.ServiceSpecError):
        services.normalize_service_spec({"unit": "u", "runtime": "kubelet"})
    with pytest.raises(services.ServiceSpecError):
        services.normalize_service_spec({"unit": "u", "failover": "instant"})
    with pytest.raises(services.ServiceSpecError):
        services.normalize_service_spec({"unit": "u", "restartPolicy": "always"})
    with pytest.raises(services.ServiceSpecError):
        services.normalize_service_spec({"unit": "u", "healthCheck": {"exec": "x"}})


def test_service_workload_mapping() -> None:
    payload = {
        "kind": "Service",
        "name": "skgateway",
        "labels": {"tier": "core"},
        "generation": 3,
        "spec": {
            "unit": "skgateway.service",
            "nodeSelector": {"always-on": "true"},
            "tolerations": [{"key": "dedicated", "value": "model-serving"}],
            "resources": {"cores": 2, "ram_gb": 1.0},
        },
    }
    wl = services.service_workload(payload)
    assert wl.kind == "service" and wl.name == "skgateway"
    assert wl.node_selector == {"always-on": "true"}
    assert wl.tolerations == ({"key": "dedicated", "value": "model-serving"},)
    assert wl.requests == {"cores": 2, "ram_gb": 1.0}


def test_explain_registers_service() -> None:
    assert "service" in explain.explain()["kinds"]
    entry = explain.explain("service")
    assert entry["kind"] == "Service"
    for field in (
        "runtime",
        "unit",
        "replicas",
        "nodeSelector",
        "tolerations",
        "resources",
        "healthCheck",
        "restartPolicy",
        "failover",
        "paused",
    ):
        assert field in entry["spec"]
    for cond in ("Ready", "Progressing", "CrashLooping", "SpecUnverified"):
        assert cond in entry["conditions"]
