"""Unit tests for the pure manifest -> operator-seat structure builders (OPS0.2).

These lock the seam that later cards consume: OPS0.3 (seat discovery wiring) and
OPS1.x (skos install planner). Everything under test is PURE, so nothing here
touches a filesystem, DB, gpg or the network.

Coverage:
  * a full v1.2 manifest -> correct Operatorapp + InstallPlan + KnowledgeSource;
  * a v1.1 manifest (no install/knowledge) -> Operatorapp only, empty plan, no
    knowledge source;
  * malformed facets -> structured errors (and the strict builders raise);
  * parity: operatorapp_from_manifest on the 4 shipped manifests matches each
    app's existing adapter CONDITIONS (reusing the drift-guard fixtures).
"""

from __future__ import annotations

import pytest

from skcapstone.fleet.operatorapp import OperatorappSpecError
from skcapstone.operator_seat import manifest_adapter as ma
from skcapstone.operator_seat import (
    skchat_adapter,
    skcode_adapter,
    skdashboard_adapter,
    skos_adapter,
)
from skcapstone.operator_seat.registration import derive_operatorapp_spec

# --- fixtures ----------------------------------------------------------------


def _v12_manifest() -> dict:
    """A full schema v1.2 skbrain-shaped manifest (operator + install + knowledge).

    Modelled on the spec 2.3 sketch, plus a signature envelope so the signed-path
    validation has something to accept.
    """
    return {
        "schema_version": "1.2",
        "id": "skbrain",
        "grade": "B",
        "entry": {"kind": "url", "url": "http://127.0.0.1:7778/skbrain"},
        "nav": {"icon": "book", "label": "Ops Wiki", "order": 60},
        "signature": {"alg": "capauth", "value": "<opaque-not-verified-here>"},
        "operator": {
            "contractVersion": 1,
            "cli": "skbrain operator",
            "repos": ["skos", "skcapstone", "skbrain-ops", "skmemory"],
            "conditions": [
                "OpsSchemaPresent",
                "ProjectorFresh",
                "CmdbDriftBounded",
                "KedbCanonCovered",
            ],
            "proposedStandardActions": ["run-skbrain-sync", "run-cmdb-reconcile"],
        },
        "knowledge": {
            "namespace": "ops",
            "search_fn": "ops.hybrid_search_ops",
            "graph": "ops_brain",
            "kinds": ["runbook", "known-error", "postmortem"],
            "kedb": True,
            "retriever": "skos.skbrain.read_api:build_retriever",
        },
        "install": {
            "requires": {
                "capabilities": ["skmem-pg"],
                "packages": {"skcapstone": ">=0.16", "skos": ">=0.3"},
            },
            "steps": [
                {
                    "kind": "sql_migration",
                    "db": "skmem-pg",
                    "script": "skmemory:deploy/skmem-pg/03-ops-namespace.sql",
                    "pre_dump": True,
                    "verify": "skmemory:deploy/skmem-pg/verify-ops.sql",
                },
                {
                    "kind": "db_roles",
                    "logins": {
                        "skbrain_projector": "skbrain_ops_rw",
                        "skbrain_reader": "skbrain_ops_ro",
                    },
                    "password_source": "skvault",
                },
                {
                    "kind": "content_repo",
                    "name": "skbrain-ops",
                    "dest": "~/clawd/skbrain-ops",
                    "private": True,
                },
                {"kind": "seed", "cmd": ["skoperator", "kedb-seed"]},
                {"kind": "seed", "cmd": ["skbrain", "sync"], "defer_ok": True},
                {
                    "kind": "fleet_objects",
                    "objects": [
                        "cronjob/skbrain-sync.json",
                        "cronjob/skbrain-cmdb-reconcile.json",
                    ],
                },
                {
                    "kind": "doctor",
                    "checks": ["skbrain:schema", "skbrain:grants", "skbrain:kedb"],
                },
            ],
        },
    }


def _v11_manifest() -> dict:
    """A schema v1.1 manifest: operator facet only, no install/knowledge (the
    shape every shipped subapp manifest has today)."""
    return {
        "schemaVersion": "1.1",
        "id": "skdashboard",
        "name": "Board",
        "grade": "B",
        "entry": {"url": "http://127.0.0.1:7778/"},
        "operator": {
            "contractVersion": 1,
            "cli": "skcapstone dashboard operator",
            "repos": ["skcapstone"],
            "conditions": ["DashboardReady", "BoardReadable"],
            "proposedStandardActions": ["restart-dashboard"],
        },
    }


# --- full v1.2 manifest ------------------------------------------------------


def test_v12_operatorapp_spec():
    spec = ma.operatorapp_from_manifest(_v12_manifest())
    assert spec["name"] == "skbrain"
    assert spec["cli"] == "skbrain operator"
    assert spec["repos"] == ["skos", "skcapstone", "skbrain-ops", "skmemory"]
    assert spec["contractVersion"] == 1
    assert spec["conditions"] == [
        "OpsSchemaPresent",
        "ProjectorFresh",
        "CmdbDriftBounded",
        "KedbCanonCovered",
    ]
    assert spec["proposedStandardActions"] == [
        "run-skbrain-sync",
        "run-cmdb-reconcile",
    ]
    # ratification is a human-only field the seat never writes.
    assert spec["ratifiedStandardActions"] == []
    assert spec["deleted"] is False


def test_v12_operatorapp_matches_normalizer_shape():
    """operatorapp_from_manifest must produce the SAME keys as the registration
    path's derive_operatorapp_spec (both run the one normalizer)."""
    spec = ma.operatorapp_from_manifest(_v12_manifest())
    reference = derive_operatorapp_spec(
        "skbrain",
        {
            "conditions": [
                "OpsSchemaPresent",
                "ProjectorFresh",
                "CmdbDriftBounded",
                "KedbCanonCovered",
            ],
            "actions": [
                {
                    "name": "run-skbrain-sync",
                    "standard": True,
                    "reversible": True,
                },
                {
                    "name": "run-cmdb-reconcile",
                    "standard": True,
                    "reversible": True,
                },
            ],
        },
        cli="skbrain operator",
        repos=["skos", "skcapstone", "skbrain-ops", "skmemory"],
    )
    assert spec == reference


def test_v12_install_plan():
    plan = ma.install_plan_from_manifest(_v12_manifest())
    assert isinstance(plan, ma.InstallPlan)
    assert not plan.is_empty
    kinds = [s.kind for s in plan.steps]
    assert kinds == [
        "sql_migration",
        "db_roles",
        "content_repo",
        "seed",
        "seed",
        "fleet_objects",
        "doctor",
    ]
    # every kind is known
    assert all(s.kind in ma.STEP_KINDS for s in plan.steps)
    # sql_migration params preserved verbatim, minus the kind discriminator
    sql = plan.steps[0]
    assert "kind" not in sql.params
    assert sql.params["db"] == "skmem-pg"
    assert sql.params["script"] == "skmemory:deploy/skmem-pg/03-ops-namespace.sql"
    assert sql.params["pre_dump"] is True
    assert sql.params["verify"] == "skmemory:deploy/skmem-pg/verify-ops.sql"
    # the two seeds are distinct and preserve defer_ok
    assert plan.steps[3].params == {"cmd": ["skoperator", "kedb-seed"]}
    assert plan.steps[4].params == {"cmd": ["skbrain", "sync"], "defer_ok": True}
    # doctor + fleet_objects lists preserved
    assert plan.steps[5].params["objects"] == [
        "cronjob/skbrain-sync.json",
        "cronjob/skbrain-cmdb-reconcile.json",
    ]
    assert plan.steps[6].params["checks"] == [
        "skbrain:schema",
        "skbrain:grants",
        "skbrain:kedb",
    ]


def test_v12_knowledge_source():
    ks = ma.knowledge_source_from_manifest(_v12_manifest())
    assert ks == ma.KnowledgeSource(
        namespace="ops",
        search_fn="ops.hybrid_search_ops",
        graph="ops_brain",
        kinds=("runbook", "known-error", "postmortem"),
        kedb=True,
        retriever="skos.skbrain.read_api:build_retriever",
    )


def test_v12_validates_clean_signed_and_unsigned():
    manifest = _v12_manifest()
    assert ma.validate_manifest(manifest) == []
    assert ma.validate_manifest(manifest, require_signed=True) == []


def test_dataclasses_are_frozen():
    step = ma.InstallStep(kind="seed", params={"cmd": ["x"]})
    with pytest.raises(Exception):
        step.kind = "doctor"  # type: ignore[misc]
    ks = ma.knowledge_source_from_manifest(_v12_manifest())
    with pytest.raises(Exception):
        ks.namespace = "other"  # type: ignore[misc]
    plan = ma.InstallPlan(steps=())
    with pytest.raises(Exception):
        plan.steps = (step,)  # type: ignore[misc]


# --- v1.1 manifest: operator only --------------------------------------------


def test_v11_operatorapp_only():
    manifest = _v11_manifest()
    spec = ma.operatorapp_from_manifest(manifest)
    assert spec["name"] == "skdashboard"
    assert spec["conditions"] == ["DashboardReady", "BoardReadable"]
    assert spec["proposedStandardActions"] == ["restart-dashboard"]


def test_v11_no_install_plan():
    plan = ma.install_plan_from_manifest(_v11_manifest())
    assert plan == ma.InstallPlan(steps=())
    assert plan.is_empty
    assert plan.steps == ()


def test_v11_no_knowledge_source():
    assert ma.knowledge_source_from_manifest(_v11_manifest()) is None


def test_v11_validates_clean():
    assert ma.validate_manifest(_v11_manifest()) == []


def test_v11_unsigned_flagged_only_when_required():
    manifest = _v11_manifest()  # no signature envelope
    assert ma.validate_manifest(manifest) == []
    errors = ma.validate_manifest(manifest, require_signed=True)
    assert [e.facet for e in errors] == ["signature"]


# --- malformed facets: structured errors + strict-builder raises -------------


def test_non_mapping_manifest_is_a_root_error():
    errors = ma.validate_manifest(["not", "a", "mapping"])
    assert len(errors) == 1
    assert errors[0].facet == "root"


def test_missing_id_and_schema_version():
    errors = ma.validate_manifest({"operator": _v11_manifest()["operator"]})
    fields = {e.field for e in errors}
    assert "id" in fields
    assert "schemaVersion" in fields


def test_missing_operator_facet_is_error_and_raises():
    manifest = {"schemaVersion": "1.2", "id": "x"}
    errors = ma.validate_manifest(manifest)
    assert any(e.facet == "operator" for e in errors)
    with pytest.raises(ma.ManifestAdapterError):
        ma.operatorapp_from_manifest(manifest)


def test_malformed_operator_conditions():
    manifest = _v11_manifest()
    manifest["operator"]["conditions"] = ["ok", 123, ""]
    errors = ma.validate_manifest(manifest)
    assert any(e.field == "operator.conditions" for e in errors)
    # the strict builder surfaces it as an OperatorappSpecError (the normalizer).
    with pytest.raises(OperatorappSpecError):
        ma.operatorapp_from_manifest(manifest)


def test_malformed_operator_contract_version():
    manifest = _v11_manifest()
    manifest["operator"]["contractVersion"] = "1"
    errors = ma.validate_manifest(manifest)
    assert any(e.field == "operator.contractVersion" for e in errors)


def test_unknown_install_step_kind():
    manifest = _v12_manifest()
    manifest["install"]["steps"] = [{"kind": "reformat_planet"}]
    errors = ma.validate_manifest(manifest)
    assert any(e.field == "install.steps[0].kind" for e in errors)
    with pytest.raises(ma.ManifestAdapterError):
        ma.install_plan_from_manifest(manifest)


def test_install_step_missing_required_field():
    manifest = _v12_manifest()
    manifest["install"]["steps"] = [{"kind": "sql_migration", "db": "skmem-pg"}]
    errors = ma.validate_manifest(manifest)
    assert any(e.field == "install.steps[0].script" for e in errors)
    with pytest.raises(ma.ManifestAdapterError):
        ma.install_plan_from_manifest(manifest)


def test_install_steps_not_a_list():
    manifest = _v12_manifest()
    manifest["install"]["steps"] = {"kind": "seed"}
    errors = ma.validate_manifest(manifest)
    assert any(e.field == "install.steps" for e in errors)
    with pytest.raises(ma.ManifestAdapterError):
        ma.install_plan_from_manifest(manifest)


def test_install_facet_not_a_mapping():
    manifest = _v12_manifest()
    manifest["install"] = ["oops"]
    errors = ma.validate_manifest(manifest)
    assert any(e.facet == "install" for e in errors)
    with pytest.raises(ma.ManifestAdapterError):
        ma.install_plan_from_manifest(manifest)


def test_knowledge_missing_namespace_and_search_fn():
    manifest = _v12_manifest()
    manifest["knowledge"] = {"graph": "ops_brain"}
    errors = ma.validate_manifest(manifest)
    fields = {e.field for e in errors}
    assert "knowledge.namespace" in fields
    assert "knowledge.search_fn" in fields
    with pytest.raises(ma.ManifestAdapterError):
        ma.knowledge_source_from_manifest(manifest)


def test_knowledge_facet_not_a_mapping():
    manifest = _v12_manifest()
    manifest["knowledge"] = "ops"
    errors = ma.validate_manifest(manifest)
    assert any(e.facet == "knowledge" for e in errors)
    with pytest.raises(ma.ManifestAdapterError):
        ma.knowledge_source_from_manifest(manifest)


def test_validate_collects_multiple_facet_errors_at_once():
    """Validation is total: one call surfaces problems across several facets."""
    manifest = {
        "id": "",  # root
        # no schemaVersion  # root
        "operator": {"contractVersion": "nope", "conditions": [1]},  # operator x2
        "install": {"steps": [{"kind": "seed"}]},  # install (missing cmd)
        "knowledge": {"namespace": "ops"},  # knowledge (missing search_fn)
    }
    errors = ma.validate_manifest(manifest)
    facets = {e.facet for e in errors}
    assert {"root", "operator", "install", "knowledge"} <= facets


# --- parity with the 4 shipped adapters (drift-guard fixtures) ---------------

# (test id, importorskip module, builder attr, adapter module) -- same fixtures
# the manifest/adapter drift-guard uses. All four resolve when the sibling repos
# are installed; importorskip keeps a bare CI env skipping rather than erroring.
_PARITY_CASES = [
    ("skchat", "skchat.skworld_manifest", "skchat_module_manifest", skchat_adapter),
    ("skcode", "skharness.manifest", "skcode_module_manifest", skcode_adapter),
    ("skos", "skos.skworld_manifest", "skos_module_manifest", skos_adapter),
    (
        "skdashboard",
        "skcapstone.skdashboard_manifest",
        "skdashboard_module_manifest",
        skdashboard_adapter,
    ),
]


@pytest.mark.parametrize(
    "app, module_name, builder_attr, adapter",
    _PARITY_CASES,
    ids=[c[0] for c in _PARITY_CASES],
)
def test_operatorapp_from_shipped_manifest_matches_adapter(
    app, module_name, builder_attr, adapter
):
    """operatorapp_from_manifest on each shipped manifest must reproduce that
    app's adapter CONDITIONS exactly and ordered, and propose exactly its
    standard+reversible actions."""
    module = pytest.importorskip(module_name, reason=f"{module_name} (sibling repo) not installed")
    manifest = getattr(module, builder_attr)("http://x/")

    spec = ma.operatorapp_from_manifest(manifest)

    assert spec["name"] == app
    assert spec["conditions"] == adapter.CONDITIONS, (
        f"{app}: operatorapp_from_manifest conditions drifted from the adapter. "
        f"got={spec['conditions']} adapter={adapter.CONDITIONS}"
    )
    expected_actions = [
        a["name"] for a in adapter._ACTIONS if a.get("standard") and a.get("reversible")
    ]
    assert spec["proposedStandardActions"] == expected_actions
    # and it agrees with the registration path fed the same manifest operator block
    reference = derive_operatorapp_spec(
        app,
        {
            "conditions": manifest["operator"]["conditions"],
            "actions": [
                {"name": n, "standard": True, "reversible": True}
                for n in manifest["operator"]["proposedStandardActions"]
            ],
        },
        cli=manifest["operator"].get("cli"),
        repos=manifest["operator"].get("repos", []),
    )
    assert spec == reference
