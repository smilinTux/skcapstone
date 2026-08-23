"""Manifest-driven Atlas discovery (OPS0.3, the load-bearing G1 fix).

These lock the additive, fail-safe, byte-compatible contract:

  * the master flag OFF makes discovery a no-op (byte-identical to today);
  * a verified, signed, non-built-in manifest unions with the built-ins in
    ``register_all`` and gets a subprocess observe adapter in the loop;
  * an unsigned / unverified manifest is NEVER loaded (skipped);
  * capauth/registry unavailable fails CLOSED (discover nothing);
  * the out-of-process observe boundary fails safe to Unknown on ANY error;
  * a manifest whose id matches a built-in never overrides it;
  * no auto-ratification path is introduced (the human still holds the lever);
  * the knowledge facet probes for its retriever and notes RAG availability.

Signature verification and the subprocess boundary are injected (``verified_ids_fn``,
``runner``) so the fast tests never touch gpg or spawn a process; a few explicit
real-boundary tests exercise ``_run_operator_json`` against actual subprocesses.
"""

from __future__ import annotations

import json
import sys

import pytest

from skcapstone.fleet import store
from skcapstone.fleet.operatorapp_controller import operatorapp_rows
from skcapstone.fleet.paths import FleetPaths
from skcapstone.operator_seat import discovery, registration

# --- fixtures ----------------------------------------------------------------


def _paths(tmp_path):
    return FleetPaths(root=tmp_path / "fleet")


def _seat():
    return store.Writer(role="operator", node="node-41", identity="operator", agent_seat=True)


def _human():
    return store.Writer(role="operator", node="cli", identity="chef", agent_seat=False)


def _skbrain_manifest() -> dict:
    """A signed v1.2 skbrain-shaped manifest (operator + knowledge facets)."""
    return {
        "schema_version": "1.2",
        "id": "skbrain",
        "grade": "B",
        "signature": {"alg": "capauth", "value": "<detached-sig-lives-in-.sig>"},
        "operator": {
            "contractVersion": 1,
            "cli": "skbrain operator",
            "repos": ["skos", "skcapstone", "skbrain-ops", "skmemory"],
            "conditions": ["OpsSchemaPresent", "ProjectorFresh", "KedbCanonCovered"],
            "proposedStandardActions": ["run-skbrain-sync", "run-cmdb-reconcile"],
        },
        "knowledge": {
            "namespace": "ops",
            "search_fn": "ops.hybrid_search_ops",
            "graph": "ops_brain",
            "kinds": ["runbook", "known-error"],
            "kedb": True,
            "retriever": "skos.skbrain.read_api:build_retriever",
        },
    }


def _write_manifest(home, manifest: dict, name: str = "skbrain") -> None:
    modules_dir = home / "shell" / "modules"
    modules_dir.mkdir(parents=True, exist_ok=True)
    (modules_dir / f"{name}.skworld-module.json").write_text(json.dumps(manifest))


def _healthy_skbrain(cli, verb, **kwargs):
    """A complete healthy SKBrain observation for discovery-gate tests."""
    assert verb == "observe"
    return {
        "conditions": [
            {"type": "OpsSchemaPresent", "status": "True"},
            {"type": "ProjectorFresh", "status": "True"},
            {"type": "KedbCanonCovered", "status": "True"},
        ]
    }


@pytest.fixture
def on(monkeypatch):
    """Turn the master discovery flag ON for a test."""
    monkeypatch.setenv(discovery.DISCOVERY_ENV, "1")


# --- flag gating: OFF is byte-identical --------------------------------------


def test_flag_off_discovers_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv(discovery.DISCOVERY_ENV, raising=False)
    _write_manifest(tmp_path, _skbrain_manifest())
    # Even with a perfectly good, "verified" manifest present, OFF yields nothing.
    apps = discovery.discover_apps(
        home=tmp_path, verified_ids_fn=lambda h: {"skbrain"}, builtin_ids=frozenset()
    )
    assert apps == []
    assert discovery.discover_operatorapp_specs(home=tmp_path) == []
    assert discovery.discover_observers(home=tmp_path) == {}


def test_flag_truthy_variants(monkeypatch):
    for val in ("1", "true", "YES", "On"):
        monkeypatch.setenv(discovery.DISCOVERY_ENV, val)
        assert discovery.discovery_enabled() is True
    for val in ("0", "", "off", "no"):
        monkeypatch.setenv(discovery.DISCOVERY_ENV, val)
        assert discovery.discovery_enabled() is False


# --- the verified happy path -------------------------------------------------


def test_verified_manifest_is_discovered(tmp_path, on):
    _write_manifest(tmp_path, _skbrain_manifest())
    apps = discovery.discover_apps(
        home=tmp_path,
        builtin_ids=frozenset({"skchat", "skos"}),
        verified_ids_fn=lambda h: {"skbrain"},
        runner=_healthy_skbrain,
    )
    assert [a.name for a in apps] == ["skbrain"]
    app = apps[0]
    assert app.cli == "skbrain operator"
    assert app.spec["name"] == "skbrain"
    assert app.spec["proposedStandardActions"] == ["run-skbrain-sync", "run-cmdb-reconcile"]
    # A discovered spec never carries ratifications: that lever is human-only.
    assert app.spec["ratifiedStandardActions"] == []


def test_discovery_unions_with_builtins_in_register_all(tmp_path, on):
    _write_manifest(tmp_path, _skbrain_manifest())
    paths = _paths(tmp_path)
    specs = discovery.discover_operatorapp_specs(
        home=tmp_path,
        verified_ids_fn=lambda h: {"skbrain"},
        runner=_healthy_skbrain,
    )
    written = registration.register_all(paths, writer=_seat(), discovered=specs)
    # The seven built-ins stay exactly as-is; skbrain is registered ALONGSIDE.
    assert written == [
        "cmdb",
        "skbrain",
        "skchat",
        "skcode",
        "skcomms",
        "skdashboard",
        "skgateway",
        "skmemory",
        "skos",
    ]
    rows = {r.name: r for r in operatorapp_rows(paths, "2026-07-31T00:00:00Z")}
    assert "skbrain" in rows
    assert rows["skbrain"].cli == "skbrain operator"
    # Built-ins untouched by the union.
    assert rows["skchat"].cli == "skchat operator"


# --- the signature trust bar -------------------------------------------------


def test_unverified_manifest_is_skipped(tmp_path, on):
    _write_manifest(tmp_path, _skbrain_manifest())
    # The registry does NOT mark skbrain verified: it must never be loaded.
    apps = discovery.discover_apps(
        home=tmp_path,
        builtin_ids=frozenset(),
        verified_ids_fn=lambda h: set(),  # nothing verified
        runner=_healthy_skbrain,
    )
    assert apps == []


def test_unsigned_manifest_never_registers(tmp_path, on):
    # An UNSIGNED manifest (absent from the verified set) yields no Operatorapp.
    _write_manifest(tmp_path, _skbrain_manifest())
    paths = _paths(tmp_path)
    specs = discovery.discover_operatorapp_specs(home=tmp_path, verified_ids_fn=lambda h: set())
    written = registration.register_all(paths, writer=_seat(), discovered=specs)
    assert "skbrain" not in written
    assert store.read_spec(paths, "operatorapp", "skbrain") is None


def test_capauth_unavailable_fails_closed(tmp_path, on):
    _write_manifest(tmp_path, _skbrain_manifest())
    # verified_ids_fn returning None models capauth/registry unavailable: fail closed.
    apps = discovery.discover_apps(
        home=tmp_path, builtin_ids=frozenset(), verified_ids_fn=lambda h: None
    )
    assert apps == []


# --- built-in precedence -----------------------------------------------------


def test_manifest_matching_a_builtin_does_not_override(tmp_path, on):
    m = _skbrain_manifest()
    m["id"] = "skchat"  # collides with a built-in
    m["operator"]["cli"] = "evil operator"
    _write_manifest(tmp_path, m, name="skchat")
    apps = discovery.discover_apps(
        home=tmp_path,
        builtin_ids=frozenset({"skchat"}),
        verified_ids_fn=lambda h: {"skchat"},  # even if "verified"
        runner=lambda *a, **k: None,
    )
    assert apps == []  # the in-process built-in keeps precedence


def test_register_all_skips_discovered_id_matching_builtin(tmp_path):
    paths = _paths(tmp_path)
    # A hand-built discovered spec that collides with a built-in must not override.
    from skcapstone.fleet.operatorapp import normalize_operatorapp_spec

    evil = normalize_operatorapp_spec({"name": "skchat", "cli": "evil operator"})
    registration.register_all(paths, writer=_seat(), discovered=[evil])
    row = {r.name: r for r in operatorapp_rows(paths, "2026-07-31T00:00:00Z")}["skchat"]
    assert row.cli == "skchat operator"  # the built-in, not "evil operator"


# --- the out-of-process observe boundary -------------------------------------


def test_observe_uses_a_valid_subprocess_payload():
    payload = {
        "conditions": [{"type": "OpsSchemaPresent", "status": "True"}],
    }
    observe = discovery.make_subprocess_observe(
        "skbrain operator", ["OpsSchemaPresent"], runner=lambda *a, **k: payload
    )
    out = observe(None, "2026-07-31T00:00:00Z")
    assert out["conditions"] == [{"type": "OpsSchemaPresent", "status": "True"}]


def test_observe_failure_yields_unknown():
    observe = discovery.make_subprocess_observe(
        "skbrain operator",
        ["OpsSchemaPresent", "ProjectorFresh"],
        runner=lambda *a, **k: None,  # models a nonzero exit / timeout / bad JSON
    )
    out = observe(None, "2026-07-31T00:00:00Z")
    assert [c["status"] for c in out["conditions"]] == ["Unknown", "Unknown"]
    assert {c["type"] for c in out["conditions"]} == {"OpsSchemaPresent", "ProjectorFresh"}


def test_observe_malformed_payload_yields_unknown():
    # A dict that fails the observe contract (status not in the allowed set).
    bad = {"conditions": [{"type": "OpsSchemaPresent", "status": "banana"}]}
    observe = discovery.make_subprocess_observe(
        "skbrain operator", ["OpsSchemaPresent"], runner=lambda *a, **k: bad
    )
    out = observe(None, "2026-07-31T00:00:00Z")
    assert out["conditions"][0]["status"] == "Unknown"


def test_discovered_app_observe_never_raises_on_timeout(tmp_path, on):
    # A real hung binary hits the hard timeout and fails safe to Unknown.
    hung = f'{sys.executable} -c "import time;time.sleep(5)" operator'
    m = _skbrain_manifest()
    m["operator"]["cli"] = hung
    _write_manifest(tmp_path, m)
    apps = discovery.discover_apps(
        home=tmp_path,
        builtin_ids=frozenset(),
        verified_ids_fn=lambda h: {"skbrain"},
        timeout=0.1,
    )
    assert apps == []


def test_unhealthy_skbrain_is_not_exposed(tmp_path, on):
    _write_manifest(tmp_path, _skbrain_manifest())
    apps = discovery.discover_apps(
        home=tmp_path,
        builtin_ids=frozenset(),
        verified_ids_fn=lambda h: {"skbrain"},
        runner=lambda *a, **k: None,
    )
    assert apps == []


def test_partial_skbrain_health_is_not_exposed(tmp_path, on):
    _write_manifest(tmp_path, _skbrain_manifest())
    apps = discovery.discover_apps(
        home=tmp_path,
        builtin_ids=frozenset(),
        verified_ids_fn=lambda h: {"skbrain"},
        runner=lambda *a, **k: {"conditions": [{"type": "OpsSchemaPresent", "status": "True"}]},
    )
    assert apps == []


# --- the real subprocess boundary (_run_operator_json) -----------------------


def test_run_operator_json_success():
    cli = f"{sys.executable} -c \"import json;print(json.dumps({{'conditions':[]}}))\""
    assert discovery._run_operator_json(cli, "observe", timeout=5.0) == {"conditions": []}


def test_run_operator_json_nonzero_exit_is_none():
    cli = f'{sys.executable} -c "import sys;sys.exit(3)"'
    assert discovery._run_operator_json(cli, "observe", timeout=5.0) is None


def test_run_operator_json_non_json_is_none():
    cli = f"{sys.executable} -c \"print('not json')\""
    assert discovery._run_operator_json(cli, "observe", timeout=5.0) is None


def test_run_operator_json_non_object_is_none():
    cli = f'{sys.executable} -c "import json;print(json.dumps([1,2,3]))"'
    assert discovery._run_operator_json(cli, "observe", timeout=5.0) is None


def test_run_operator_json_missing_binary_is_none():
    cli = "definitely-not-a-real-binary-xyz operator"
    assert discovery._run_operator_json(cli, "observe", timeout=5.0) is None


def test_run_operator_json_timeout_is_none():
    cli = f'{sys.executable} -c "import time;time.sleep(5)"'
    assert discovery._run_operator_json(cli, "observe", timeout=0.1) is None


# --- no auto-ratification path -----------------------------------------------


def test_discovered_app_is_not_auto_ratified(tmp_path, on):
    _write_manifest(tmp_path, _skbrain_manifest())
    paths = _paths(tmp_path)
    specs = discovery.discover_operatorapp_specs(
        home=tmp_path, verified_ids_fn=lambda h: {"skbrain"}, runner=_healthy_skbrain
    )
    registration.register_all(paths, writer=_seat(), discovered=specs)
    row = {r.name: r for r in operatorapp_rows(paths, "2026-07-31T00:00:00Z")}["skbrain"]
    # Everything proposed, nothing ratified: discovery only widens PROPOSAL surface.
    assert row.proposed_count == 2
    assert row.ratified_count == 0
    assert row.proposals_ratified is False


def test_seat_cannot_ratify_a_discovered_app(tmp_path, on):
    _write_manifest(tmp_path, _skbrain_manifest())
    paths = _paths(tmp_path)
    specs = discovery.discover_operatorapp_specs(
        home=tmp_path, verified_ids_fn=lambda h: {"skbrain"}, runner=_healthy_skbrain
    )
    registration.register_all(paths, writer=_seat(), discovered=specs)
    # The store's human-only guard blocks the seat from ratifying, discovered or not.
    with pytest.raises(store.OwnershipError):
        registration.ratify(paths, "skbrain", "run-skbrain-sync", writer=_seat())


def test_human_can_ratify_a_discovered_app(tmp_path, on):
    _write_manifest(tmp_path, _skbrain_manifest())
    paths = _paths(tmp_path)
    specs = discovery.discover_operatorapp_specs(
        home=tmp_path, verified_ids_fn=lambda h: {"skbrain"}, runner=_healthy_skbrain
    )
    registration.register_all(paths, writer=_seat(), discovered=specs)
    registration.ratify(paths, "skbrain", "run-skbrain-sync", writer=_human())
    on_disk = store.read_spec(paths, "operatorapp", "skbrain")
    assert on_disk["spec"]["ratifiedStandardActions"] == ["run-skbrain-sync"]


def test_refresh_preserves_discovered_app_ratification(tmp_path, on):
    _write_manifest(tmp_path, _skbrain_manifest())
    paths = _paths(tmp_path)

    def _specs():
        return discovery.discover_operatorapp_specs(
            home=tmp_path, verified_ids_fn=lambda h: {"skbrain"}, runner=_healthy_skbrain
        )

    registration.register_all(paths, writer=_seat(), discovered=_specs())
    registration.ratify(paths, "skbrain", "run-skbrain-sync", writer=_human())
    # A re-discovery + refresh must not blank the human's ratification.
    registration.register_all(paths, writer=_seat(), discovered=_specs())
    on_disk = store.read_spec(paths, "operatorapp", "skbrain")
    assert on_disk["spec"]["ratifiedStandardActions"] == ["run-skbrain-sync"]


# --- malformed / edge manifests ----------------------------------------------


def test_invalid_manifest_is_skipped(tmp_path, on):
    m = _skbrain_manifest()
    del m["operator"]  # required facet missing -> validation error
    _write_manifest(tmp_path, m)
    apps = discovery.discover_apps(
        home=tmp_path, builtin_ids=frozenset(), verified_ids_fn=lambda h: {"skbrain"}
    )
    assert apps == []


def test_manifest_without_cli_is_skipped(tmp_path, on):
    m = _skbrain_manifest()
    del m["operator"]["cli"]  # cannot run out-of-process without a cli
    _write_manifest(tmp_path, m)
    apps = discovery.discover_apps(
        home=tmp_path, builtin_ids=frozenset(), verified_ids_fn=lambda h: {"skbrain"}
    )
    assert apps == []


def test_unreadable_file_is_skipped_others_survive(tmp_path, on):
    modules_dir = tmp_path / "shell" / "modules"
    modules_dir.mkdir(parents=True)
    (modules_dir / "broken.skworld-module.json").write_text("{not json")
    _write_manifest(tmp_path, _skbrain_manifest())
    apps = discovery.discover_apps(
        home=tmp_path,
        builtin_ids=frozenset(),
        verified_ids_fn=lambda h: {"skbrain"},
        runner=_healthy_skbrain,
    )
    assert [a.name for a in apps] == ["skbrain"]  # the good one still loads


def test_missing_modules_dir_is_fine(tmp_path, on):
    apps = discovery.discover_apps(
        home=tmp_path, builtin_ids=frozenset(), verified_ids_fn=lambda h: {"skbrain"}
    )
    assert apps == []


# --- the capauth signature-gate wrapper (_verified_module_ids) ---------------


def test_verified_ids_keeps_only_ok_and_enabled(tmp_path, monkeypatch):
    import capauth.manifest as cm

    entries = [
        {"id": "skbrain", "signature": "ok", "enabled": True},
        {"id": "tampered", "signature": "failed", "enabled": True},
        {"id": "nosig", "signature": "missing-sig", "enabled": True},
        {"id": "disabled", "signature": "ok", "enabled": False},
    ]
    monkeypatch.setattr(cm, "list_registered", lambda home, expected_signer=None: entries)
    assert discovery._verified_module_ids(tmp_path) == {"skbrain"}


def test_verified_ids_pins_the_signer(tmp_path, monkeypatch):
    import capauth.manifest as cm

    seen = {}

    def _list(home, expected_signer=None):
        seen["signer"] = expected_signer
        return []

    monkeypatch.setattr(cm, "list_registered", _list)
    monkeypatch.setenv(discovery.SIGNER_FPR_ENV, "DEADBEEF")
    discovery._verified_module_ids(tmp_path)
    assert seen["signer"] == "DEADBEEF"


def test_verified_ids_registry_error_fails_closed(tmp_path, monkeypatch):
    import capauth.manifest as cm

    def _boom(home, expected_signer=None):
        raise RuntimeError("corrupt registry")

    monkeypatch.setattr(cm, "list_registered", _boom)
    assert discovery._verified_module_ids(tmp_path) is None  # None => fail closed


# --- knowledge facet / RAG probe ---------------------------------------------


def test_knowledge_probe_off_mode(monkeypatch):
    monkeypatch.setenv(discovery.SKBRAIN_ENV, "off")
    from skcapstone.operator_seat.manifest_adapter import KnowledgeSource

    ks = KnowledgeSource(namespace="ops", search_fn="f", retriever="mod:fn")
    assert discovery.probe_knowledge(ks, prober=lambda k: True) is False


def test_knowledge_probe_on_mode(monkeypatch):
    monkeypatch.setenv(discovery.SKBRAIN_ENV, "on")
    from skcapstone.operator_seat.manifest_adapter import KnowledgeSource

    ks = KnowledgeSource(namespace="ops", search_fn="f")
    assert discovery.probe_knowledge(ks, prober=lambda k: False) is True  # assumed present


def test_knowledge_probe_auto_uses_prober(monkeypatch):
    monkeypatch.setenv(discovery.SKBRAIN_ENV, "auto")
    from skcapstone.operator_seat.manifest_adapter import KnowledgeSource

    ks = KnowledgeSource(namespace="ops", search_fn="f", retriever="mod:fn")
    assert discovery.probe_knowledge(ks, prober=lambda k: True) is True
    assert discovery.probe_knowledge(ks, prober=lambda k: False) is False


def test_knowledge_probe_none_facet_is_false():
    assert discovery.probe_knowledge(None) is False


def test_knowledge_probe_default_absent_retriever_is_false(monkeypatch):
    monkeypatch.setenv(discovery.SKBRAIN_ENV, "auto")
    from skcapstone.operator_seat.manifest_adapter import KnowledgeSource

    # The ops read-API does not exist yet: the default probe reads RAG-unavailable
    # WITHOUT hard-depending on it (fail-safe).
    ks = KnowledgeSource(
        namespace="ops", search_fn="f", retriever="skos.skbrain.read_api:build_retriever"
    )
    assert discovery.probe_knowledge(ks) is False


def test_discovered_app_notes_rag_availability(tmp_path, on, monkeypatch):
    monkeypatch.setenv(discovery.SKBRAIN_ENV, "on")  # force RAG present for the assert
    _write_manifest(tmp_path, _skbrain_manifest())
    apps = discovery.discover_apps(
        home=tmp_path,
        builtin_ids=frozenset(),
        verified_ids_fn=lambda h: {"skbrain"},
        runner=_healthy_skbrain,
    )
    app = apps[0]
    assert app.knowledge is not None
    assert app.knowledge.namespace == "ops"
    assert app.rag_available is True
