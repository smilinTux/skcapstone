"""Protect exact production source custody from generic liveness reclamation."""

import json
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from skcoord.card_store import CardStore

from .paths import FleetPaths
from .production_receipts import load_production_snapshot
from .production_routes import served_backend_matches
from .refusal_log import reason_of, refuse
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
        site = "source-review-candidate"
        if not policy:
            return refuse(site, card_id, "no production policy")
        if card is None:
            return refuse(site, card_id, "card not found")
        released_revision = None
        if card.owner is None:
            from .released_source import direct_review_view

            released_revision = (
                LiveCardStoreGateway(home).read_card(card_id, _store=store).revision
            )
            store, _, _ = direct_review_view(home, card)
            card = store.fold(card_id)
        if card.owner != producer:
            return refuse(site, card_id, "card owner differs from verdict producer")
        if card.meta.get("claim_conflicts"):
            return refuse(site, card_id, "claim conflicts present")
        if {"hold", "do-not-claim"}.intersection(card.labels):
            return refuse(site, card_id, "card carries hold or do-not-claim")
        if not retains_source_custody(
            home,
            card_id,
            producer,
            card.meta.get("_claim_revision"),
            fleet_paths=FleetPaths(home / "fleet"),
            store=store,
        ):
            return refuse(site, card_id, "source custody not retained")
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
        for key, value in expected.items():
            if outcome.get(key) != value:
                return refuse(site, card_id, f"current outcome {key} differs from candidate")
        if snapshot.verdict != "PASS_FOR_REVIEW" or snapshot.producer_identity != producer:
            return refuse(site, card_id, "card snapshot verdict or producer differs")
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
        for key, value in expected.items():
            if manifest.get(key) != value:
                return refuse(site, card_id, f"source bundle manifest {key} differs")
        if (
            _root(home, card_id).parent not in Path(path).parents
            or _sha(_read(Path(path), MAX_EVIDENCE)) != evidence_sha
        ):
            return refuse(site, card_id, "candidate evidence path or sha256 differs")
        terminal = _producer_terminal(home, card, manifest)
        if terminal.get("receipt_kind") == "direct-seat":
            if terminal["host"] != policy["authority_host"]:
                return refuse(site, card_id, "direct producer is not on the authority")
        else:
            request = read_json(home / "fleet/dispatch" / terminal["node"] / (card_id + ".json"))
            _request_matches_current_card(home, request, retained_claim=True)
            production = request["production"]
            # A consumed continuation ran under the current policy (rebind_request),
            # so its immutable offer may name an older policy digest. Requiring the
            # offer digest to equal today's policy kept every re-bound continuation
            # from ever being reviewed (b1f004f2, 7ad93023 on chi, 2026-10-11).
            status = read_json(
                FleetPaths(home / "fleet").status_path(terminal["node"], "dispatch", card_id)
            )
            continued = bool(status.get("continuation_consumed")) and (
                status.get("request_id") == request.get("request_id")
            )
            if (
                request.get("schema") != "skfleet.builder-dispatch/v1"
                or request.get("node") != terminal["node"]
                or any(request.get(key) != value for key, value in binding.items())
                or (production.get("policy_sha256") != digest(policy) and not continued)
                or production.get("authority") != policy["authority_host"]
            ):
                return refuse(
                    site, card_id, "builder request differs from current binding or policy"
                )
        process = process_check(card_id)
        if process.get("sessions") != [] or process.get("units") != []:
            return refuse(site, card_id, "card still has a live session or unit")
        unit_terminal(terminal["unit"], terminal["invocation"], host=terminal["host"])
        if not verify_fresh:
            return True
        # Re-read with a new store so the check notices events appended while
        # the shared inspection snapshot was in use.
        if gateway.read_card(card_id).revision != (released_revision or snapshot.revision):
            return refuse(site, card_id, "card revision changed during inspection")
        return True
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        subprocess.SubprocessError,
    ) as exc:
        return refuse("source-review-candidate", card_id, reason_of(exc))


def source_binding(card) -> dict:
    """Return the exact native source binding, rejecting incomplete/conflicting data."""
    value = {
        key: _binding({"id": card.id, "meta": card.meta, "links": card.links}, key)
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
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return refuse("source-custody", card_id, reason_of(exc))


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
            # A consumed continuation launches under the current policy
            # (rebind_request: same host and model family), so its preflight may
            # name today's route rather than the immutable offer's model. The
            # served backend must still match the offer. Without this, every
            # re-bound continuation lost source custody and was never reviewed
            # (b1f004f2, 7ad93023 on chi, 2026-10-11).
            requested_ok = proof.get("requested_identity") == production.get("model") or (
                bool(status.get("continuation_consumed"))
                and proof.get("provider") == production.get("gateway_backend")
            )
            if (
                requested_ok
                and served_backend_matches(
                    proof.get("provider"), production.get("gateway_backend")
                )
                and isinstance(proof.get("requested_identity"), str)
                and proof["requested_identity"]
                and isinstance(proof.get("provider"), str)
                and proof["provider"]
            ):
                return True
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return False


REVIEW_CUSTODY_STATES = {"running", "awaiting-review-acceptance"}


def retains_review_custody(
    home: Path,
    card_id: str,
    owner: str,
    claim: str,
    *,
    fleet_paths: FleetPaths,
    store=None,
) -> bool:
    """Keep a remote Seraph reviewer's claim until production acceptance consumes it.

    The reviewer exits after its handoff and must keep its claim; acceptance
    later requires that exact owner and claim. retains_source_custody refuses
    review cards, so the absent-worker reaper released every finished review
    (worker_absent_after_quorum, then release_claim) and acceptance stopped on
    'native pair changed before trusted tests' (add19bf8, 369cb4bc, 2026-10-10).
    """
    if not re.fullmatch(r"[0-9a-f]{8}", card_id) or not owner or not claim:
        return False
    try:
        store = store or CardStore(home)
        card = store.fold(card_id)
        if (
            card is None
            or card.archived
            or card.owner != owner
            or card.meta.get("_claim_revision") != claim
        ):
            return False
        launches = [
            event
            for event in store._read_events(card_id)
            if event.get("action") == "review_assignment_launch"
            and event.get("schema") == "skfleet.review-assignment-launch/v3"
            and event.get("writer") == owner
            and event.get("reviewer") == owner
            and event.get("claim_revision") == claim
            and event.get("launched") is True
        ]
        if not launches:
            return False
        execution = launches[-1].get("execution") or {}
        node = str(execution.get("node") or "")
        token = execution.get("request_id")
        if not re.fullmatch(r"node-[a-z0-9-]{1,60}", node) or not isinstance(token, str):
            return False
        status = json.loads(
            _read(fleet_paths.status_path(node, "dispatch", card_id), MAX_EVIDENCE)
        )
        return (
            status.get("card_id") == card_id
            and status.get("work_kind") == "review"
            and status.get("request_id") == token
            and status.get("owner") == owner
            and status.get("claim_revision") == claim
            and status.get("state") in REVIEW_CUSTODY_STATES
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return refuse("review-custody", card_id, reason_of(exc))
