"""Versioned Atlas observation and adapter-catalog contracts."""

from skcapstone.operator_seat import adapter, registration


def test_normalized_observation_carries_evidence_contract() -> None:
    schema = adapter.condition_schema(["Ready"], ttl_seconds=90)
    out = adapter.normalize_observe(
        "demo",
        {"conditions": [{"type": "Ready", "status": "True", "object": "svc"}]},
        schema,
        observed_at="2026-08-20T12:00:00Z",
        provenance="signed-manifest:demo",
        scope="node:41",
    )
    assert out["schema"] == "skoperator.observation/v1"
    assert out["conditions"] == [
        {
            "type": "Ready",
            "status": "True",
            "object": "svc",
            "app": "demo",
            "observed_at": "2026-08-20T12:00:00Z",
            "ttl_seconds": 90,
            "provenance": "signed-manifest:demo",
            "scope": "node:41",
            "polarity": "problem_when_false",
        }
    ]


def test_missing_or_malformed_probe_evidence_is_unknown() -> None:
    schema = adapter.condition_schema(["Ready", "Backlog"], problem_when_true={"Backlog"})
    out = adapter.normalize_observe(
        "demo", {"conditions": [{"type": "Ready", "status": "healthy"}]}, schema
    )
    assert [item["status"] for item in out["conditions"]] == ["Unknown", "Unknown"]
    assert out["conditions"][1]["polarity"] == "problem_when_true"


def test_adapter_catalog_converges_sources_and_namespaces_actions() -> None:
    registry = {
        "demo": {
            "explain": lambda: {
                "kinds": ["service"],
                "conditions": ["Ready"],
                "actions": [
                    {
                        "name": "restart",
                        "standard": True,
                        "reversible": True,
                        "blast_radius": "low",
                        "runbook": "restart it",
                        "kedb_refs": [],
                    }
                ],
            },
            "cli": "demo operator",
            "repos": ["demo"],
        }
    }
    catalog = registration.adapter_catalog(
        registry=registry,
        discovered=[{"name": "extra", "conditions": [], "proposedStandardActions": []}],
    )
    assert catalog["demo"]["actions"][0]["id"] == "demo.restart"
    assert catalog["demo"]["source"] == "builtin"
    assert catalog["extra"]["source"] == "signed-manifest"


def test_builtin_wins_catalog_collision() -> None:
    registry = {
        "demo": {
            "explain": lambda: {"kinds": [], "conditions": [], "actions": []},
            "cli": None,
            "repos": [],
        }
    }
    catalog = registration.adapter_catalog(registry=registry, discovered=[{"name": "demo"}])
    assert catalog["demo"]["source"] == "builtin"
