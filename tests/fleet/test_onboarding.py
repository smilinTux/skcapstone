"""Tests for Card 3.4: onboarding docs + skmem-pg health-condition-only."""

from __future__ import annotations

import json
from pathlib import Path

from skcapstone.fleet import conditions, services, sknoded, store

DOCS = Path(__file__).resolve().parents[2] / "docs" / "fleet" / "services"
ONBOARD = ["skmemory-daemon", "ollama", "piper-tts", "nostr-relay"]
NOW = "2026-07-28T12:00:00Z"
CAP = {"cores": 4, "ram_gb": 8.0, "disk_gb": 50.0, "gpu": None, "vram_gb": None}


def test_onboarding_docs_valid_and_conservative() -> None:
    assert sorted(p.stem for p in DOCS.glob("*.json")) == sorted(ONBOARD)
    for path in DOCS.glob("*.json"):
        doc = json.loads(path.read_text())
        spec = services.normalize_service_spec(doc["spec"])
        assert spec["failover"] == "manual"


def test_ollama_targets_the_gpu_node_with_toleration() -> None:
    doc = json.loads((DOCS / "ollama.json").read_text())
    wl = services.service_workload(doc)
    assert wl.node_selector == {"gpu": "true"}
    assert {"key": "dedicated", "value": "model-serving"} in wl.tolerations


def test_skmem_pg_is_never_a_service() -> None:
    for base in (DOCS, DOCS.parent / "pilot-services"):
        for path in base.glob("*.json"):
            assert "skmem-pg" not in path.stem
            assert "skmem-pg" not in path.read_text()


def test_probe_conditions(monkeypatch) -> None:
    monkeypatch.setattr(
        conditions, "tcp_open", lambda port, host="127.0.0.1", timeout=1.0: port == 5432
    )
    conds = conditions.probe_conditions(
        [
            {"name": "skmem-pg", "port": 5432, "condition": "SkmemPgReady"},
            {"name": "dead-thing", "port": 9999, "condition": "DeadThingReady"},
        ],
        NOW,
    )
    by_type = {c["type"]: c for c in conds}
    assert by_type["SkmemPgReady"]["status"] == "True"
    assert by_type["SkmemPgReady"]["reason"] == "TcpProbe"
    assert by_type["DeadThingReady"]["status"] == "False"
    assert conditions.probe_conditions([], NOW) == []


def test_node_report_carries_probe_conditions(paths, operator, monkeypatch) -> None:
    monkeypatch.setattr("skcapstone.fleet.sknoded.node_capacity", lambda: dict(CAP))
    monkeypatch.setattr(conditions, "tcp_open", lambda port, host="127.0.0.1", timeout=1.0: True)
    store.write_spec(
        paths,
        "node",
        "node-41",
        {"healthProbes": [{"name": "skmem-pg", "port": 5432, "condition": "SkmemPgReady"}]},
        writer=operator,
    )
    sknoded.run_once(paths, "node-41")
    report = store.read_node_file(paths, "node-41", "node.json")
    by_type = {c["type"]: c for c in report["conditions"]}
    assert by_type["SkmemPgReady"]["status"] == "True"


def test_no_probes_means_no_extra_conditions(paths, monkeypatch) -> None:
    monkeypatch.setattr("skcapstone.fleet.sknoded.node_capacity", lambda: dict(CAP))
    sknoded.run_once(paths, "node-41")  # unadmitted: no node spec
    report = store.read_node_file(paths, "node-41", "node.json")
    types = {c["type"] for c in report["conditions"]}
    assert "SkmemPgReady" not in types


def test_tcp_open_false_on_closed_port() -> None:
    # A refused or unreachable connection must degrade to False (except branch).
    assert conditions.tcp_open(65533, host="127.0.0.1", timeout=0.2) is False


def test_probe_conditions_skips_malformed() -> None:
    # Malformed probes are skipped, never raised (KeyError/ValueError fallback).
    assert conditions.probe_conditions([{"condition": "X"}], NOW) == []
    assert conditions.probe_conditions([{"port": "nope", "condition": "X"}], NOW) == []
    out = conditions.probe_conditions(
        [{"port": 65533, "condition": "SkmemPg", "name": "skmem-pg"}], NOW
    )
    assert len(out) == 1 and out[0]["type"] == "SkmemPg"
