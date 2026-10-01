"""Protect exact production source custody from generic liveness reclamation."""

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from skcoord.card_store import CardStore

from .paths import FleetPaths
from .production_receipts import load_production_snapshot
from .source_bundle import MAX_EVIDENCE, _binding, _read


def source_binding(card) -> dict:
    """Return the exact native source binding, rejecting incomplete/conflicting data."""
    value = {
        key: _binding({"meta": card.meta, "links": card.links}, key)
        for key in ("repository", "base_ref", "base_revision")
    }
    url = urlsplit(value["repository"] or "")
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or not value["base_ref"]
        or not re.fullmatch(r"[0-9a-f]{40}", value["base_revision"] or "")
    ):
        raise ValueError("production custody source binding is incomplete")
    return value


def retains_source_custody(
    home: Path, card_id: str, owner: str, claim: str, *, fleet_paths: FleetPaths
) -> bool:
    """Require current native ownership plus a real production launch generation.

    A missing proposal can mean delayed evidence publication. Once an exact
    producer has launched, generic absence or age cannot decide its outcome.
    Independent review, explicit typed BLOCKED or operator recovery owns release.
    """
    if not re.fullmatch(r"[0-9a-f]{8}", card_id):
        return False
    try:
        store = CardStore(home)
        card = store.fold(card_id)
        if (
            card is None
            or card.owner != owner
            or card.meta.get("_claim_revision") != claim
            or not claim
            or card.archived
            or "source-only" not in card.labels
            or "review" in card.labels
            or "[REVIEW]" in card.title.upper()
            or card.meta.get("link_source_card")
            or card.links.get("link_source_card")
            or getattr(card.status, "value", card.status) not in {"ready", "doing", "review"}
        ):
            return False
        binding = source_binding(card)
        events = store._read_events(card_id)
        claims = [
            row
            for row in events
            if row.get("action") == "claim"
            and (row.get("claim_revision") or row.get("event_id")) == claim
        ]
        if len(claims) != 1:
            return False
        for event in events:
            if (
                event.get("action") != "production_assignment_launch"
                or event.get("schema") != "skfleet.production-assignment-launch/v1"
                or event.get("writer") != owner
                or event.get("worker") != owner
                or event.get("claim_revision") != claim
                or event.get("launched") is not True
                or event.get("source_binding") != binding
                or str(event.get("ts", "")) < str(claims[0].get("ts", ""))
            ):
                continue
            try:
                route = event["route_identity"]
                if route.get("provider") != "skgateway":
                    continue
                observed = load_production_snapshot(home, route["production_snapshot"])
                if any(
                    row.get("model_or_bucket") == route.get("model_or_bucket")
                    and row.get("capacity_domain") in route.get("capacity_domains", [])
                    for row in observed["routes"]
                ):
                    return True
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return _remote_custody(fleet_paths, card_id, owner, claim, binding)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def _remote_custody(fleet_paths, card_id, owner, claim, binding):
    """Match selected native request/status contents, never just their filenames."""
    paths = list((fleet_paths.root / "dispatch").glob("*/" + card_id + ".json"))
    if len(paths) > 32:
        return False
    for path in paths:
        try:
            node = path.parent.name
            request = json.loads(_read(path, MAX_EVIDENCE))
            status_path = fleet_paths.status_path(node, "dispatch", card_id)
            status = json.loads(_read(status_path, MAX_EVIDENCE))
            token = request.get("request_id")
            production = request.get("production")
            attempt = status.get("attempt")
            if (
                request.get("schema") != "skfleet.builder-dispatch/v1"
                or status.get("schema") != "skfleet.builder-dispatch-status/v1"
                or request.get("card_id") != card_id
                or status.get("card_id") != card_id
                or request.get("node") != node
                or status.get("node") != node
                or not isinstance(token, str)
                or not re.fullmatch(r"[0-9a-f]{64}", token)
                or status.get("request_id") != token
                or status.get("owner") != owner
                or status.get("claim_revision") != claim
                or status.get("claim_released") is True
                or status.get("state") not in {"running", "awaiting-evidence", "awaiting-review"}
                or not isinstance(production, dict)
                or status.get("production") != production
                or production.get("host") != node.removeprefix("node-")
                or not re.fullmatch(r"[0-9a-f]{64}", str(production.get("policy_sha256", "")))
                or type(attempt) is not int
                or attempt <= 0
                or status.get("unit") != f"skfleet-builder-{card_id}-{token}-{attempt}.service"
                or any(request.get(key) != value for key, value in binding.items())
            ):
                continue
            proof = status.get("route_preflight") or {}
            if (
                proof.get("requested_identity") == production.get("model")
                and proof.get("provider") == production.get("gateway_backend")
                and isinstance(proof.get("requested_identity"), str)
                and proof["requested_identity"]
                and isinstance(proof.get("provider"), str)
                and proof["provider"]
            ):
                return True
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return False
