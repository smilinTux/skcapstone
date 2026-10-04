"""Governed review transport on the existing authority and sknoded request tree."""

from __future__ import annotations

import json
import re
import secrets
import socket
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from skcoord.card_store import CardStore

from ..seat_boundaries import canonical_principal
from ..seat_runtime import (
    authorize_review_launch,
    governed_review_assignment_ready,
    recommend_reviewer,
    review_state_revision,
)
from . import builder_dispatch as dispatch
from . import production_builder as production
from . import store
from .production_policy import require_destination, validate_execution_policy
from .production_review import producer_family, provider_family
from .production_review_finish import native_command, read_json
from .source_bundle import _binding, _review_manifest

SCHEMA = "skfleet.builder-dispatch/v2"
CAPABILITY = "remote-review-v1"


def partition_remote_reviews(owned, rollout):
    """Keep legacy provisional reviews on their existing local seat path."""
    remote = [
        row
        for row in owned
        if {"review", "seat-seraph", "source-only"} <= set(row[4])
        and (rollout["card_ids"] is None or row[2] in rollout["card_ids"])
    ]
    remote_ids = {row[2] for row in remote}
    return remote, [row for row in owned if row[2] not in remote_ids]


def validate_contract(request, policy, *, host, historical=False):
    """Reject ambiguous role, placement, generation or independence bindings."""
    validate_execution_policy(policy)
    bound = request["production"]
    admitted = request["policy"]
    validate_execution_policy(admitted)
    rollout = admitted["remote_review"]
    source = request["source"]
    family = producer_family(source["owner"], source["family"])
    if (
        request.get("schema") != SCHEMA
        or request.get("work_kind") != "review"
        or request.get("capability") != CAPABILITY
        or request.get("seat") != "seraph"
        or not {"review", "seat-seraph", "source-only"} <= set(request["labels"])
        or request["reviewer"] != "pi-seraph-" + host + "-" + request["card_id"]
        or canonical_principal(source["owner"]) == canonical_principal(request["reviewer"])
        or provider_family(bound["family"]) != provider_family(bound["provider_family"])
        or provider_family(bound["capacity_domain"]) != provider_family(bound["family"])
        or provider_family(bound["family"]) in {None, family}
        or bound["provider"] != "skgateway"
        or bound["host"] != host
        or bound["authority"] != policy["authority_host"]
        or admitted["authority_host"] != policy["authority_host"]
        or bound["policy_sha256"] != production.digest(admitted)
        or (not historical and admitted != policy)
        or bound["resources"] != require_destination(admitted, host)
        or not rollout["enabled"]
        or host not in rollout["destinations"]
        or (rollout["card_ids"] is not None and request["card_id"] not in rollout["card_ids"])
        or request.get("writer", {}).get("role") != "scheduler"
        or request.get("writer", {}).get("node") != "niobe"
        or not request["writer"].get("identity")
    ):
        raise ValueError("remote review request contract differs")
    require_destination(policy, host)
    for value, width in [
        (request["request_id"], 64),
        (request["card_id"], 8),
        (request["review_revision"], 64),
        (source["card"], 8),
        (source["revision"], 64),
        (source["claim"], 32),
        (source["head"], 40),
        (source["tree"], 40),
        (source["evidence_sha256"], 64),
        (source["manifest_sha256"], 64),
    ]:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{%d}" % width, value):
            raise ValueError("remote review generation is malformed")
    expiry = datetime.fromisoformat(request["lease_expires_at"].replace("Z", "+00:00"))
    if expiry.tzinfo is None or (not historical and expiry <= datetime.now(timezone.utc)):
        raise ValueError("remote review offer expired")


def _source(home, card):
    """Resolve producer facts from native source custody, not review prose."""
    from ..seraph_review_cardstore import LiveCardStoreGateway
    from .production_acceptance import _current_outcome, _producer_terminal

    core = card.model_dump(mode="json")
    parent, head = _binding(core, "link_source_card"), _binding(core, "link_head_revision")
    repository = _binding(core, "repository")
    selected = _review_manifest(core, repository, head)
    source = CardStore(home).fold(parent) if parent else None
    if selected is None or source is None or not source.owner or source.archived:
        raise ValueError("original source custody unavailable")
    manifest, _ = selected
    outcome = _current_outcome(CardStore(home), source)
    revision = LiveCardStoreGateway(home).read_card(parent).revision
    if (
        outcome.get("verdict") != "PASS_FOR_REVIEW"
        or outcome.get("candidate_commit") != head
        or manifest["owner"] != source.owner
        or manifest["claim_revision"] != source.meta.get("_claim_revision")
        or _binding(core, "producer_identity") != source.owner
        or _binding(core, "source_revision") != revision
        or {"hold", "do-not-claim"}.intersection(source.labels)
    ):
        raise ValueError("review source generation changed")
    from ..review_replacement import current_review_attempt

    if current_review_attempt(home, parent, head) != card.id:
        raise ValueError("review is not the current attempt")
    terminal = _producer_terminal(Path(home), source, manifest)
    producer_family(source.owner, terminal["family"])
    return dict(
        card=parent,
        revision=revision,
        claim=source.meta["_claim_revision"],
        owner=source.owner,
        family=terminal["family"],
        head=head,
        tree=manifest["tree"],
        evidence_sha256=manifest["evidence_sha256"],
        manifest_sha256=production.digest(manifest),
    )


def _activation(home, policy, card):
    """Retain the authority's actual activation and product/action scope remotely."""
    from ..niobe_activation import parse_activation

    value = read_json(Path(home) / "coordination/niobe-activation.json")
    activation = parse_activation(value, home=home, host=policy["authority_host"])
    repository = _binding(card.model_dump(mode="json"), "repository") or ""
    product = repository.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git").lower()
    if product not in {p.lower() for p in activation.product_scope}:
        raise ValueError("review product outside active scope")
    return production.digest(value)


def _current(home, request, *, claimed=False):
    """Recheck native explicit dependencies, holds, complete criteria and source."""
    from ..human_wait import assert_human_claim
    from ..review_admission import dependency_blocker_unresolved, governed_review_gate_reasons

    cards = CardStore(home)
    card = cards.fold(request["card_id"])
    if card is None or card.archived or card.meta.get("claim_conflicts"):
        raise ValueError("review is unavailable")
    core = card.model_dump(mode="json")
    dependencies = [cards.fold(key) for key in card.dependencies]
    if (
        any(row is None or row.status.value != "done" for row in dependencies)
        or {"hold", "do-not-claim"}.intersection(card.labels)
        or dependency_blocker_unresolved(home, core, card.labels)
        or governed_review_gate_reasons(core, card.labels)
    ):
        raise ValueError("review has unfinished explicit gates")
    assert_human_claim(home, card.id, request["reviewer"])
    if not claimed and (
        not governed_review_assignment_ready(card)
        or review_state_revision(card) != request["review_revision"]
    ):
        raise ValueError("review changed before claim")
    if claimed and card.owner != request["reviewer"]:
        raise ValueError("review exact owner changed")
    if _source(home, card) != request["source"]:
        raise ValueError("source changed after offer")
    if _activation(home, request["policy"], card) != request["activation_sha256"]:
        raise ValueError("authority activation changed")
    # Claim changes status/owner, but never authorizes edited instructions.
    if (
        production.digest(
            {
                key: core.get(key)
                for key in (
                    "title",
                    "description",
                    "acceptance_criteria",
                    "labels",
                    "dependencies",
                    "links",
                )
            }
        )
        != request["criteria_sha256"]
    ):
        raise ValueError("review contract changed")
    return card


def _capable(paths, node):
    spec = store.read_spec(paths, "node", node) or {}
    # Operator-owned installation capability; a heartbeat cannot self-qualify.
    return CAPABILITY in spec.get("spec", {}).get("capabilities", [])


def _recorded(home, request):
    events = CardStore(home)._read_events(request["card_id"])
    matches = [
        e
        for e in events
        if e.get("action") == "remote_review_offer"
        and e.get("request_id") == request["request_id"]
    ]
    if (
        len(matches) != 1
        or matches[0].get("writer") != "niobe"
        or matches[0].get("request_sha256") != production.digest(request)
    ):
        raise ValueError("exact native authority offer is missing")


def offer_review(paths, home, card_id, *, writer):
    """Offer once under the existing serialized authority placement lock."""
    policy = production.policy()
    if not policy or not policy.get("remote_review", {}).get("enabled"):
        return None
    if (
        socket.gethostname().split(".")[0].lower() != policy["authority_host"]
        or writer.role != "scheduler"
        or writer.node != "niobe"
    ):
        raise ValueError("only the current authority may offer a remote review")
    with dispatch._request_exclusion(paths.root / "dispatch/.production-offer"):
        if not store.actuation_allowed(paths):
            return None
        # Any generation, including expired or orphaned custody, suppresses
        # reassignment. An operator must resolve uncertain custody explicitly.
        existing = list((paths.root / "dispatch").glob("*/" + card_id + ".json"))
        offered = any(
            e.get("action") == "remote_review_offer" for e in CardStore(home)._read_events(card_id)
        )
        if existing or offered or card_id in dispatch.held_card_ids(paths):
            return None
        rollout = policy["remote_review"]
        if rollout["card_ids"] is not None and card_id not in rollout["card_ids"]:
            return None
        card = CardStore(home).fold(card_id)
        if not governed_review_assignment_ready(card):
            return None
        source = _source(home, card)
        from .production_receipts import card_required_size

        route = "sk-" + (card_required_size(card) or "").lower()
        routes = production.production_routes.candidates(policy, route, card.labels)
        family = producer_family(source["owner"], source["family"])
        routes = [r for r in routes if provider_family(r["family"]) not in {None, family}]
        nodes = production.ready_nodes(paths, dispatch._ready_builders(paths), policy, card_id)
        nodes = [
            v
            for v in nodes
            if _capable(paths, v.name)
            and production.node_binding(paths, v.name, policy)["host"] in rollout["destinations"]
        ]
        if not routes or not nodes:
            return None
        node = min(nodes, key=lambda v: (dispatch._node_load(paths, v.name), v.name)).name
        bound = {**production.node_binding(paths, node, policy), **routes[0]}
        core = card.model_dump(mode="json")
        request = dict(
            schema=SCHEMA,
            work_kind="review",
            capability=CAPABILITY,
            request_id=secrets.token_hex(32),
            card_id=card_id,
            node=node,
            seat="seraph",
            reviewer="pi-seraph-" + bound["host"] + "-" + card_id,
            review_revision=review_state_revision(card),
            source=source,
            criteria_sha256=production.digest(
                {
                    key: core.get(key)
                    for key in (
                        "title",
                        "description",
                        "acceptance_criteria",
                        "labels",
                        "dependencies",
                        "links",
                    )
                }
            ),
            activation_sha256=_activation(home, policy, card),
            policy=policy,
            production=bound,
            logical_route=route,
            labels=card.labels,
            repository=_binding(core, "repository"),
            base_revision=source["head"],
            offered_at=dispatch._iso(dispatch._now()),
            lease_expires_at=dispatch._iso(
                dispatch._now() + timedelta(seconds=dispatch.LEASE_SECONDS)
            ),
            writer=dict(role=writer.role, node=writer.node, identity=writer.identity),
        )
        validate_contract(request, policy, host=bound["host"])
        _current(home, request)
        # Persist the request before the ledger proof: a crash holds the offer,
        # and consumers refuse until both independently readable records exist.
        path = dispatch.request_path(paths, node, card_id)
        from .source_bundle import _once

        _once(path, json.dumps(request, sort_keys=True).encode())
        CardStore(home).append_event(
            card_id,
            "remote_review_offer",
            "niobe",
            request_id=request["request_id"],
            request_sha256=production.digest(request),
        )
        return request


def validate_request(paths, home, node, request, *, claimed=False):
    """Validate live destination/node/route truth before any claim or spawn."""
    policy = production.policy()
    if policy is None:
        raise ValueError("remote review requires production policy")
    host = socket.gethostname().split(".")[0].lower()
    try:
        validate_contract(request, policy, host=host)
    except ValueError as exc:
        if str(exc) != "remote review offer expired":
            raise
        # Capacity may defer an exact native offer past its lease without ever
        # launching it. Continue only that same generation under current
        # source/policy/activation checks; unknown custody still stays held.
        if request["policy"] != policy or not _expired_unlaunched(paths, home, node, request):
            raise
        validate_contract(request, policy, host=host, historical=True)
    if request["node"] != node or not _capable(paths, node):
        raise ValueError("destination consumer capability or node differs")
    production.validate_request(paths, node, request, local=True)
    if not any(
        view.name == node
        for view in production.ready_nodes(
            paths, dispatch._ready_builders(paths), policy, request["card_id"]
        )
    ):
        raise ValueError("destination no longer ready or resource feasible")
    if not store.actuation_allowed(paths):
        raise ValueError("fleet actuation is frozen")
    _recorded(home, request)
    return _current(home, request, claimed=claimed)


def _expired_unlaunched(paths, home, node, request):
    """Prove the expired review has recommendation but no launch custody."""
    if dispatch.status_path(paths, node, request["card_id"]).exists():
        return False
    events = CardStore(home)._read_events(request["card_id"])
    recommendation = request["request_id"]
    if not any(
        row.get("action") == "review_assignment_recommendation"
        and row.get("recommendation_id") == recommendation
        and row.get("reviewer") == request["reviewer"]
        for row in events
    ) or any(row.get("action") == "review_assignment_launch" for row in events):
        return False
    observed = process_snapshot(request["card_id"])
    return not any(observed.values())


def process_snapshot(card):
    """Read local service/session custody; failed inventory is a hold."""
    from .production_resources import active_resource_units

    units = [
        r["unit"] for r in active_resource_units() if r["unit"].endswith("-" + card + ".service")
    ]
    result = subprocess.run(
        ["tmux", "ls", "-F", "#{session_name}"], capture_output=True, text=True, timeout=5
    )
    if result.returncode not in {0, 1}:
        raise ValueError("session inventory unavailable")
    sessions = [s for s in result.stdout.splitlines() if s.endswith("-" + card)]
    return dict(units=units, sessions=sessions)


def consume_review(paths, home, node, request, *, launcher=None):
    """Use governed recommendation and native claim, then destination admission."""
    from .production_admission import AdmissionDeferredError
    from .production_resources import active_resource_units, local_worker_admission
    from .source_bundle import import_review_source

    prior = dispatch._validated_status(
        dispatch.status_path(paths, node, request["card_id"]), paths, node
    )
    if prior:
        return reconcile_review(paths, home, request, prior)
    card = validate_request(paths, home, node, request)
    host, owner = request["production"]["host"], request["reviewer"]
    ready, _ = local_worker_admission(request["policy"], host, active_resource_units(home))
    if not ready:
        return None  # No claim, intent or retry is charged for capacity deferral.
    observed = process_snapshot(card.id)
    if any(observed.values()):
        raise ValueError("review already has a same-card process")
    workspace = paths.root / "workspaces" / owner
    if not workspace.exists():
        if not import_review_source(
            card.model_dump(mode="json"),
            request["repository"],
            request["source"]["head"],
            workspace,
        ):
            raise ValueError("exact review source could not be materialized")
    validate_request(paths, home, node, request)
    production.production_routes.preflight(request["policy"], request["production"])
    recommendation = recommend_reviewer(
        home,
        card_id=card.id,
        recommendation_id=request["request_id"],
        author=request["source"]["owner"],
        candidates=[owner],
        observed_process=observed,
        evidence_sha256=request["source"]["evidence_sha256"],
        expected_state_revision=request["review_revision"],
    )
    handoff = authorize_review_launch(
        home,
        recommendation,
        actor=owner,
        current_process=process_snapshot(card.id),
        used_recommendation_ids={
            e.get("recommendation_id")
            for e in CardStore(home)._read_events(card.id)
            if e.get("action") == "review_assignment_launch"
        },
    )
    capacity_path = Path(home) / "evidence" / ("fleet-review-routes." + host + ".json")
    capacity_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    dispatch.atomic_write_text(
        capacity_path, json.dumps(production.production_routes.snapshot(request["policy"]))
    )
    native_command(home, ["claim", card.id, "--agent", owner])
    card = validate_request(paths, home, node, request, claimed=True)
    claim = card.meta["_claim_revision"]
    try:
        return production.launch_review(
            paths, home, request, card, handoff, workspace, launcher=launcher
        )
    except AdmissionDeferredError:
        native_command(
            home,
            [
                "release-claim",
                card.id,
                "--owner",
                owner,
                "--expected-claim-revision",
                claim,
                "--agent",
                owner,
                "--abandon-reason",
                "error",
            ],
        )
        return None


def reconcile_review(paths, home, request, status):
    """Retain every uncertain generation; exit alone never completes a review."""
    from .production_review_custody import read_exit, unit_terminal
    from .source_bundle import export_review_packet, retain_review_packet

    process = dispatch._PROCESSES.get(request["request_id"])
    if process is not None and hasattr(process, "poll") and process.poll() is not None:
        dispatch._PROCESSES.pop(request["request_id"], None)
    if status.get("request_id") != request["request_id"]:
        raise ValueError("different review generation retains custody")
    if status.get("state") in {"awaiting-review-acceptance", "review-fail", "review-blocked"}:
        return status
    terminal = read_exit(home, request["card_id"], status["claim_revision"])
    execution = status["execution"]
    if any(terminal.get(key) != execution[key] for key in ("host", "unit", "invocation")):
        raise ValueError("remote review terminal service differs")
    if terminal.get("execution") != execution or terminal["owner"] != request["reviewer"]:
        raise ValueError("remote review terminal custody differs")
    proof = unit_terminal(execution["unit"], execution["invocation"])
    packet = export_review_packet(
        home, Path(terminal["workspace"]), proposal_binding(request), execution=execution
    )
    verdict = packet["manifest"]["verdict"].split()[0]
    return dispatch._write_status(
        paths,
        request["node"],
        request,
        "awaiting-review-acceptance" if verdict == "PASS" else "review-" + verdict.lower(),
        owner=request["reviewer"],
        claim_revision=status["claim_revision"],
        claim_released=False,
        execution=execution,
        terminal=terminal,
        terminal_observation=proof,
        review_packet=retain_review_packet(home, packet),
        admission=status["admission"],
        route_snapshot=status["route_snapshot"],
        acknowledgment=status["acknowledgment"],
        command=status["command"],
    )


def proposal_binding(request):
    """One original candidate and exact independent reviewer for Git inspection."""
    return dict(
        card=request["card_id"],
        parent_card=request["source"]["card"],
        source_head=request["source"]["head"],
        source_tree=request["source"]["tree"],
        reviewer_identity=request["reviewer"],
    )


def validate_execution(*args, **kwargs):
    """Use the shared receipt validator before recording a native launch."""
    from .production_receipts import validate_review_execution

    return validate_review_execution(*args, **kwargs)
