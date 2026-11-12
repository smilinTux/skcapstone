import datetime
import os
import time
from types import SimpleNamespace

import pytest

from skcapstone.fleet.production_receipts import (
    load_production_snapshot,
    persist_production_snapshot,
    production_receipt_allowed,
)
from skcapstone.fleet.review_capacity import seal_review_capacity_truth


def snapshot():
    return seal_review_capacity_truth(
        {
            "schema_version": 1,
            "cycle_id": "fixture",
            "observed_at": time.time(),
            "endpoint": "http://gateway.test",
            "error": None,
            "routes": [
                {
                    "logical_route": "gateway-current",
                    "model_or_bucket": "gateway-current",
                    "provider": "deepseek",
                    "capacity_domain": "deepseek",
                    "state": "healthy",
                    "size_class": "M",
                    "policy_tier": "paid-cloud",
                    "max": 1,
                    "gateway_active": 0,
                }
            ],
        },
        {},
    )


def receipt(tmp_path):
    snap = snapshot()
    ref = persist_production_snapshot(tmp_path, snap)
    launch = {
        "host": "chiap08",
        "card": "deadbeef",
        "owner": "pi-atlas-deadbeef",
        "revision": "exact-claim",
        "lane": "deepseek",
        "model": "gateway-current",
    }
    route = {
        "provider": "skgateway",
        "logical_route": "sk-m",
        "model_or_bucket": "gateway-current",
        "capacity_domains": ["deepseek"],
        "production_snapshot": ref,
    }
    event = {
        "writer": launch["owner"],
        "claim_revision": launch["revision"],
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "route_identity": route,
    }
    card = SimpleNamespace(title="[M] Useful card", labels=["dispatch-approved"], meta={})
    policy = {
        "authority_host": "chiap08",
        "gateway_url": "http://gateway.test",
        "lanes": {
            name: {
                "enabled": name != "kimi",
                **({"provider": "skgateway"} if name != "kimi" else {}),
            }
            for name in ("codex", "glm", "deepseek", "qwen", "kimi")
        },
    }
    return snap, ref, launch, event, card, policy


def test_immutable_private_snapshot_roundtrip_and_idempotence(tmp_path):
    snap, ref, *_ = receipt(tmp_path)
    assert persist_production_snapshot(tmp_path, snap) == ref
    assert load_production_snapshot(tmp_path, ref) == snap
    path = tmp_path / "evidence/production-routes" / f"{snap['capacity_revision']}.json"
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_text("{}")
    with pytest.raises(ValueError):
        persist_production_snapshot(tmp_path, snap)


def test_receipt_uses_original_capacity_even_after_last_slot_consumed(tmp_path, monkeypatch):
    snap, ref, launch, event, card, policy = receipt(tmp_path)
    monkeypatch.setattr(time, "time", lambda: snap["observed_at"] + 1000)
    assert production_receipt_allowed(tmp_path, policy, launch, card, event)


@pytest.mark.parametrize(
    "mutation",
    [
        "claim",
        "writer",
        "model",
        "domain",
        "provider",
        "host",
        "size",
        "privacy",
        "family",
        "stale-at-launch",
        "future-at-launch",
        "seal",
        "hash",
        "path",
        "missing",
    ],
)
def test_invalid_receipt_or_changed_card_contract_refused(tmp_path, mutation):
    snap, ref, launch, event, card, policy = receipt(tmp_path)
    if mutation == "claim":
        event["claim_revision"] = "other"
    if mutation == "writer":
        event["writer"] = "another-owner"
    if mutation == "model":
        launch["model"] = "other-model"
    if mutation == "domain":
        event["route_identity"]["capacity_domains"] = ["zai"]
    if mutation == "provider":
        event["route_identity"]["provider"] = "direct-provider"
    if mutation == "host":
        launch["host"] = "chiap01"
    if mutation == "size":
        card.title = "[XL] Changed requirement"
    if mutation == "privacy":
        card.labels.append("local-only")
    if mutation == "family":
        card.labels.append("codex-only")
    if mutation in {"stale-at-launch", "future-at-launch"}:
        delta = 1000 if mutation == "stale-at-launch" else -1000
        event["ts"] = datetime.datetime.fromtimestamp(
            snap["observed_at"] + delta, datetime.timezone.utc
        ).isoformat()
    if mutation == "seal":
        ref["capacity_revision"] = "0" * 64
    if mutation == "hash":
        ref["sha256"] = "0" * 64
    if mutation == "path":
        ref["path"] = "/etc/passwd"
    if mutation == "missing":
        del event["route_identity"]["production_snapshot"]
    assert not production_receipt_allowed(tmp_path, policy, launch, card, event)


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_nonregular_snapshot_never_blocks_or_reads_redirect(tmp_path, kind):
    snap, ref, *_ = receipt(tmp_path)
    path = tmp_path / "evidence/production-routes" / f"{snap['capacity_revision']}.json"
    original = path.read_bytes()
    path.unlink()
    target = tmp_path / "untouched"
    target.write_bytes(original)
    if kind == "symlink":
        path.symlink_to(target)
    else:
        os.mkfifo(path)
    with pytest.raises((OSError, ValueError)):
        load_production_snapshot(tmp_path, ref)
    assert target.read_bytes() == original


def test_native_production_receipt_binds_exact_claim_and_is_idempotent(tmp_path):
    from skcapstone.card_store import CardCore, CardStore
    from skcapstone.seat_runtime import append_production_launch_receipt

    snap, ref, launch, event, card, policy = receipt(tmp_path)
    store = CardStore(tmp_path)
    store.create(CardCore(id=launch["card"], title="[M] Useful", created_by="fixture"))
    store.append_event(
        launch["card"],
        "claim",
        launch["owner"],
        owner=launch["owner"],
        claim_revision=launch["revision"],
    )
    kwargs = dict(
        actor=launch["owner"],
        claim_revision=launch["revision"],
        launched=True,
        route_identity=event["route_identity"],
    )
    recorded = append_production_launch_receipt(tmp_path, launch["card"], **kwargs)
    again = append_production_launch_receipt(tmp_path, launch["card"], **kwargs)
    assert recorded == again
    assert recorded["action"] == "production_assignment_launch"
    assert recorded["writer"] == launch["owner"]
    assert production_receipt_allowed(
        tmp_path, policy, launch, store.fold(launch["card"]), recorded
    )


@pytest.mark.parametrize("mutation", ["owner", "claim", "missing-snapshot", "bad-snapshot"])
def test_native_production_receipt_refuses_without_appending(tmp_path, mutation):
    from skcapstone.card_store import CardCore, CardStore
    from skcapstone.seat_boundaries import BoundaryError
    from skcapstone.seat_runtime import append_production_launch_receipt

    snap, ref, launch, event, card, policy = receipt(tmp_path)
    store = CardStore(tmp_path)
    store.create(CardCore(id=launch["card"], title="[M] Useful", created_by="fixture"))
    store.append_event(
        launch["card"],
        "claim",
        launch["owner"],
        owner=launch["owner"],
        claim_revision=launch["revision"],
    )
    kwargs = dict(
        actor=launch["owner"],
        claim_revision=launch["revision"],
        launched=True,
        route_identity=event["route_identity"],
    )
    if mutation == "owner":
        kwargs["actor"] = "pi-atlas-another"
    if mutation == "claim":
        kwargs["claim_revision"] = "old-claim"
    if mutation == "missing-snapshot":
        del kwargs["route_identity"]["production_snapshot"]
    if mutation == "bad-snapshot":
        kwargs["route_identity"]["production_snapshot"]["sha256"] = "0" * 64
    before = store._read_events(launch["card"])
    with pytest.raises(BoundaryError):
        append_production_launch_receipt(tmp_path, launch["card"], **kwargs)
    assert store._read_events(launch["card"]) == before


def test_review_receipt_accepts_validated_snapshot_extension(tmp_path):
    from skcapstone.card_store import CardCore, CardStore
    from skcapstone.seat_runtime import ReviewLaunchHandoff, append_review_launch_receipt

    snap, ref, launch, event, card, policy = receipt(tmp_path)
    store = CardStore(tmp_path)
    store.create(CardCore(id=launch["card"], title="[M] Review", created_by="fixture"))
    store.append_event(
        launch["card"],
        "claim",
        launch["owner"],
        owner=launch["owner"],
        claim_revision=launch["revision"],
    )
    handoff = ReviewLaunchHandoff(launch["card"], launch["owner"], "recommendation", "state")
    row = append_review_launch_receipt(
        tmp_path,
        handoff,
        actor=launch["owner"],
        claim_revision=launch["revision"],
        launched=True,
        route_identity=event["route_identity"],
    )
    assert row["route_identity"]["production_snapshot"] == ref
