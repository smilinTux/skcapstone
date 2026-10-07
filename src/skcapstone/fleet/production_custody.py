"""Protect exact production source custody from generic liveness reclamation."""

import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from skcoord.card_store import CardStore

from .paths import FleetPaths
from .production_receipts import load_production_snapshot
from .source_bundle import MAX_EVIDENCE, _binding, _read


def reviewable_source_candidate(
    home: Path,
    card_id: str,
    outcome_ts: str,
    candidate: tuple,
    *,
    policy: dict,
    process_check,
    store=None,
    verify_fresh=True,
) -> bool:
    """Admit only an exact stopped production handoff, without releasing custody."""
    from ..seraph_review_cardstore import LiveCardStoreGateway
    from .builder_dispatch import _request_matches_current_card
    from .production_acceptance import _current_outcome, _producer_terminal
    from .production_builder import digest
    from .production_review_custody import unit_terminal
    from .production_review_finish import read_json
    from .source_bundle import _root, _sha
    from .source_transport import _packet

    try:
        producer, path, evidence_sha, head, tree, ref = candidate
        store = store or CardStore(home)
        card = store.fold(card_id)
        if (
            not policy
            or card is None
            or card.owner != producer
            or card.meta.get("claim_conflicts")
            or {"hold", "do-not-claim"}.intersection(card.labels)
            or not retains_source_custody(
                home,
                card_id,
                producer,
                card.meta.get("_claim_revision"),
                fleet_paths=FleetPaths(home / "fleet"),
                store=store,
            )
        ):
            return False
        gateway = LiveCardStoreGateway(home)
        snapshot = gateway.read_card(card_id, _store=store)
        outcome = _current_outcome(store, card)
        expected = {
            "action": "verdict",
            "verdict": "PASS_FOR_REVIEW",
            "ts": outcome_ts,
            "candidate_commit": head,
            "candidate_tree": tree,
            "candidate_ref": ref,
            "candidate_path": path,
            "candidate_sha256": evidence_sha,
        }
        if any(outcome.get(key) != value for key, value in expected.items()):
            return False
        if snapshot.verdict != "PASS_FOR_REVIEW" or snapshot.producer_identity != producer:
            return False
        binding = source_binding(card)
        # Read already transferred bytes only; eligibility never initiates a transfer.
        manifest = _packet(home, card_id, head)["manifest"]
        expected = {
            "schema": "skfleet.source-bundle/v1",
            "card": card_id,
            "owner": producer,
            "claim_revision": card.meta["_claim_revision"],
            "head": head,
            "tree": tree,
            "ref": ref,
            "evidence_sha256": evidence_sha,
            "repository": binding["repository"],
            "base_revision": binding["base_revision"],
        }
        if any(manifest.get(key) != value for key, value in expected.items()):
            return False
        if (
            _root(home, card_id).parent not in Path(path).parents
            or _sha(_read(Path(path), MAX_EVIDENCE)) != evidence_sha
        ):
            return False
        terminal = _producer_terminal(home, card, manifest)
        request = read_json(home / "fleet/dispatch" / terminal["node"] / (card_id + ".json"))
        _request_matches_current_card(home, request, retained_claim=True)
        production = request["production"]
        if (
            request.get("schema") != "skfleet.builder-dispatch/v1"
            or request.get("node") != terminal["node"]
            or any(request.get(key) != value for key, value in binding.items())
            or production.get("policy_sha256") != digest(policy)
            or production.get("authority") != policy["authority_host"]
        ):
            return False
        process = process_check(card_id)
        if process.get("sessions") != [] or process.get("units") != []:
            return False
        unit_terminal(terminal["unit"], terminal["invocation"], host=terminal["host"])
        if not verify_fresh:
            return True
        # Re-read with a new store so the check notices events appended while
        # the shared inspection snapshot was in use.
        return gateway.read_card(card_id).revision == snapshot.revision
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        return False


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
    home: Path,
    card_id: str,
    owner: str,
    claim: str,
    *,
    fleet_paths: FleetPaths,
    store=None,
) -> bool:
    """Require current native ownership plus a real production launch generation.

    A missing proposal can mean delayed evidence publication. Once an exact
    producer has launched, generic absence or age cannot decide its outcome.
    Independent review, explicit typed BLOCKED or operator recovery owns release.
    """
    if not re.fullmatch(r"[0-9a-f]{8}", card_id):
        return False
    try:
        store = store or CardStore(home)
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
