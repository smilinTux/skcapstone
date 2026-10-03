"""Authority-only bridge from stopped independent review to tested completion."""

import base64
import json
import os
import re
import socket
import subprocess
from pathlib import Path

from skcoord.card_store import CardStore

from ..review_verdict import _source_only_applicability, is_review_card
from ..seraph_review_cardstore import LiveCardStoreGateway, _latest_outcome
from .production_receipts import production_receipt_allowed
from .production_review import producer_family, provider_family
from .production_review_custody import read_exit, unit_terminal
from .production_review_evidence import ReviewEvidenceError, inspect_proposal
from .production_review_finish import artifacts, finish_pair, native_state, once, read_json
from .source_bundle import (
    _binding,
    _once,
    _review_manifest,
    _root,
    _sha,
    import_review_source,
)


def digest(value):
    """Use one canonical digest for immutable controller context."""
    return _sha(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def review_directory(home, card, claim):
    """Restrict acceptance state to an exact native review claim."""
    if not re.fullmatch(r"[0-9a-f]{8}", card) or not re.fullmatch(r"[0-9a-f]{32}", claim):
        raise ReviewEvidenceError("acceptance identity invalid")
    return Path(home) / "evidence/production-acceptance" / (card + "-" + claim)


def _producer_terminal(home, source, manifest):
    """Read native sknoded terminal custody for this exact producer generation."""
    matches = []
    for path in (home / "fleet/status").glob("*/dispatch/" + source.id + ".json"):
        status = read_json(path)
        if status.get("owner") != source.owner or status.get("claim_revision") != source.meta.get(
            "_claim_revision"
        ):
            continue
        artifact = status.get("source_artifact") or {}
        if (
            status.get("schema") != "skfleet.builder-dispatch-status/v1"
            or status.get("node") != path.parent.parent.name
            or status.get("card_id") != source.id
            or status.get("state") != "awaiting-review"
            or status.get("exit_code") != 0
            or status.get("claim_released") is not False
            or (status.get("writer") or {}).get("role") != "sknoded"
            or (status.get("writer") or {}).get("node") != status["node"]
            or any(artifact.get(key) != value for key, value in manifest.items())
        ):
            raise ReviewEvidenceError("native producer terminal custody differs")
        request = read_json(home / "fleet/dispatch" / status["node"] / (source.id + ".json"))
        if (
            request.get("request_id") != status.get("request_id")
            or request.get("card_id") != source.id
            or request.get("production") != status.get("production")
        ):
            raise ReviewEvidenceError("producer terminal request changed")
        from .production_builder import unit_name

        if status.get("unit") != unit_name(request, status.get("attempt")):
            raise ReviewEvidenceError("producer terminal unit differs")
        matches.append(
            {
                "unit": status["unit"],
                "invocation": status.get("invocation"),
                "host": status["production"]["host"],
                "request_id": status["request_id"],
                "family": status["production"]["family"],
                "node": status["node"],
            }
        )
    if len(matches) != 1:
        raise ReviewEvidenceError("one native producer terminal receipt required")
    return matches[0]


def _current_outcome(store, row):
    """Bind the native outcome to the original current claim, including time."""
    claim = row.meta.get("_claim_revision")
    claims = [
        event
        for event in store._read_events(row.id)
        if event.get("action") == "claim"
        and (event.get("claim_revision") or event.get("event_id")) == claim
    ]
    outcome = _latest_outcome(store, row.id)
    if (
        len(claims) != 1
        or outcome.get("writer") != row.owner
        or outcome.get("expected_claim_revision") not in (None, claim)
        or str(outcome.get("ts", "")) < str(claims[0].get("ts", ""))
    ):
        raise ReviewEvidenceError("native outcome predates current claim")
    return outcome


def terminal_guard(context, process_check):
    """Recheck exact recorded workers; no receipt grants permission to stop them."""
    for role in ("source", "review"):
        item = context[role]
        process = process_check(item["card"])
        if process.get("sessions") or process.get("units"):
            raise ReviewEvidenceError("native worker remains active")
        terminal = item["terminal"]
        unit_terminal(terminal["unit"], terminal["invocation"], host=terminal["host"])


def collect(home, policy, card, claim, *, process_check):
    """Capture an exact native pair and committed proposal before any mutation."""
    home = Path(home)
    directory = review_directory(home, card, claim)
    store = CardStore(home)
    review = store.fold(card)
    if (
        review is None
        or not review.owner
        or review.meta.get("_claim_revision") != claim
        or review.meta.get("claim_conflicts")
        or "source-only" not in review.labels
        or "review" not in review.labels
        or not is_review_card(review.title)
        or {"do-not-claim", "hold"}.intersection(review.labels)
        or review.archived
        or review.status.value in {"done", "archived"}
    ):
        raise ReviewEvidenceError("review custody is not current")
    core = review.model_dump(mode="json")
    parent = _binding(core, "link_source_card")
    head = _binding(core, "link_head_revision")
    source = store.fold(parent) if parent else None
    if (
        source is None
        or source.archived
        or "source-only" not in source.labels
        or {"do-not-claim", "hold"}.intersection(source.labels)
        or source.meta.get("claim_conflicts")
        or not source.owner
        or source.status.value in {"done", "archived"}
        or source.owner == review.owner
    ):
        raise ReviewEvidenceError("original producer custody unavailable")
    try:
        from ..review_replacement import current_review_attempt
    except ModuleNotFoundError as exc:
        if exc.name != "skcapstone.review_replacement":
            raise ReviewEvidenceError("review replacement dependency failed") from exc
        if review.meta.get("review_attempt") or review.links.get("superseded_by"):
            raise ReviewEvidenceError("review replacement authority dependency missing") from None
    else:
        if current_review_attempt(home, parent, head) != card:
            raise ReviewEvidenceError("review is not the authorized current attempt")
    gateway = LiveCardStoreGateway(home)
    source_snapshot, review_snapshot = gateway.read_card(parent), gateway.read_card(card)
    outcome = _current_outcome(store, source)
    review_outcome = _current_outcome(store, review)
    if (
        source_snapshot.verdict != "PASS_FOR_REVIEW"
        or source_snapshot.head_sha != head
        or outcome.get("action") != "verdict"
        or outcome.get("writer") != source.owner
        or _binding(core, "producer_identity") != source.owner
        or _binding(core, "source_revision") != source_snapshot.revision
        or review_snapshot.verdict != "PASS"
        or review_snapshot.unresolved_review
        or review_outcome.get("writer") != review.owner
        or not _source_only_applicability(card, home)
    ):
        raise ReviewEvidenceError("native proposal or source-only applicability differs")
    repository = _binding(core, "repository")
    selected = _review_manifest(core, repository, head)
    if selected is None:
        raise ReviewEvidenceError("exact unpublished source bundle required")
    manifest, _ = selected
    if (
        manifest["owner"] != source.owner
        or manifest["claim_revision"] != source.meta.get("_claim_revision")
        or manifest["tree"] != outcome.get("candidate_tree")
    ):
        raise ReviewEvidenceError("source bundle belongs to another claim")
    terminal = read_exit(home, card, claim)
    if terminal["owner"] != review.owner or terminal["source_head"] != head:
        raise ReviewEvidenceError("review terminal binding differs")
    launches = [
        event
        for event in store._read_events(card)
        if event.get("action") == "review_assignment_launch"
        and event.get("claim_revision") == claim
        and event.get("launched") is True
    ]
    launch = {
        "host": terminal["host"],
        "owner": review.owner,
        "revision": claim,
        "model": terminal["model"],
        "lane": terminal["lane"],
    }
    if len(launches) != 1 or not production_receipt_allowed(
        home, policy, launch, review, launches[0]
    ):
        raise ReviewEvidenceError("exact sealed independent review launch missing")
    domain = launches[0]["route_identity"]["capacity_domains"][0]
    source_terminal = _producer_terminal(home, source, manifest)
    if producer_family(source.owner, source_terminal["family"]) == provider_family(domain):
        raise ReviewEvidenceError("review family is not independent")
    proposal = inspect_proposal(
        Path(terminal["workspace"]),
        card=card,
        parent_card=parent,
        source_head=head,
        source_tree=manifest["tree"],
        reviewer_identity=review.owner,
    )
    if (
        proposal["proposal"]["verdict"] != "PASS"
        or review_snapshot.evidence_sha256 != proposal["report_sha256"]
    ):
        raise ReviewEvidenceError("committed independent review did not pass")
    report = directory / "COMPLETION-EVIDENCE.md"
    decision = directory / "REVIEW-DECISION.json"
    for key, path in (("report", report), ("decision", decision)):
        _once(path, base64.b64decode(proposal[key + "_b64"], validate=True))
    workspace = directory / "source"
    if not os.path.lexists(workspace):
        if not import_review_source(core, repository, head, workspace):
            raise ReviewEvidenceError("exact source test checkout unavailable")
    source_item = {
        "card": parent,
        "owner": source.owner,
        "claim": source.meta["_claim_revision"],
        "revision": source_snapshot.revision,
        "head": head,
        "tree": manifest["tree"],
        "ref": manifest["ref"],
        "repository": repository,
        "terminal": source_terminal,
        "evidence_path": str(_root(home, parent) / (manifest["evidence_sha256"] + ".md")),
        "evidence_sha256": manifest["evidence_sha256"],
    }
    review_item = {
        "card": card,
        "owner": review.owner,
        "claim": claim,
        "revision": review_snapshot.revision,
        "terminal": terminal,
        "review_head": proposal["review_head"],
        "review_tree": proposal["review_tree"],
        "evidence_path": str(report),
        "evidence_sha256": proposal["report_sha256"],
        "decision_path": str(decision),
        "decision_sha256": proposal["decision_sha256"],
        "proposal": proposal["proposal"],
        "launch_event": launches[0],
    }
    binding = {
        "source_card": parent,
        "source_owner": source.owner,
        "source_claim_revision": source_item["claim"],
        "source_head": head,
        "source_tree": manifest["tree"],
        "source_revision": source_snapshot.revision,
        "criteria_sha256": digest(source.acceptance_criteria),
    }
    context = {
        "schema": "skfleet.production-acceptance-context/v1",
        "controller": "fleet-review-closer@" + policy["authority_host"],
        "policy_sha256": digest(policy),
        "source": source_item,
        "review": review_item,
        "test_binding": binding,
        "source_workspace": str(workspace),
    }
    terminal_guard(context, process_check)
    if (
        gateway.read_card(parent).revision != source_snapshot.revision
        or gateway.read_card(card).revision != review_snapshot.revision
    ):
        raise ReviewEvidenceError("native pair changed while collecting proposal")
    once(directory / "context.json", context)
    return context


def reconcile(home, policy, *, process_check):
    """Reconcile retained review exits with bounded per-card refusal isolation."""
    from .production_test_profile import seal_candidate
    from .production_tests import run_or_read_tests

    home = Path(home)
    if policy["authority_host"] != socket.gethostname().split(".")[0].lower():
        raise ReviewEvidenceError("source acceptance is authority-only")
    results = []
    for path in sorted((home / "evidence/production-review-exits").glob("*.json")):
        match = re.fullmatch(r"([0-9a-f]{8})-([0-9a-f]{32})\.json", path.name)
        if not match:
            continue
        card, claim = match.groups()
        directory = review_directory(home, card, claim)
        try:
            context_path = directory / "context.json"
            context = (
                read_json(context_path)
                if context_path.exists()
                else collect(home, policy, card, claim, process_check=process_check)
            )
            if context["policy_sha256"] != digest(policy):
                raise ReviewEvidenceError("acceptance operational policy changed")

            def guard():
                return terminal_guard(context, process_check)

            if not (directory / "finish-intent.json").exists():
                guard()
                for role in ("source", "review"):
                    item = context[role]
                    state = native_state(home, item["card"])
                    if (
                        state["revision"] != item["revision"]
                        or state["owner"] != item["owner"]
                        or state["claim_revision"] != item["claim"]
                    ):
                        raise ReviewEvidenceError("native pair changed before trusted tests")
                artifacts(context)
                seal_candidate(
                    home,
                    context["test_binding"],
                    Path(context["source_workspace"]),
                    policy,
                    context["source"]["repository"],
                )
                receipt = run_or_read_tests(
                    home, context["test_binding"], Path(context["source_workspace"]), policy
                )
                if receipt is None:
                    results.append({"card": card, "state": "awaiting-trusted-tests"})
                    continue
            result = finish_pair(home, directory, context, guard=guard)
            results.append({"card": card, "state": "accepted", "receipt": result})
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            results.append({"card": card, "state": "pending", "reason": str(exc)[:200]})
    return results
