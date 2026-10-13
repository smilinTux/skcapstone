"""Retained direct PASS candidates require exact stopped launch custody."""

import json
from pathlib import Path

import pytest

from skcapstone.fleet import direct_source_recovery as recovery
from skcapstone.fleet.production_receipts import persist_production_snapshot
from skcapstone.fleet.production_review_finish import once, read_json
from tests.fleet.test_production_receipts import snapshot
from tests.fleet.test_source_bundle import source  # noqa: F401


@pytest.fixture
def direct(source, monkeypatch):  # noqa: F811
    value = source
    home, store, card, owner = (value[k] for k in ("home", "store", "card", "owner"))
    monkeypatch.setattr(recovery.socket, "gethostname", lambda: "worker")
    monkeypatch.setattr(recovery, "unit_terminal", lambda *a, **k: {})
    route = {
        "provider": "skgateway",
        "logical_route": "sk-m",
        "capacity_domains": ["deepseek"],
        "model_or_bucket": "gateway-current",
        "production_snapshot": persist_production_snapshot(home, snapshot()),
    }
    store.append_event(card, "remove_label", "lumina-nor", label="source-only")
    launch = store.append_event(
        card,
        "production_assignment_launch",
        owner,
        schema="skfleet.production-assignment-launch/v1",
        worker=owner,
        claim_revision=value["claim"],
        launched=True,
        route_identity=route,
    )
    store.append_event(card, "verdict", owner, **value["outcome"])
    receipt = {
        "schema": "skfleet.direct-seat/v1",
        "card": card,
        "owner": owner,
        "claim_revision": value["claim"],
        "host": "worker",
        "lane": "glm",
        "pid": 123,
        "unit": f"skfleet-worker-glm-{card}.service",
        "invocation": "e" * 32,
        "completion_state": "awaiting-evidence",
        "exit_code": 0,
        "route_schema": "skfleet.runtime-route/v1",
        **{
            k: route[k]
            for k in ("provider", "logical_route", "capacity_domains", "model_or_bucket")
        },
        "source_disposition": {
            "state": "awaiting-evidence",
            "process_terminal": True,
            "claim_released": False,
            "reason": "exact-typed-candidate-publication-pending",
        },
    }
    path = home / "fleet/direct-seats" / f"{owner}.json"
    once(path, receipt)
    admission = {
        "card_id": card,
        "owner": owner,
        "claim_revision": value["claim"],
        "host": "worker",
        "unit": receipt["unit"],
        "workspace": str(value["workspace"]),
    }
    once(home / "fleet/admission" / f"{card}.json", admission)
    value.update(receipt_path=path, receipt=receipt, launch=launch)
    return value


def run(value, process=None):
    return recovery.recover_one(
        value["home"],
        value["card"],
        authority_host="worker",
        process_check=process or (lambda _: {"sessions": [], "units": []}),
    )


@pytest.mark.parametrize("source", ["https"], indirect=True)
def test_recovers_exact_candidate_without_board_write_and_is_idempotent(direct):
    before = direct["store"]._read_events(direct["card"])
    result = run(direct)
    assert result == {"card": direct["card"], "state": "published", "head": direct["head"]}
    assert read_json(direct["receipt_path"])["source_disposition"]["state"] == "awaiting-review"
    assert direct["store"]._read_events(direct["card"]) == before
    assert direct["store"].fold(direct["card"]).owner == direct["owner"]
    assert run(direct)["state"] == "already-published"


@pytest.mark.parametrize("source", ["https"], indirect=True)
@pytest.mark.parametrize(
    "mutation",
    [
        "claim",
        "outcome",
        "hold",
        "source-link",
        "route",
        "exit",
        "invocation",
        "not-terminal",
        "release",
        "admission",
        "dirty",
        "live",
        "unit",
    ],
)
def test_refuses_missing_or_changed_proof_without_receipt_write(direct, mutation, monkeypatch):
    v = direct
    store, card = v["store"], v["card"]
    receipt = dict(v["receipt"])
    if mutation == "claim":
        store.append_event(card, "claim", v["owner"], owner=v["owner"])
    elif mutation == "outcome":
        store.append_event(card, "verdict", v["owner"], verdict="BLOCKED")
    elif mutation == "hold":
        store.append_event(card, "add_label", "lumina-nor", label="hold")
    elif mutation == "source-link":
        store.append_event(card, "link", "lumina-nor", link_key="base_ref", link_value="main")
    elif mutation == "route":
        receipt["model_or_bucket"] = "different"
    elif mutation == "exit":
        receipt["exit_code"] = 1
    elif mutation == "invocation":
        receipt["invocation"] = ""
    elif mutation == "not-terminal":
        receipt["source_disposition"]["process_terminal"] = False
    elif mutation == "release":
        receipt["source_disposition"]["claim_released"] = True
    elif mutation == "admission":
        (v["home"] / "fleet/admission" / f"{card}.json").unlink()
    elif mutation == "dirty":
        (v["workspace"] / "source.txt").write_text("uncommitted")
    elif mutation == "unit":

        def unknown(*a, **k):
            raise ValueError("worker unit unavailable")

        monkeypatch.setattr(recovery, "unit_terminal", unknown)
    v["receipt_path"].write_text(json.dumps(receipt))
    before = v["receipt_path"].read_bytes()
    process = (lambda _: {"sessions": ["live"], "units": []}) if mutation == "live" else None
    with pytest.raises((ValueError, OSError)):
        run(v, process)
    assert v["receipt_path"].read_bytes() == before


@pytest.mark.parametrize("source", ["https"], indirect=True)
def test_recovered_legacy_launch_enters_existing_review_opener(direct, monkeypatch):
    from skcapstone.fleet import production_review_custody
    from tests.fleet.test_production_custody import candidate_allowed

    monkeypatch.setattr(production_review_custody, "unit_terminal", lambda *a, **k: {})
    direct["policy"] = {"authority_host": "worker"}
    assert run(direct)["state"] == "published"
    assert candidate_allowed(direct)


@pytest.mark.parametrize("source", ["https"], indirect=True)
@pytest.mark.parametrize(
    "mutation",
    ["missing-launch", "binding", "snapshot", "no-exit", "public-receipt", "admission-owner"],
)
def test_exact_native_launch_and_private_receipts_required(direct, monkeypatch, mutation):
    from skcoord.card_store import CardStore

    if mutation in {"missing-launch", "binding"}:
        read = CardStore._read_events

        def changed(store, card):
            rows = read(store, card)
            if mutation == "missing-launch":
                return [row for row in rows if row.get("action") != "production_assignment_launch"]
            return [
                (
                    {**row, "source_binding": {"base_revision": "f" * 40}}
                    if row.get("action") == "production_assignment_launch"
                    else row
                )
                for row in rows
            ]

        monkeypatch.setattr(CardStore, "_read_events", changed)
    elif mutation == "snapshot":
        Path(direct["launch"]["route_identity"]["production_snapshot"]["path"]).unlink()
    elif mutation == "no-exit":
        receipt = read_json(direct["receipt_path"])
        receipt.pop("exit_code")
        direct["receipt_path"].write_text(json.dumps(receipt))
    elif mutation == "public-receipt":
        direct["receipt_path"].chmod(0o644)
    elif mutation == "admission-owner":
        path = direct["home"] / "fleet/admission" / f"{direct['card']}.json"
        admission = read_json(path)
        admission["owner"] = "different-owner"
        path.write_text(json.dumps(admission))
    before = direct["receipt_path"].read_bytes()
    with pytest.raises((ValueError, OSError)):
        run(direct)
    assert direct["receipt_path"].read_bytes() == before


@pytest.mark.parametrize("source", ["https"], indirect=True)
def test_changed_receipt_during_export_never_advances_custody(direct, monkeypatch):
    publish = recovery.publish_source

    def raced(*args, **kwargs):
        artifact = publish(*args, **kwargs)
        receipt = read_json(direct["receipt_path"])
        receipt["heartbeat_at"] = "changed"
        direct["receipt_path"].write_text(json.dumps(receipt))
        return artifact

    monkeypatch.setattr(recovery, "publish_source", raced)
    with pytest.raises(ValueError, match="changed during publication"):
        run(direct)
    assert read_json(direct["receipt_path"])["completion_state"] == "awaiting-evidence"


@pytest.mark.parametrize("source", ["https"], indirect=True)
def test_sweep_honors_shared_deadline_and_recovers_after_resume(direct, tmp_path):
    from skcapstone.fleet.cycle_deadline import CycleDeadline

    clock = [100.0]
    cycle = CycleDeadline(
        100, started=0, clock=lambda: clock[0], state_dir=str(tmp_path / "cycle")
    )
    policy = {"authority_host": "worker"}

    def process(_):
        return {"sessions": [], "units": []}

    assert (
        recovery.recover_pending_direct_sources(
            direct["home"], policy, process_check=process, cycle=cycle
        )
        == []
    )
    assert cycle.read_cursor("direct_source_publication") == direct["receipt_path"].name
    clock[0] = 0
    assert (
        recovery.recover_pending_direct_sources(
            direct["home"], policy, process_check=process, cycle=cycle
        )[0]["state"]
        == "published"
    )
    assert cycle.read_cursor("direct_source_publication") is None
    assert (
        recovery.recover_pending_direct_sources(
            direct["home"], policy, process_check=process, cycle=cycle
        )
        == []
    )


@pytest.mark.parametrize("source", ["https"], indirect=True)
def test_claim_changed_during_export_refuses_receipt_update(direct, monkeypatch):
    publish = recovery.publish_source

    def raced(*args, **kwargs):
        artifact = publish(*args, **kwargs)
        direct["store"].append_event(
            direct["card"], "claim", direct["owner"], owner=direct["owner"]
        )
        return artifact

    monkeypatch.setattr(recovery, "publish_source", raced)
    before = direct["receipt_path"].read_bytes()
    with pytest.raises(ValueError):
        run(direct)
    assert direct["receipt_path"].read_bytes() == before


@pytest.mark.parametrize("source", ["https"], indirect=True)
def test_wrong_authority_cannot_publish(direct):
    with pytest.raises(ValueError, match="authority host"):
        recovery.recover_one(
            direct["home"],
            direct["card"],
            authority_host="elsewhere",
            process_check=lambda _: {"sessions": [], "units": []},
        )
