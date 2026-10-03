"""One operator-authorized replacement of a stopped, invalid native review."""

from __future__ import annotations

import json
import re
import socket
import subprocess
from pathlib import Path

from skcoord.card_store import CardStore, card_mutation_lock
from skcoord.review_replacement import read_artifact as _read

from .seraph_review_contracts import _digest

ACTION = "review_replacement_authorization"
SCHEMA = "skfleet.review-replacement-authorization/v1"


def _events(store: CardStore, card_id: str) -> list[dict]:
    """Bind native and legacy history without depending on directory order."""
    rows = store._read_events(card_id) + store._legacy_events(card_id)
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True))


def _fold_digest(card) -> str:
    """Bind even malformed review metadata without parsing it as a valid review."""
    value = card.model_dump(mode="json")
    value.pop("updated_at", None)
    return _digest(value)


def _source(home: Path, source_card: str) -> tuple[dict, str]:
    """Resolve the current exact producer candidate and attempt-zero identity."""
    from .guarded_review_work import _candidate
    from .link_review_work import card_generation, review_card_id
    from .seraph_review_cardstore import LiveCardStoreGateway, _latest_outcome

    store = CardStore(home)
    card = store.fold(source_card)
    if (
        card is None
        or card.archived
        or card.status.value not in {"ready", "doing", "review"}
        or "source-only" not in card.labels
        or "review" in card.labels
        or card.meta.get("claim_conflicts")
        or not card.owner
        or not re.fullmatch(r"[0-9a-f]{32}", str(card.meta.get("_claim_revision", "")))
    ):
        raise ValueError("replacement source claim invalid")
    outcome = _latest_outcome(store, source_card)
    if (
        outcome.get("action") != "verdict"
        or outcome.get("writer") != card.owner
        or outcome.get("verdict") not in {"PASS_FOR_REVIEW", "PASS_FOR_REREVIEW"}
    ):
        raise ValueError("replacement typed source candidate missing")
    _candidate(home, source_card, outcome)
    generation = card_generation(card)
    binding = dict(
        source_card=source_card,
        producer=card.owner,
        producer_claim=card.meta["_claim_revision"],
        source_revision=LiveCardStoreGateway(home).read_card(source_card).revision,
        source_native_revision=_fold_digest(card),
        source_events_sha256=_digest(_events(store, source_card)),
        source_head=outcome["candidate_commit"],
        source_tree=outcome["candidate_tree"],
        source_ref=outcome["candidate_ref"],
        source_evidence_sha256=outcome["candidate_sha256"],
        source_outcome_sha256=_digest(outcome),
        source_generation=generation,
    )
    return binding, review_card_id(
        source_card, outcome["candidate_commit"], generation, outcome["candidate_sha256"]
    )


def _retained_proof(home: Path, request: dict) -> tuple[dict, dict]:
    """Verify attributed incident bytes and the independent exact launch receipt."""
    incident = json.loads(_read(home, request["invalidation"]))
    launch = json.loads(_read(home, request["launch_receipt"]))
    if not isinstance(incident, dict) or not isinstance(launch, dict):
        raise ValueError("replacement retained proof malformed")
    if (
        incident.get("schema") != "skfleet.independent-review-discrepancy/v1"
        or launch.get("schema") != "skfleet.independent-review-checkout-readback/v1"
        or incident.get("review_card") != request["predecessor"]
        or incident.get("source_card") != request["source_card"]
        or incident.get("source_head") != request["source_head"]
        or launch.get("review_card") != request["predecessor"]
        or launch.get("source_head") != request["source_head"]
        or launch.get("source_tree") != request["source_tree"]
        or launch.get("owner") != request["predecessor_owner"]
        or launch.get("claim_revision") != request["predecessor_claim"]
        or incident.get("native_review", {}).get("owner") != request["predecessor_owner"]
        or incident.get("native_review", {}).get("claim_revision") != request["predecessor_claim"]
    ):
        raise ValueError("replacement retained proof binding invalid")
    for name in ("COMPLETION-EVIDENCE.md", "REVIEW-DECISION.json"):
        _read(home, incident["committed_artifacts"][name])
    invocation = launch.get("unit_properties", {}).get("InvocationID")
    if (
        not isinstance(invocation, str)
        or not re.fullmatch(r"[0-9a-f]{32}", invocation)
        or incident.get("expected_invocation_id") != invocation
        or incident.get("unit", {}).get("ActiveState") not in {"inactive", "failed"}
        or incident.get("unit", {}).get("MainPID") != "0"
        or incident.get("unit", {}).get("InvocationID") not in {"", invocation}
    ):
        raise ValueError("replacement retained terminal proof invalid")
    return incident, launch


def _terminal(home: Path, request: dict) -> dict:
    """Recheck stopped process custody, including a previously collected unit."""
    _, launch = _retained_proof(home, request)
    unit = launch.get("unit", "")
    if request.get("authority_host") != socket.gethostname().split(".")[0] or not re.fullmatch(
        r"skfleet-worker-(?:codex|glm|deepseek|qwen)-"
        + re.escape(request["predecessor"])
        + r"\.service",
        unit,
    ):
        raise ValueError("replacement terminal host or unit invalid")
    result = subprocess.run(
        [
            "systemctl",
            "--user",
            "show",
            unit,
            "--property=LoadState,ActiveState,SubState,MainPID,InvocationID,ControlGroup,Job,TasksCurrent",
        ],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if result.returncode:
        raise ValueError("replacement terminal unit unknown")
    props = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if (
        props.get("ActiveState") not in {"inactive", "failed"}
        or props.get("SubState") not in {"dead", "failed"}
        or props.get("MainPID") != "0"
        or props.get("Job") != ""
        or props.get("ControlGroup") != ""
        or props.get("TasksCurrent") not in {"0", "[not set]"}
    ):
        raise ValueError("replacement reviewer still live or unknown")
    invocation = launch["unit_properties"]["InvocationID"]
    if props.get("LoadState") == "not-found":
        pid = str(launch["unit_properties"].get("MainPID", ""))
        if (
            props.get("InvocationID") != ""
            or not pid.isdecimal()
            or int(pid) <= 1
            or Path("/proc", pid).exists()
        ):
            raise ValueError("replacement collected process death unproven")
    elif props.get("LoadState") != "loaded" or props.get("InvocationID") != invocation:
        raise ValueError("replacement terminal invocation changed")
    return dict(
        unit=unit,
        invocation_id=invocation,
        properties=props,
        retained_launch_sha256=request["launch_receipt"]["sha256"],
        retained_terminal_sha256=request["invalidation"]["sha256"],
    )


def _validate(
    home: Path, request: dict, *, live_source: bool = True, _store: CardStore | None = None
) -> tuple[dict, str]:
    """Validate source and raw predecessor custody without legacy PR parsing."""
    from .blocked_verdict import is_blocked_verdict
    from .seraph_review_cardstore import _latest_outcome

    if (
        not isinstance(request, dict)
        or not isinstance(request.get("invalidation"), dict)
        or not isinstance(request.get("launch_receipt"), dict)
    ):
        raise ValueError("replacement authorization malformed")
    store = _store if _store is not None else CardStore(home)
    if live_source:
        binding, predecessor = _source(home, request["source_card"])
    else:
        # Reading a completed review is not permission to complete its producer.
        # Feature A checks the live source initially, then uses its guarded intent.
        from .link_review_work import review_card_id

        binding = request
        predecessor = review_card_id(
            request["source_card"],
            request["source_head"],
            request["source_generation"],
            request["source_evidence_sha256"],
        )
        outcomes = [
            row
            for row in _events(store, request["source_card"])
            if _digest(row) == request["source_outcome_sha256"]
        ]
        if (
            len(outcomes) != 1
            or outcomes[0].get("action") != "verdict"
            or outcomes[0].get("writer") != request["producer"]
        ):
            raise ValueError("replacement original source history changed")
    if request.get("schema") != SCHEMA or any(request.get(k) != v for k, v in binding.items()):
        raise ValueError("replacement source generation changed")
    if request.get("predecessor") != predecessor:
        raise ValueError("replacement predecessor is not canonical attempt zero")
    old = store.fold(predecessor)
    if (
        old is None
        or old.archived
        or old.status.value == "done"
        or not {"review", "source-only", "do-not-claim"}.issubset(old.labels)
        or old.owner != request.get("predecessor_owner")
        or old.owner == binding["producer"]
        or not old.owner
        or old.meta.get("claim_conflicts")
        or old.meta.get("_claim_revision") != request.get("predecessor_claim")
        or _fold_digest(old) != request.get("predecessor_revision")
    ):
        raise ValueError("replacement predecessor custody changed")
    rows = [r for r in _events(store, predecessor) if r.get("action") != ACTION]
    if _digest(rows) != request.get("predecessor_events_sha256"):
        raise ValueError("replacement predecessor history changed")
    invalidation = request["invalidation"]
    matches = [r for r in rows if _digest(r) == invalidation.get("event_sha256")]
    if (
        len(matches) != 1
        or matches[0].get("writer") != "jarvis"
        or matches[0].get("action") != "link"
        or matches[0].get("link_key") != "operator_review_invalidation"
        or str(invalidation.get("path")) not in str(matches[0].get("link_value", ""))
        or "sha256=" + str(invalidation.get("sha256")) not in str(matches[0].get("link_value", ""))
    ):
        raise ValueError("replacement operator invalidation missing")
    outcome = _latest_outcome(store, predecessor)
    if outcome.get("writer") != "jarvis" or not is_blocked_verdict(
        "verdict", outcome.get("verdict") or outcome.get("link_value", "")
    ):
        raise ValueError("replacement invalidation was superseded")
    expected = dict(
        link_source_card=binding["source_card"],
        link_head_revision=binding["source_head"],
        link_card_generation=binding["source_generation"],
        link_evidence_sha256=binding["source_evidence_sha256"],
        producer_identity=binding["producer"],
        candidate_tree=binding["source_tree"],
        candidate_ref=binding["source_ref"],
        source_revision=binding["source_revision"],
    )
    if any(old.meta.get(key) != value for key, value in expected.items()):
        raise ValueError("replacement predecessor source binding changed")
    _retained_proof(home, request)
    return binding, predecessor


def replacement_binding(home: Path, source_card: str, head: str) -> dict:
    """Return validated lineage for the only authorized current attempt."""
    binding, predecessor = _source(home, source_card)
    if binding["source_head"] != head:
        raise ValueError("replacement requested source head changed")
    return _record_binding(home, predecessor)


def _record_binding(
    home: Path, predecessor: str, *, live_source: bool = True, _store: CardStore | None = None
) -> dict:
    """Validate stored lineage independently of a later completion snapshot."""
    from .link_review_work import review_card_id

    store = _store if _store is not None else CardStore(home)
    authorizations = [r for r in _events(store, predecessor) if r.get("action") == ACTION]
    if not authorizations:
        return {"review_card_id": predecessor}
    if len(authorizations) != 1:
        raise ValueError("replacement authorization ambiguous")
    event = authorizations[0]
    request = event.get("authorization", {})
    digest = _digest(request)
    if event.get("writer") != "jarvis" or event.get("transition_id") != digest:
        raise ValueError("replacement authorization invalid")
    binding, _ = _validate(home, request, live_source=live_source, _store=store)
    current = review_card_id(
        binding["source_card"],
        binding["source_head"],
        binding["source_generation"],
        binding["source_evidence_sha256"],
        review_attempt=digest,
    )
    return dict(
        review_card_id=current,
        review_attempt=digest,
        review_predecessor=predecessor,
        review_replacement_authorization=event["event_id"],
    )


def current_review_attempt(home: Path, source_card: str, head: str) -> str:
    """Identify the only acceptable review, failing closed on invalid lineage."""
    return replacement_binding(home, source_card, head)["review_card_id"]


def authorized_predecessor(
    home: Path, review, sibling, *, _store: CardStore | None = None
) -> bool:
    """Exclude only the exact invalid predecessor of a validated replacement."""
    if (
        not review.meta.get("review_attempt")
        or review.meta.get("review_predecessor") != sibling.id
    ):
        return False
    # Share only the caller's current snapshot; never retain a store across reads.
    expected = _record_binding(home, sibling.id, live_source=False, _store=_store)
    if expected["review_card_id"] != review.id or any(
        review.meta.get(key) != value for key, value in expected.items() if key != "review_card_id"
    ):
        raise ValueError("replacement review lineage changed")
    return True


def authorize_replacement(
    home: Path, request: dict, *, actor: str, check_only: bool = False
) -> dict:
    """Persist operator authority once, then resume the existing native opener."""
    from .guarded_review_work import open_guarded_review
    from .jarvis_emergency import authorize_coord_mutation
    from .seat_boundaries import Action

    if actor != "jarvis":
        raise ValueError("review replacement requires operator identity")
    authorize_coord_mutation(actor, Action.CREATE_CARD, request["source_card"], None, None)
    if not re.fullmatch(r"[0-9a-f]{8}", str(request.get("predecessor", ""))):
        raise ValueError("replacement predecessor invalid")
    with (
        card_mutation_lock(home, request["source_card"]),
        card_mutation_lock(home, request["predecessor"]),
    ):
        _validate(home, request)
        terminal = _terminal(home, request)
        store = CardStore(home)
        digest = _digest(request)
        prior = [r for r in _events(store, request["predecessor"]) if r.get("action") == ACTION]
        if prior and (
            len(prior) != 1
            or prior[0].get("authorization") != request
            or prior[0].get("writer") != actor
        ):
            raise ValueError("replacement already authorized differently")
        if check_only:
            return {
                "source_card": request["source_card"],
                "predecessor": request["predecessor"],
                "authorization_sha256": digest,
                "state": "validated-not-opened",
            }
        if not prior:
            store.append_event(
                request["predecessor"],
                ACTION,
                actor,
                transition_id=digest,
                authorization=request,
                terminal_check=terminal,
            )
        lineage = replacement_binding(home, request["source_card"], request["source_head"])
        existing = store.fold(lineage["review_card_id"])
        if existing is not None and (existing.owner or existing.status.value == "done"):
            if any(existing.meta.get(k) != v for k, v in lineage.items() if k != "review_card_id"):
                raise ValueError("replacement existing review binding changed")
            return dict(
                source_card=request["source_card"],
                head_revision=request["source_head"],
                review_card_id=existing.id,
                created=False,
                launchable=False,
                reason="replacement_already_started",
            )
        result = open_guarded_review(
            home,
            request["source_card"],
            request["producer"],
            expected_source_revision=request["source_revision"],
            expected_claim_revision=request["producer_claim"],
        )
        _validate(home, request)
        return result.as_dict()
