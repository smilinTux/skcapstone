"""Production policy removes counts while retaining actual execution custody."""

import json
import time
from types import SimpleNamespace

import pytest

from skcapstone.fleet import builder_dispatch as builder
from skcapstone.fleet import production_builder as production
from skcapstone.fleet import store
from skcapstone.fleet.node_controller import NodeView
from tests.fleet.test_builder_dispatch import _card, _folded


@pytest.mark.parametrize("label", ["codex-only", "glm-only", "qwen-only", "deepseek-only"])
def test_production_family_exclusive_eligibility_preserves_legacy_and_host_exclusions(
    monkeypatch, label
):
    labels = ["source-only", "sk-m", label]
    monkeypatch.delenv("SKFLEET_PRODUCTION_POLICY", raising=False)
    assert builder.eligible({"id": "24b00001"}, labels) is (label == "deepseek-only")
    monkeypatch.setenv("SKFLEET_PRODUCTION_POLICY", "/reviewed/policy.json")
    assert builder.eligible({"id": "24b00001"}, labels)
    assert not builder.eligible({"id": "24b00001"}, labels + ["host-pin"])
    assert not builder.eligible({"id": "24b00001"}, labels + ["seat-seraph"])


def test_production_glm_dispatch_approved_work_is_remote_eligible(monkeypatch):
    core = {"id": "24b00001"}
    labels = ["sk-m", "glm-only", "dispatch-approved"]

    monkeypatch.delenv("SKFLEET_PRODUCTION_POLICY", raising=False)
    assert not builder.eligible(core, labels)

    monkeypatch.setenv("SKFLEET_PRODUCTION_POLICY", "/reviewed/policy.json")
    assert builder.eligible(core, labels)
    assert not builder.eligible(core, labels + ["do-not-claim"])
    assert not builder.eligible(core, labels + ["host-pin"])


def test_production_offer_selects_least_loaded_quota_normalized_node(
    paths, operator, production_setup, monkeypatch
):
    p = production_setup
    large = NodeView(
        "node-z-large",
        "Ready",
        role="builder-standby",
        labels={"host": "large"},
        capacity={"cores": 32, "ram_gb": 64},
        allocatable={"cores": 32, "ram_gb": 64},
    )
    store.write_spec(
        paths,
        "node",
        large.name,
        {"role": "builder-standby", "actuate": True, "cordoned": False},
        writer=operator,
        labels=large.labels,
    )
    p.policy["node_quotas"]["large"] = {
        "cpu_quota_percent": 400,
        "memory_max_bytes": 6 * 1024**3,
        "tasks_max": 128,
        "runtime_max_seconds": 600,
    }
    p.path.write_text(json.dumps(p.policy))
    monkeypatch.setattr(builder, "node_views", lambda _paths: [p.view, large])

    request = builder.offer(
        paths,
        _card() | {"id": "24b0007f"},
        ["sk-m", "glm-only", "dispatch-approved"],
        writer=p.writer,
    )

    assert request["node"] == "node-z-large"


@pytest.fixture
def production_setup(paths, operator, monkeypatch, tmp_path, qualified_runtime):
    value = {
        "schema": "skfleet.production/v1",
        "authority_host": "control",
        "capacity_authority": "skgateway",
        "gateway_url": "http://gateway:18790",
        "lanes": {
            name: {"enabled": True, "provider": "skgateway"}
            for name in ("codex", "glm", "deepseek", "qwen")
        },
        "node_quotas": {
            "control": {
                "cpu_quota_percent": 100,
                "memory_max_bytes": 1024**3,
                "tasks_max": 128,
                "runtime_max_seconds": 600,
            },
            "worker": {
                "cpu_quota_percent": 100,
                "memory_max_bytes": 1024**3,
                "tasks_max": 128,
                "runtime_max_seconds": 600,
            },
        },
    }
    value["lanes"]["kimi"] = {"enabled": False}
    path = tmp_path / "production.json"
    path.write_text(json.dumps(value))
    monkeypatch.setenv("SKFLEET_PRODUCTION_POLICY", str(path))
    monkeypatch.setenv("SKFLEET_AUTHORITY_HOST", "control")
    monkeypatch.setenv("SKFLEET_PI", "/test/pi")
    monkeypatch.setattr(builder, "_guard_path", lambda: "/test/guard.mjs")
    monkeypatch.setattr(production.socket, "gethostname", lambda: "control")
    domains = {"codex": "codex", "glm": "zai", "deepseek": "deepseek", "qwen": "chiap08-qwen38"}
    monkeypatch.setattr(
        production.production_routes,
        "snapshot",
        lambda value: {
            "schema_version": 1,
            "error": None,
            "observed_at": time.time(),
            "routes": [
                {
                    "logical_route": name + "-model",
                    "model_or_bucket": name + "-model",
                    "provider": domain,
                    "capacity_domain": domain,
                    "size_class": "S" if name == "qwen" else "XL",
                    "max": 10,
                    "gateway_active": 0,
                    "state": "healthy",
                    "policy_tier": "local" if name == "qwen" else "cloud",
                }
                for name, domain in domains.items()
            ],
        },
    )
    monkeypatch.setattr(
        production.production_routes,
        "preflight",
        lambda value, bound: {
            "requested_identity": bound["model"],
            "served_identity": bound["model"],
            "provider": bound["gateway_backend"],
            "observed_at": time.time(),
        },
    )
    store.write_spec(
        paths,
        "node",
        "node-worker",
        {"role": "builder-standby", "actuate": True, "cordoned": False},
        writer=operator,
        labels={"host": "worker"},
    )
    view = NodeView(
        "node-worker",
        "Ready",
        role="builder-standby",
        labels={"host": "worker"},
        capacity={"cores": 16, "ram_gb": 32},
        allocatable={"cores": 12, "ram_gb": 16},
    )
    monkeypatch.setattr(builder, "node_views", lambda _paths: [view])
    # These dispatch tests predate required profiles. Qualify the synthetic
    # contract through the real API; profile refusal has its own test matrix.
    from skcapstone.fleet import production_admission as admission
    from skcapstone.fleet import production_test_profile as profiles

    # Host capacity is independently covered by test_production_admission.
    monkeypatch.setattr(admission, "local_worker_admission", lambda *args: (True, "fixture"))

    preflight = profiles.preflight

    def qualified_preflight(home, core, labels, policy):
        path = home / "fleet/test-profiles" / (core["id"] + ".json")
        if not path.exists():
            profiles.qualify_profile(
                home,
                core,
                policy,
                {
                    "pytest": {"tests/test_fixture.py": 1},
                    "compile": [],
                    "lint": [],
                    "changelog": False,
                },
                "synthetic-test-operator",
                "b" * 64,
            )
        return preflight(home, core, labels, policy)

    monkeypatch.setattr(profiles, "preflight", qualified_preflight)
    builder._PROCESSES.clear()
    yield SimpleNamespace(
        policy=value,
        path=path,
        view=view,
        writer=store.Writer(role="scheduler", node="niobe", identity="niobe"),
    )
    builder._PROCESSES.clear()


def test_production_offers_above_old_count_cap_and_enforces_readiness(paths, production_setup):
    p = production_setup
    offers = [
        builder.offer(
            paths,
            _card() | {"id": f"24b000{number:02x}"},
            ["sk-m", "source-only"],
            writer=p.writer,
        )
        for number in range(9)
    ]
    assert all(offers)
    assert all(row["production"]["resources"]["cpu_quota_percent"] == 100 for row in offers)
    assert {row["production"]["family"] for row in offers} <= {"codex", "glm", "deepseek"}
    assert {
        production.route_binding(p.policy, f"{number:08x}", "sk-m", [])["family"]
        for number in range(64)
    } == {"codex", "glm", "deepseek"}
    p.view.phase = "NotReady"
    assert (
        builder.offer(
            paths, _card() | {"id": "24b000ff"}, ["sk-m", "source-only"], writer=p.writer
        )
        is None
    )
    p.view.phase = "Ready"
    p.view.allocatable["ram_gb"] = 0.5
    assert (
        builder.offer(
            paths, _card() | {"id": "24b000ff"}, ["sk-m", "source-only"], writer=p.writer
        )
        is None
    )


def test_missing_node_quota_never_invents_a_limit(paths, production_setup):
    p = production_setup
    p.policy["node_quotas"].pop("worker")
    p.path.write_text(json.dumps(p.policy))
    assert builder.offer(paths, _card(), ["sk-m", "source-only"], writer=p.writer) is None


@pytest.mark.parametrize("retained", ["awaiting-evidence", "expired", "unknown", "running"])
def test_production_spreads_workers_without_charging_retained_evidence(
    paths, operator, production_setup, monkeypatch, retained
):
    p = production_setup
    p.policy["node_quotas"]["idle"] = dict(p.policy["node_quotas"]["worker"])
    p.path.write_text(json.dumps(p.policy))
    idle = NodeView(
        "node-z",
        "Ready",
        role="builder-standby",
        labels={"host": "idle"},
        capacity=dict(p.view.capacity),
        allocatable=dict(p.view.allocatable),
    )
    store.write_spec(
        paths,
        "node",
        idle.name,
        {"role": "builder-standby", "actuate": True, "cordoned": False},
        writer=operator,
        labels=idle.labels,
    )
    monkeypatch.setattr(builder, "node_views", lambda _paths: [p.view, idle])
    labels = ["sk-m", "source-only", "glm-only"]
    active = builder.offer(paths, _card() | {"id": "24b00001"}, labels, writer=p.writer)
    assert active["node"] == p.view.name
    builder._write_status(paths, p.view.name, active, "running")
    historical = dict(active, card_id="24b00002", node=idle.name, request_id="e" * 64)
    if retained == "expired":
        historical["lease_expires_at"] = "2000-01-01T00:00:00Z"
    store._dump(builder.request_path(paths, idle.name, historical["card_id"]), historical)
    if retained != "expired":
        status_request = dict(historical)
        if retained == "unknown":
            status_request["request_id"] = "f" * 64
        builder._write_status(
            paths,
            idle.name,
            status_request,
            "running" if retained == "running" else "awaiting-evidence",
        )
    request_path = builder.request_path(paths, idle.name, historical["card_id"])
    original = request_path.read_bytes()
    assert builder._node_load(paths, idle.name) == 1  # Legacy ceilings are unchanged.
    fresh = builder.offer(paths, _card() | {"id": "24b00003"}, labels, writer=p.writer)
    expected = idle.name if retained in {"awaiting-evidence", "expired"} else p.view.name
    assert fresh["node"] == expected
    assert request_path.read_bytes() == original


@pytest.mark.parametrize("original_blocker", ["memory", "occupied"])
def test_expired_unclaimed_production_offer_can_move_to_an_eligible_host(
    paths, operator, production_setup, monkeypatch, original_blocker
):
    p = production_setup
    p.policy["node_quotas"]["idle"] = dict(p.policy["node_quotas"]["worker"])
    p.path.write_text(json.dumps(p.policy))
    labels = ["sk-m", "source-only", "glm-only"]
    card = _card() | {"id": "24b00004"}
    expired = builder.offer(paths, card, labels, writer=p.writer)
    expired["lease_expires_at"] = "2000-01-01T00:00:00Z"
    old_path = builder.request_path(paths, p.view.name, card["id"])
    store._dump(old_path, expired)
    builder._write_status(
        paths,
        p.view.name,
        expired,
        "blocked",
        attempt=0,
        error="unclaimed offer expired",
        claim_released=False,
    )
    old_bytes = old_path.read_bytes()
    if original_blocker == "memory":
        p.view.allocatable["ram_gb"] = 0.5
    else:
        occupied = dict(expired, card_id="24b00005", request_id="a" * 64)
        store._dump(builder.request_path(paths, p.view.name, occupied["card_id"]), occupied)
        builder._write_status(paths, p.view.name, occupied, "running")
    idle = NodeView(
        "node-z",
        "Ready",
        role="builder-standby",
        labels={"host": "idle"},
        capacity={"cores": 16, "ram_gb": 32},
        allocatable={"cores": 12, "ram_gb": 16},
    )
    store.write_spec(
        paths,
        "node",
        idle.name,
        {"role": "builder-standby", "actuate": True, "cordoned": False},
        writer=operator,
        labels=idle.labels,
    )
    monkeypatch.setattr(builder, "node_views", lambda _paths: [p.view, idle])
    fresh = builder.offer(paths, card, labels, writer=p.writer)
    assert fresh["request_id"] != expired["request_id"]
    assert fresh["node"] == idle.name
    assert old_path.read_bytes() == old_bytes


def test_local_content_requires_explicit_simple_qwen_policy(production_setup):
    p = production_setup.policy
    assert production.route_binding(p, "24b00001", "sk-s", ["local-only"])["family"] == "qwen"
    with pytest.raises(ValueError):
        production.route_binding(p, "24b00001", "sk-m", ["local-only"])


def test_production_consumer_uses_real_resource_service_and_stable_claims(
    paths, production_setup, monkeypatch, tmp_path
):
    p = production_setup
    requests = [
        builder.offer(
            paths,
            _card() | {"id": f"24b000{number:02x}"},
            ["sk-m", "source-only"],
            writer=p.writer,
        )
        for number in range(6)
    ]
    monkeypatch.setattr(production.socket, "gethostname", lambda: "worker")
    rows = {row["card_id"]: _folded(id=row["card_id"]) for row in requests}
    # The deployed claim fence requires a native core even with synthetic folds.
    from skcoord.card_store import CardCore

    for row in requests:
        builder.CardStore(tmp_path).create(
            CardCore(id=row["card_id"], title="Synthetic production claim")
        )

    def claim(_board, owner, card):
        rows[card].owner = owner
        rows[card].meta["_claim_revision"] = "a" * 32

    monkeypatch.setattr(builder.Board, "claim_task", claim)
    monkeypatch.setattr(builder.CardStore, "fold", lambda _store, card: rows[card])
    monkeypatch.setattr(builder, "startup_hello", lambda *args, **kwargs: None)
    monkeypatch.setattr(builder, "_proc_start_ticks", lambda pid: str(pid))
    launches = []

    def launch(command, workspace):
        launches.append(command)
        return SimpleNamespace(pid=100 + len(launches), poll=lambda: None)

    builder.consume_one(
        paths,
        tmp_path,
        "node-worker",
        launcher=launch,
        materializer=lambda request, workspace: workspace.mkdir(parents=True, exist_ok=True),
    )
    assert len(launches) == 6
    for command, request in zip(launches, requests):
        assert command[0] == "/usr/bin/systemd-run"
        assert "--property=UMask=0077" in command
        assert "--property=CPUQuota=100%" in command
        assert "--property=MemoryMax=1073741824" in command
        assert "--property=TasksMax=128" in command
        assert "--property=RuntimeMaxSec=600" in command
        assert command[command.index("--model") + 1] == request["production"]["model"]
        status = builder._load(builder.status_path(paths, "node-worker", request["card_id"]))
        assert status["unit"] == production.unit_name(request, 1)
        assert status["claim_revision"] == "a" * 32
        assert status["owner"].startswith("pi-" + request["production"]["family"] + "-builder-")


@pytest.mark.parametrize("change", ["quota", "host", "route"])
def test_policy_drift_refuses_before_claim(paths, production_setup, monkeypatch, tmp_path, change):
    p = production_setup
    request = builder.offer(paths, _card(), ["sk-m", "source-only"], writer=p.writer)
    if change == "quota":
        p.policy["node_quotas"]["worker"]["tasks_max"] += 1
        p.path.write_text(json.dumps(p.policy))
    elif change == "host":
        monkeypatch.setattr(production.socket, "gethostname", lambda: "other-worker")
    else:
        p.policy["lanes"][request["production"]["family"]]["enabled"] = False
        p.path.write_text(json.dumps(p.policy))
    monkeypatch.setattr(builder.Board, "claim_task", lambda *args: pytest.fail("must not claim"))
    builder.consume_one(
        paths, tmp_path, "node-worker", launcher=lambda *args: pytest.fail("must not launch")
    )
    assert (
        builder._load(builder.status_path(paths, "node-worker", request["card_id"]))["state"]
        == "blocked"
    )


@pytest.mark.parametrize("outcome", ["blocked", "awaiting-evidence"])
def test_superseded_evidence_generation_is_reconciled_before_new_offer(
    paths, production_setup, monkeypatch, tmp_path, outcome
):
    request = builder.offer(
        paths, _card(), ["sk-m", "source-only"], writer=production_setup.writer
    )
    builder._write_status(
        paths,
        "node-worker",
        {"request_id": "f" * 64, "card_id": request["card_id"]},
        "awaiting-evidence",
        owner="old-owner",
        claim_revision="a" * 32,
        attempt=1,
        production=request["production"],
        claim_released=False,
    )
    seen = []

    def reconcile(_paths, _home, node, generation, status):
        seen.append((node, generation["request_id"], status["state"]))
        return {"state": outcome}

    monkeypatch.setattr(builder, "_reconcile_running", reconcile)
    monkeypatch.setattr(builder.Board, "claim_task", lambda *args: pytest.fail("must not claim"))
    builder.consume_one(
        paths, tmp_path, "node-worker", launcher=lambda *args: pytest.fail("must not launch")
    )
    assert seen == [("node-worker", "f" * 64, "awaiting-evidence")]


def test_aggregate_quota_reservations_replace_fixed_worker_count(paths, production_setup):
    p = production_setup
    offers = [
        builder.offer(
            paths,
            _card() | {"id": f"24b000{number:02x}"},
            ["sk-m", "source-only"],
            writer=p.writer,
        )
        for number in range(13)
    ]
    assert all(offers[:12])
    assert offers[12] is None
    builder._write_status(
        paths,
        "node-worker",
        offers[0],
        "awaiting-review",
        exit_code=0,
        owner="source-owner",
        claim_revision="a" * 32,
        claim_released=False,
    )
    assert builder.offer(
        paths, _card() | {"id": "24b000ff"}, ["sk-m", "source-only"], writer=p.writer
    )


def test_unit_invocation_survives_launcher_death_and_refuses_reuse(monkeypatch):
    request = {"card_id": "24b00001", "request_id": "a" * 64}
    status = {
        **request,
        "attempt": 1,
        "unit": production.unit_name(request, 1),
        "invocation": "b" * 32,
    }

    def answer(invocation, state="active"):
        return SimpleNamespace(
            returncode=0,
            stdout=f"LoadState=loaded\nActiveState={state}\nInvocationID={invocation}\nExecMainStatus=0\n",
        )

    monkeypatch.setattr(production.subprocess, "run", lambda *args, **kwargs: answer("b" * 32))
    assert production.service_state(status, SimpleNamespace(poll=lambda: 137)) == (True, None)
    monkeypatch.setattr(production.subprocess, "run", lambda *args, **kwargs: answer("c" * 32))
    assert production.service_state(status, SimpleNamespace(poll=lambda: 0)) == (None, None)
    monkeypatch.setattr(
        production.subprocess, "run", lambda *args, **kwargs: answer("b" * 32, "inactive")
    )
    assert production.service_state(status) == (False, 0)
    monkeypatch.setattr(
        production.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="LoadState=not-found\n"),
    )
    assert production.service_state(status) == (None, None)


@pytest.mark.parametrize("valid", [True, False])
def test_production_exit_preserves_source_custody(
    paths, production_setup, monkeypatch, tmp_path, valid
):
    from skcapstone.fleet import source_bundle

    request = builder.offer(
        paths, _card(), ["sk-m", "source-only"], writer=production_setup.writer
    )
    status = {
        "owner": "source-owner",
        "claim_revision": "a" * 32,
        "attempt": 1,
        "production": request["production"],
        "unit": production.unit_name(request, 1),
    }
    monkeypatch.setattr(builder, "_process_state", lambda status: (False, 0))
    monkeypatch.setattr(builder.CardStore, "fold", lambda *args: _folded(owner="source-owner"))
    monkeypatch.setattr(
        builder, "_release_exact", lambda *args, **kwargs: pytest.fail("must preserve custody")
    )

    def publish(*args, **kwargs):
        if not valid:
            raise source_bundle.SourceBundleError("missing exact candidate")
        return {"manifest_sha256": "b" * 64}

    monkeypatch.setattr(source_bundle, "publish_source", publish)
    result = builder._reconcile_running(paths, tmp_path, "node-worker", request, status)
    assert result["state"] == ("awaiting-review" if valid else "awaiting-evidence")
    assert result["claim_released"] is False
    assert result["owner"] == "source-owner"


@pytest.mark.parametrize(
    "folded,closed",
    [
        ({"owner": None, "status": SimpleNamespace(value="ready")}, True),
        ({"owner": None, "status": SimpleNamespace(value="backlog")}, True),
        ({"owner": None, "status": SimpleNamespace(value="review")}, False),
        ({"owner": "source-owner", "status": SimpleNamespace(value="ready")}, False),
    ],
)
def test_authority_released_generation_closes_instead_of_holding_evidence(
    paths, production_setup, monkeypatch, tmp_path, folded, closed
):
    from skcapstone.fleet import source_bundle

    request = builder.offer(
        paths, _card(), ["sk-m", "source-only"], writer=production_setup.writer
    )
    status = {
        "owner": "source-owner",
        "claim_revision": "a" * 32,
        "attempt": 1,
        "production": request["production"],
        "unit": production.unit_name(request, 1),
    }
    monkeypatch.setattr(builder, "_process_state", lambda status: (False, 0))
    monkeypatch.setattr(builder.CardStore, "fold", lambda *args: _folded(**folded))
    monkeypatch.setattr(
        builder, "_release_exact", lambda *args, **kwargs: pytest.fail("already released")
    )

    def publish(*args, **kwargs):
        raise source_bundle.SourceBundleError("source proposal claim changed")

    monkeypatch.setattr(source_bundle, "publish_source", publish)
    result = builder._reconcile_running(paths, tmp_path, "node-worker", request, status)
    if closed:
        assert result["state"] == "blocked"
        assert result["claim_released"] is True
        assert result["error"] == "claim released by authority before evidence"
    else:
        assert result["state"] == "awaiting-evidence"
        assert result["claim_released"] is False


@pytest.mark.parametrize(
    "case",
    ["empty", "own-link", "own-verdict", "dirty", "other-error", "claim-moved"],
)
def test_exited_generation_without_outcome_is_released_only_when_provably_empty(
    paths, production_setup, monkeypatch, tmp_path, case
):
    from skcapstone.fleet import source_bundle

    request = builder.offer(
        paths, _card(), ["sk-m", "source-only"], writer=production_setup.writer
    )
    claim = "a" * 32
    status = {
        "owner": "source-owner",
        "claim_revision": claim,
        "attempt": 1,
        "production": request["production"],
        "unit": production.unit_name(request, 1),
    }
    folded = _folded(
        owner="source-owner",
        meta=dict(_card()["meta"], _claim_revision="b" * 32 if case == "claim-moved" else claim),
    )
    events = [{"action": "claim", "claim_revision": claim, "ts": "2026-10-10T10:00:00Z"}]
    if case == "own-link":
        events.append({"action": "link", "writer": "source-owner", "ts": "2026-10-10T10:05:00Z"})
    if case == "own-verdict":
        events.append(
            {"action": "verdict", "writer": "source-owner", "ts": "2026-10-10T10:05:00Z"}
        )
    # An older generation's outcome by someone else must not keep custody.
    events.append({"action": "link", "writer": "september-author", "ts": "2026-09-08T00:00:00Z"})
    monkeypatch.setattr(builder, "_process_state", lambda status: (False, 0))
    monkeypatch.setattr(builder.CardStore, "fold", lambda *args: folded)
    monkeypatch.setattr(builder.CardStore, "_read_events", lambda *args: events)

    def clean(workspace, base):
        if case == "dirty":
            raise source_bundle.SourceBundleError("workspace changed")
        return {"head": base}

    monkeypatch.setattr(source_bundle, "inspect_clean_base", clean)
    releases = []
    monkeypatch.setattr(
        builder, "_release_exact", lambda *args, **kwargs: releases.append(args) or True
    )

    def publish(*args, **kwargs):
        if case == "other-error":
            raise source_bundle.SourceBundleError("source proposal claim changed")
        raise source_bundle.SourceBundleError(
            "source proposal lacks a current typed review request"
        )

    monkeypatch.setattr(source_bundle, "publish_source", publish)
    result = builder._reconcile_running(paths, tmp_path, "node-worker", request, status)
    if case == "empty":
        assert result["state"] == "blocked"
        assert result["claim_released"] is True
        assert result["error"] == builder.EXITED_WITHOUT_OUTCOME
        assert len(releases) == 1
        assert builder._unclaimed_expired_offer(request, result) is True
    else:
        assert result["state"] == "awaiting-evidence"
        assert result["claim_released"] is False
        assert releases == []
        assert builder._unclaimed_expired_offer(request, result) is False


def test_released_empty_generation_gets_a_fresh_offer(paths, production_setup):
    first = builder.offer(paths, _card(), ["sk-m", "source-only"], writer=production_setup.writer)
    builder._write_status(
        paths,
        first["node"],
        first,
        "blocked",
        owner="source-owner",
        claim_revision="a" * 32,
        attempt=1,
        production=first["production"],
        claim_released=True,
        error=builder.EXITED_WITHOUT_OUTCOME,
    )
    second = builder.offer(paths, _card(), ["sk-m", "source-only"], writer=production_setup.writer)
    assert second is not None
    assert second["request_id"] != first["request_id"]


def test_stopped_remote_typed_blocked_does_not_replay_or_publish(
    paths, production_setup, monkeypatch, tmp_path
):
    from skcapstone.fleet import production_exit, source_bundle

    request = builder.offer(
        paths, _card(), ["sk-m", "source-only"], writer=production_setup.writer
    )
    status = {
        "owner": "source-owner",
        "claim_revision": "a" * 32,
        "attempt": 1,
        "production": request["production"],
    }
    monkeypatch.setattr(builder, "_process_state", lambda status: (False, 0))
    monkeypatch.setattr(builder.CardStore, "fold", lambda *args: _folded(owner="source-owner"))
    calls = []

    def blocked(home, actual, owner, claim):
        calls.append((actual["card_id"], owner, claim))
        return {
            "reason": "BLOCKED blocked_on=human referent=approval:test",
            "outcome_event": "typed-event",
            "claim_released": True,
        }

    monkeypatch.setattr(production_exit, "release_blocked", blocked)
    monkeypatch.setattr(
        source_bundle, "publish_source", lambda *args, **kwargs: pytest.fail("blocked")
    )
    result = builder._reconcile_running(paths, tmp_path, "node-worker", request, status)
    assert result["state"] == "blocked" and result["claim_released"]
    assert calls == [(request["card_id"], "source-owner", "a" * 32)]
    monkeypatch.setattr(builder, "_process_state", lambda status: (None, None))
    result = builder._reconcile_running(paths, tmp_path, "node-worker", request, status)
    assert result["state"] == "running" and len(calls) == 1


def test_terminal_process_custody_survives_transient_unit_collection(
    paths, production_setup, monkeypatch, tmp_path
):
    from skcapstone.fleet import source_bundle

    request = builder.offer(
        paths, _card(), ["sk-m", "source-only"], writer=production_setup.writer
    )
    status = {
        "state": "awaiting-review",
        "exit_code": 0,
        "owner": "source-owner",
        "claim_revision": "a" * 32,
        "production": request["production"],
        "source_artifact": {"manifest_sha256": "b" * 64},
    }
    monkeypatch.setattr(
        builder, "_process_state", lambda *args: pytest.fail("terminal proof retained")
    )
    monkeypatch.setattr(builder.CardStore, "fold", lambda *args: _folded(owner="source-owner"))

    def retained(*args, **kwargs):
        assert kwargs["acknowledged"] == "b" * 64
        return status["source_artifact"]

    monkeypatch.setattr(source_bundle, "publish_source", retained)
    result = builder._reconcile_running(paths, tmp_path, "node-worker", request, status)
    assert result["state"] == "awaiting-review"
    assert result["claim_released"] is False
