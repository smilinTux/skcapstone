"""Recover publication of an exact stopped direct producer, never its verdict."""

import hashlib
import json
import re
import socket
from pathlib import Path

from skcoord.atomic_io import atomic_write_text
from skcoord.card_store import CardStore, card_mutation_lock

from .production_acceptance import _current_outcome, _direct_seat_terminal
from .production_custody import source_binding
from .production_receipts import load_production_snapshot
from .production_review_custody import unit_terminal
from .production_review_finish import once, read_json
from .source_bundle import MAX_EVIDENCE, _read, publish_source


def inspect_direct_source(home, card, *, store=None):
    """Prove local native custody and stable launch-time source identity."""
    store = store or CardStore(home)
    if (
        card is None
        or card.archived
        or not card.owner
        or not re.fullmatch(r"pi-[a-z0-9_.-]+", card.owner)
        or card.status.value not in {"ready", "doing", "review"}
        or {"hold", "do-not-claim", "review", "seat-seraph"}.intersection(card.labels)
        or card.meta.get("claim_conflicts")
        or "[REVIEW]" in card.title.upper()
        or card.meta.get("link_source_card")
        or card.links.get("link_source_card")
    ):
        raise ValueError("direct source custody is not current")
    claim = card.meta.get("_claim_revision")
    outcome = _current_outcome(store, card)
    if outcome.get("verdict") != "PASS_FOR_REVIEW":
        raise ValueError("direct source requires current typed PASS_FOR_REVIEW")
    path = home / "fleet/direct-seats" / f"{card.owner}.json"
    receipt = read_json(path)
    pending = receipt.get("completion_state") == "awaiting-evidence"
    artifact = (receipt.get("source_disposition") or {}).get("source_artifact") or {}
    terminal = _direct_seat_terminal(
        home,
        card,
        {} if pending else artifact,
        card.owner,
        claim,
        publication_pending=pending,
    )
    binding = source_binding(card)
    events = store._read_events(card.id) + store._legacy_events(card.id)
    claims = [
        e
        for e in events
        if e.get("action") == "claim" and (e.get("claim_revision") or e.get("event_id")) == claim
    ]
    launches = [
        e
        for e in events
        if e.get("action") == "production_assignment_launch"
        and e.get("schema") == "skfleet.production-assignment-launch/v1"
        and e.get("launched") is True
        and e.get("writer") == card.owner
        and e.get("worker") == card.owner
        and e.get("claim_revision") == claim
    ]
    if len(claims) != 1 or len(launches) != 1:
        raise ValueError("exact native direct source launch required")
    launch = launches[0]
    if not claims[0]["ts"] <= launch["ts"] <= outcome["ts"]:
        raise ValueError("direct source launch chronology differs")
    if "source_binding" in launch:
        if launch["source_binding"] != binding:
            raise ValueError("direct source binding differs from launch")
    elif any(
        e.get("action") == "link"
        and e.get("link_key") in binding
        and e.get("ts", "") > launch["ts"]
        for e in events
    ):
        # Old local launches omitted this field for complete, unlabeled bindings.
        # Birth metadata plus unchanged native links fixes the launch-time value.
        raise ValueError("direct source binding changed after legacy launch")
    route = launch.get("route_identity") or {}
    if route.get("provider") != "skgateway" or any(
        route.get(key) != receipt.get(key)
        for key in ("provider", "logical_route", "capacity_domains", "model_or_bucket")
    ):
        raise ValueError("direct source route differs from native launch")
    observed = load_production_snapshot(home, route["production_snapshot"])
    if not any(
        r.get("model_or_bucket") == route["model_or_bucket"]
        and r.get("capacity_domain") in route["capacity_domains"]
        for r in observed["routes"]
    ):
        raise ValueError("direct source route snapshot differs")
    admission = read_json(home / "fleet/admission" / f"{card.id}.json")
    expected = {
        "card_id": card.id,
        "owner": card.owner,
        "claim_revision": claim,
        "host": terminal["host"],
        "unit": terminal["unit"],
    }
    workspace = Path(str(admission.get("workspace") or ""))
    if (
        any(admission.get(k) != v for k, v in expected.items())
        or not workspace.is_absolute()
        or workspace.resolve() != workspace
        or not workspace.is_dir()
    ):
        raise ValueError("direct source admission workspace differs")
    return receipt, terminal, binding, admission, outcome, launch


def recover_one(home, card_id, *, authority_host, process_check):
    """Publish retained bytes under an unchanged claim after fresh terminal proof."""
    home = Path(home)
    if socket.gethostname().split(".")[0].lower() != authority_host:
        raise ValueError("direct source recovery requires its authority host")
    store = CardStore(home)
    card = store.fold(card_id)
    before = inspect_direct_source(home, card, store=store)
    receipt, terminal, binding, admission, _outcome, launch = before
    if terminal["host"] != authority_host:
        raise ValueError("direct source recovery host differs")
    if receipt["completion_state"] == "awaiting-review":
        return {"card": card_id, "state": "already-published"}
    process = process_check(card_id)
    if process.get("sessions") != [] or process.get("units") != []:
        raise ValueError("direct source still has a live worker")
    unit_terminal(terminal["unit"], terminal["invocation"], host=terminal["host"])
    artifact = publish_source(
        home,
        {"card_id": card_id, **binding},
        card.owner,
        card.meta["_claim_revision"],
        Path(admission["workspace"]),
    )
    with card_mutation_lock(home, card_id):
        fresh = CardStore(home)
        if inspect_direct_source(home, fresh.fold(card_id), store=fresh) != before:
            raise ValueError("direct source changed during publication")
        process = process_check(card_id)
        if process.get("sessions") != [] or process.get("units") != []:
            raise ValueError("direct source worker appeared during publication")
        unit_terminal(terminal["unit"], terminal["invocation"], host=terminal["host"])
        path = Path(terminal["receipt_path"])
        if hashlib.sha256(_read(path, MAX_EVIDENCE)).hexdigest() != terminal["receipt_sha256"]:
            raise ValueError("direct source terminal receipt changed")
        once(
            home
            / "fleet/source-publication-recovery"
            / f"{card_id}-{card.meta['_claim_revision']}.json",
            {
                "original_receipt": receipt,
                "terminal": terminal,
                "source_binding": binding,
                "launch_event": launch["event_id"],
                "source_artifact": artifact,
            },
        )
        updated = {
            **receipt,
            "completion_state": "awaiting-review",
            "source_disposition": {
                "state": "awaiting-review",
                "claim_released": False,
                "process_terminal": True,
                "source_artifact": artifact,
            },
            "source_publication_recovery": {
                "original_receipt_sha256": terminal["receipt_sha256"],
                "launch_event": launch["event_id"],
                "source_binding": binding,
            },
        }
        atomic_write_text(path, json.dumps(updated, sort_keys=True) + "\n")
        path.chmod(0o600)
    return {"card": card_id, "state": "published", "head": artifact["head"]}


def recover_pending_direct_sources(home, policy, *, process_check, cycle=None, limit=8):
    """Use the existing cycle budget/cursor to retry stopped local publications."""
    import subprocess

    home = Path(home)
    authority = policy.get("authority_host")
    if authority != socket.gethostname().split(".")[0].lower():
        return []
    paths = sorted((home / "fleet/direct-seats").glob("*.json"))
    cursor = "direct_source_publication"
    if cycle is not None:
        paths = cycle.rotate_from(paths, cycle.read_cursor(cursor), key=lambda p: p.name)
    results = []
    store = CardStore(home)
    for index, path in enumerate(paths):
        if len(results) >= limit or (
            cycle is not None and cycle.stop("review_sweeps", index, len(paths) - index)
        ):
            if cycle is not None:
                cycle.write_cursor(cursor, path.name)
            return results
        card_id = ""
        try:
            receipt = read_json(path)
            if receipt.get("completion_state") != "awaiting-evidence":
                continue
            card_id = receipt.get("card") or ""
            if not re.fullmatch(r"[0-9a-f]{8}", card_id):
                continue
            card = store.fold(card_id)
            if (
                card is None
                or card.owner != receipt.get("owner")
                or path.name != f"{card.owner}.json"
                or card.meta.get("_claim_revision") != receipt.get("claim_revision")
                or _current_outcome(store, card).get("verdict") != "PASS_FOR_REVIEW"
            ):
                continue
            results.append(
                recover_one(home, card_id, authority_host=authority, process_check=process_check)
            )
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            subprocess.SubprocessError,
        ) as exc:
            results.append(
                {"card": card_id or path.stem, "state": "refused", "reason": str(exc)[:200]}
            )
    if cycle is not None:
        cycle.write_cursor(cursor, None)
    return results
