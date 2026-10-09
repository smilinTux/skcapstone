"""Authority-only bridge from stopped independent review to tested completion."""

import base64
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
from pathlib import Path

from skcoord.card_store import CardStore

from ..review_verdict import _source_only_applicability, is_review_card
from ..seraph_review_cardstore import LiveCardStoreGateway, _latest_outcome
from .production_receipts import production_receipt_allowed
from .production_review import producer_family, review_family_allowed
from .production_review_custody import read_exit, unit_terminal
from .production_review_evidence import ReviewEvidenceError, inspect_proposal
from .production_review_finish import artifacts, finish_pair, native_state, once, read_json
from .source_bundle import (
    MAX_EVIDENCE,
    _binding,
    _once,
    _read,
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
    producer = manifest["owner"]
    claim = manifest["claim_revision"]
    matches = []
    for path in (home / "fleet/status").glob("*/dispatch/" + source.id + ".json"):
        status = read_json(path)
        if status.get("owner") != producer or status.get("claim_revision") != claim:
            continue
        artifact = status.get("source_artifact") or {}
        if (
            status.get("schema") != "skfleet.builder-dispatch-status/v1"
            or status.get("node") != path.parent.parent.name
            or status.get("card_id") != source.id
            or status.get("state") != "awaiting-review"
            or not (
                type(status.get("exit_code")) is int
                and status["exit_code"] == 0
                or status.get("exit_code") is None
                and status.get("terminal_proof") == "qualified-terminal"
            )
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
    if not matches:
        return _direct_seat_terminal(home, source, manifest)
    if len(matches) != 1:
        raise ReviewEvidenceError("one native producer terminal receipt required")
    return matches[0]


def _direct_seat_terminal(home, source, manifest):
    """Validate a completed direct worker's exact native source receipt."""
    producer = manifest["owner"]
    claim = manifest["claim_revision"]
    path = Path(home) / "fleet/direct-seats" / (producer + ".json")
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise ReviewEvidenceError("direct producer terminal receipt is not private")
        raw = path.read_bytes()
        if len(raw) > 262144:
            raise ReviewEvidenceError("direct producer terminal receipt is oversized")
        receipt = json.loads(raw)
        disposition = receipt.get("source_disposition") or {}
        artifact = disposition.get("source_artifact") or {}
        schema = receipt.get("schema")
        explicit_receipt = schema == "skfleet.direct-seat/v1"
        parts = producer.split("-")
        owner_host = parts[-2] if len(parts) >= 4 else ""
        host = str(receipt.get("host") or owner_host)
        domains = receipt.get("capacity_domains")
        if (
            schema not in (None, "skfleet.direct-seat/v1")
            or receipt.get("card") != source.id
            or receipt.get("owner") != producer
            or receipt.get("claim_revision") != claim
            or receipt.get("completion_state") != "awaiting-review"
            or type(receipt.get("pid")) is not int
            or receipt["pid"] <= 0
            or host != owner_host
            or host != socket.gethostname().split(".")[0].lower()
            or disposition.get("state") != "awaiting-review"
            or disposition.get("claim_released") is not False
            or disposition.get("process_terminal") is not True
            or not isinstance(artifact, dict)
            or any(artifact.get(key) != value for key, value in manifest.items())
            or receipt.get("route_schema") != "skfleet.runtime-route/v1"
            or not receipt.get("logical_route")
            or not receipt.get("provider")
            or not isinstance(domains, list)
            or not domains
            or any(not isinstance(domain, str) or not domain for domain in domains)
            or not receipt.get("model_or_bucket")
            or ("exit_code" in receipt and receipt["exit_code"] != 0)
            or (
                explicit_receipt
                and (receipt.get("exit_code") != 0 or not receipt.get("invocation"))
            )
        ):
            raise ReviewEvidenceError("direct producer terminal custody differs")
        invocation = str(receipt.get("invocation") or "")
        if invocation and not re.fullmatch(r"[0-9a-f]{32}", invocation):
            raise ReviewEvidenceError("direct producer invocation invalid")
        lane = str(receipt.get("lane") or (parts[1] if len(parts) >= 4 else ""))
        unit = str(receipt.get("unit") or f"skfleet-worker-{lane}-{source.id}.service")
        if (
            unit != f"skfleet-worker-{lane}-{source.id}.service"
            or explicit_receipt
            and (receipt.get("host") != host or receipt.get("lane") != lane)
        ):
            raise ReviewEvidenceError("direct producer unit differs")
        artifact_path = artifact.get("manifest")
        artifact_sha256 = artifact.get("manifest_sha256")
        if bool(artifact_path) != bool(artifact_sha256):
            raise ReviewEvidenceError("direct producer manifest reference differs")
        if artifact_path:
            expected_path = _root(home, source.id) / (manifest["head"] + ".json")
            if artifact_path != str(expected_path):
                raise ReviewEvidenceError("direct producer manifest path differs")
            raw_manifest = _read(expected_path, MAX_EVIDENCE)
            if hashlib.sha256(raw_manifest).hexdigest() != artifact_sha256:
                raise ReviewEvidenceError("direct producer manifest digest differs")
        return {
            "receipt_kind": "direct-seat",
            "receipt_path": str(path),
            "receipt_sha256": hashlib.sha256(raw).hexdigest(),
            "card": source.id,
            "owner": producer,
            "claim_revision": claim,
            "pid": receipt["pid"],
            "unit": unit,
            "invocation": invocation or None,
            "host": host,
            "family": domains[0],
        }
    except (OSError, ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, ReviewEvidenceError):
            raise
        raise ReviewEvidenceError("direct producer terminal receipt unavailable") from exc


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


def _sealed_review_outcome(store, row, owner, claim):
    """Prove the terminal reviewer PASS against its original native claim."""
    claims = [
        event
        for event in store._read_events(row.id)
        if event.get("action") == "claim"
        and event.get("writer") == owner
        and (event.get("claim_revision") or event.get("event_id")) == claim
    ]
    outcome = _latest_outcome(store, row.id)
    value = str(outcome.get("verdict") or outcome.get("link_value") or "").strip()
    if (
        len(claims) != 1
        or outcome.get("action") not in {"verdict", "link"}
        or outcome.get("writer") != owner
        or outcome.get("expected_claim_revision") not in (None, claim)
        or value.split(maxsplit=1)[0].upper() != "PASS"
        or str(outcome.get("ts", "")) < str(claims[0].get("ts", ""))
    ):
        raise ReviewEvidenceError("sealed independent review outcome is no longer current")
    return outcome


def _sealed_review_policy(home, launch, card, event, current_policy):
    """Resolve the exact policy snapshot bound to the completed review launch."""
    if event.get("schema") == "skfleet.review-assignment-launch/v3":
        from .builder_dispatch import request_path
        from .paths import FleetPaths
        from .review_dispatch import _recorded, validate_contract

        execution = event.get("execution") or {}
        node = execution.get("node")
        if not isinstance(node, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", node):
            raise ReviewEvidenceError("sealed review policy request identity invalid")
        request = read_json(request_path(FleetPaths(Path(home) / "fleet"), node, card.id))
        sealed = request.get("policy")
        policy_sha256 = (request.get("production") or {}).get("policy_sha256")
        if (
            request.get("card_id") != card.id
            or request.get("reviewer") != launch.get("owner")
            or request.get("request_id") != execution.get("request_id")
            or digest(request) != execution.get("request_sha256")
            or not isinstance(sealed, dict)
            or digest(sealed) != policy_sha256
            or policy_sha256 != execution.get("policy_sha256")
        ):
            raise ReviewEvidenceError("sealed review policy digest differs from launch")
        _recorded(home, request)
        validate_contract(request, sealed, host=launch["host"], historical=True)
        return sealed

    route = event.get("route_identity") or {}
    sealed = route.get("policy_snapshot")
    if isinstance(sealed, dict) and route.get("policy_sha256") == digest(sealed):
        from .production_policy import validate_execution_policy

        try:
            validate_execution_policy(sealed)
        except ValueError as exc:
            raise ReviewEvidenceError("sealed review policy snapshot is invalid") from exc
        return sealed
    # Older local reviews did not retain the policy body. They can be replayed
    # only if the exact policy digest is still the live one.
    recorded = route.get("policy_sha256")
    if not isinstance(recorded, str) or recorded != digest(current_policy):
        raise ReviewEvidenceError("sealed review policy provenance unavailable")
    return current_policy


def _sealed_context_policy(home, context, current_policy):
    """Revalidate a retained acceptance against the review's own launch policy."""
    store = CardStore(home)
    review_item = context["review"]
    review = store.fold(review_item["card"])
    terminal = review_item["terminal"]
    launch = {
        "host": terminal["host"],
        "owner": review_item["owner"],
        "revision": review_item["claim"],
        "model": terminal["model"],
        "lane": terminal["lane"],
    }
    if review is None:
        raise ReviewEvidenceError("sealed review card disappeared")
    event = review_item["launch_event"]
    sealed = _sealed_review_policy(home, launch, review, event, current_policy)
    if context.get("schema") == "skfleet.production-acceptance-context/v2":
        if context.get("policy_sha256") != digest(sealed):
            raise ReviewEvidenceError("sealed review policy differs from acceptance context")
    elif context.get("schema") != "skfleet.production-acceptance-context/v1":
        raise ReviewEvidenceError("acceptance context schema is unsupported")
    if not production_receipt_allowed(home, sealed, launch, review, event):
        raise ReviewEvidenceError("sealed review launch receipt is no longer valid")
    return sealed


def _sealed_source_outcome(store, source_id, manifest):
    """Prove the exact producer verdict retained by the sealed review bundle."""
    owner = manifest.get("owner")
    claim = manifest.get("claim_revision")
    if not isinstance(owner, str) or not owner or not re.fullmatch(r"[0-9a-f]{32}", str(claim)):
        raise ReviewEvidenceError("sealed source claim binding invalid")
    events = store._read_events(source_id)
    claims = [
        event
        for event in events
        if event.get("action") == "claim"
        and event.get("writer") == owner
        and (event.get("claim_revision") or event.get("event_id")) == claim
    ]
    outcomes = [
        event
        for event in events
        if event.get("action") == "verdict"
        and event.get("verdict") == "PASS_FOR_REVIEW"
        and event.get("writer") == owner
        and event.get("expected_claim_revision") in (None, claim)
        and event.get("candidate_commit") == manifest.get("head")
        and event.get("candidate_tree") == manifest.get("tree")
        and event.get("candidate_ref") == manifest.get("ref")
        and event.get("candidate_sha256") == manifest.get("evidence_sha256")
    ]
    latest = _latest_outcome(store, source_id)
    if (
        len(claims) != 1
        or len(outcomes) != 1
        or str(outcomes[0].get("ts", "")) < str(claims[0].get("ts", ""))
        or latest.get("event_id") != outcomes[0].get("event_id")
    ):
        raise ReviewEvidenceError("sealed producer outcome is no longer current")
    return outcomes[0]


def _review_attempt_is_current(home, card, core, parent, head, manifest):
    """Validate deterministic review lineage without requiring live source custody."""
    from ..review_work_identity import review_card_id

    generation = _binding(core, "link_card_generation")
    attempt = _binding(core, "review_attempt") or ""
    if (
        generation
        and review_card_id(
            parent, head, generation, manifest["evidence_sha256"], review_attempt=attempt
        )
        != card
    ):
        raise ReviewEvidenceError("review card does not match sealed source generation")
    if attempt and not generation:
        raise ReviewEvidenceError("replacement review source generation missing")
    try:
        from ..review_replacement import _record_binding
    except ModuleNotFoundError as exc:
        if exc.name != "skcapstone.review_replacement":
            raise ReviewEvidenceError("review replacement dependency failed") from exc
        if attempt or _binding(core, "superseded_by"):
            raise ReviewEvidenceError("review replacement authority dependency missing") from None
        return
    if attempt:
        predecessor = _binding(core, "review_predecessor")
        lineage = _record_binding(Path(home), predecessor or "", live_source=False)
        if lineage.get("review_card_id") != card or lineage.get("review_attempt") != attempt:
            raise ReviewEvidenceError("review replacement lineage is not current")
    elif _record_binding(Path(home), card, live_source=False).get("review_card_id") != card:
        raise ReviewEvidenceError("review was superseded by an authorized replacement")


def terminal_guard(home, context, process_check):
    """Recheck exact recorded workers; no receipt grants permission to stop them."""
    for role in ("source", "review"):
        item = context[role]
        process = process_check(item["card"])
        if process.get("sessions") or process.get("units"):
            raise ReviewEvidenceError("native worker remains active")
        terminal = item["terminal"]
        if terminal.get("receipt_kind") == "direct-seat" and role == "source":
            path = Path(home) / "fleet/direct-seats" / (item["owner"] + ".json")
            try:
                raw = path.read_bytes()
            except OSError as exc:
                raise ReviewEvidenceError("direct producer terminal receipt disappeared") from exc
            if str(path) != terminal.get("receipt_path") or hashlib.sha256(
                raw
            ).hexdigest() != terminal.get("receipt_sha256"):
                raise ReviewEvidenceError("direct producer terminal receipt changed")
            receipt = json.loads(raw)
            disposition = receipt.get("source_disposition") or {}
            if (
                receipt.get("card") != terminal.get("card")
                or receipt.get("owner") != terminal.get("owner")
                or receipt.get("claim_revision") != terminal.get("claim_revision")
                or receipt.get("pid") != terminal.get("pid")
                or disposition.get("process_terminal") is not True
                or disposition.get("claim_released") is not False
            ):
                raise ReviewEvidenceError("direct producer terminal receipt changed")
            if terminal.get("invocation"):
                unit_terminal(terminal["unit"], terminal["invocation"], host=terminal["host"])
        else:
            unit_terminal(terminal["unit"], terminal["invocation"], host=terminal["host"])


def collect(home, policy, card, claim, *, process_check):
    """Capture an exact native pair and committed proposal before any mutation."""
    home = Path(home)
    directory = review_directory(home, card, claim)
    store = CardStore(home)
    review = store.fold(card)
    terminal = read_exit(home, card, claim)
    review_owner = terminal.get("owner")
    if (
        review is None
        or not isinstance(review_owner, str)
        or not review_owner
        or (review.owner is not None and review.owner != review_owner)
        or (review.owner is not None and review.meta.get("_claim_revision") != claim)
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
        or source.status.value in {"done", "archived"}
    ):
        raise ReviewEvidenceError("original producer custody unavailable")
    gateway = LiveCardStoreGateway(home)
    source_snapshot, review_snapshot = gateway.read_card(parent), gateway.read_card(card)
    review_outcome = _sealed_review_outcome(store, review, review_owner, claim)
    repository = _binding(core, "repository")
    selected = _review_manifest(core, repository, head)
    if selected is None:
        raise ReviewEvidenceError("exact unpublished source bundle required")
    manifest, _ = selected
    source_revision = _binding(core, "source_revision")
    if not re.fullmatch(r"[0-9a-f]{64}", str(source_revision or "")):
        raise ReviewEvidenceError("sealed source snapshot binding invalid")
    if source.meta.get("claim_conflicts") or (
        source.owner is not None
        and (
            source.owner != manifest["owner"]
            or source.meta.get("_claim_revision") != manifest["claim_revision"]
        )
    ):
        raise ReviewEvidenceError("a different producer claim is active")
    _review_attempt_is_current(home, card, core, parent, head, manifest)
    outcome = _sealed_source_outcome(store, parent, manifest)
    if (
        source_snapshot.verdict != "PASS_FOR_REVIEW"
        or source_snapshot.head_sha != head
        or outcome.get("action") != "verdict"
        or outcome.get("writer") != manifest["owner"]
        or _binding(core, "producer_identity") != manifest["owner"]
        or manifest["owner"] == review_owner
        or review_snapshot.verdict != "PASS"
        or review_snapshot.unresolved_review
        or review_outcome.get("writer") != review_owner
        or not _source_only_applicability(card, home)
    ):
        raise ReviewEvidenceError("native proposal or source-only applicability differs")
    if manifest["tree"] != outcome.get("candidate_tree"):
        raise ReviewEvidenceError("source bundle belongs to another claim")
    if terminal["owner"] != review_owner or terminal["source_head"] != head:
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
        "owner": review_owner,
        "revision": claim,
        "model": terminal["model"],
        "lane": terminal["lane"],
    }
    if len(launches) != 1:
        raise ReviewEvidenceError("exact sealed independent review launch missing")
    review_policy = _sealed_review_policy(home, launch, review, launches[0], policy)
    if not production_receipt_allowed(home, review_policy, launch, review, launches[0]):
        raise ReviewEvidenceError("exact sealed independent review launch missing")
    domain = launches[0]["route_identity"]["capacity_domains"][0]
    source_terminal = _producer_terminal(home, source, manifest)
    if not review_family_allowed(
        producer_family(manifest["owner"], source_terminal["family"]), domain, review.labels
    ):
        raise ReviewEvidenceError("review family is not independent")
    review_workspace = Path(terminal["workspace"])
    if launches[0].get("schema") == "skfleet.review-assignment-launch/v3":
        from .builder_dispatch import request_path, status_path
        from .paths import FleetPaths
        from .review_dispatch import proposal_binding
        from .source_bundle import import_review_packet, load_review_packet

        paths = FleetPaths(home / "fleet")
        execution = launches[0]["execution"]
        status = read_json(status_path(paths, execution["node"], card))
        request = read_json(request_path(paths, execution["node"], card))
        if (
            status.get("state") != "awaiting-review-acceptance"
            or status.get("terminal") != terminal
            or terminal.get("execution") != execution
        ):
            raise ReviewEvidenceError("remote terminal custody differs")
        unit_terminal(execution["unit"], execution["invocation"], host=execution["host"])
        review_workspace = directory / "imported-review"
        if not review_workspace.exists():
            if not import_review_source(core, repository, head, review_workspace):
                raise ReviewEvidenceError("remote review source import unavailable")
        import_review_packet(
            home,
            load_review_packet(home, status["review_packet"], card),
            review_workspace,
            proposal_binding(request),
            execution=execution,
        )
    proposal = inspect_proposal(
        review_workspace,
        card=card,
        parent_card=parent,
        source_head=head,
        source_tree=manifest["tree"],
        reviewer_identity=review_owner,
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
        "owner": manifest["owner"],
        "claim": manifest["claim_revision"],
        "revision": source_revision,
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
        "owner": review_owner,
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
        "source_owner": manifest["owner"],
        "source_claim_revision": source_item["claim"],
        "source_head": head,
        "source_tree": manifest["tree"],
        "source_revision": source_snapshot.revision,
        "criteria_sha256": digest(source.acceptance_criteria),
    }
    context = {
        "schema": "skfleet.production-acceptance-context/v2",
        "controller": "fleet-review-closer@" + policy["authority_host"],
        "policy_sha256": digest(review_policy),
        "source": source_item,
        "review": review_item,
        "test_binding": binding,
        "source_workspace": str(workspace),
    }
    terminal_guard(home, context, process_check)
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
    from .production_review_custody import import_remote_exits

    import_remote_exits(home, policy)
    results = []
    for path in sorted((home / "evidence/production-review-exits").glob("*.json")):
        match = re.fullmatch(r"([0-9a-f]{8})-([0-9a-f]{32})\.json", path.name)
        if not match:
            continue
        card, claim = match.groups()
        directory = review_directory(home, card, claim)
        try:
            negative_path = directory / "negative-disposition.json"
            if negative_path.exists():
                results.append(read_json(negative_path))
                continue
            from ..review_verdict import recorded_verdict

            verdict = recorded_verdict(card, home)
            if verdict == "FAIL" or str(verdict).startswith("BLOCKED "):
                results.append(finish_remote_disposition(home, policy, card, claim))
                continue
            context_path = directory / "context.json"
            context = (
                read_json(context_path)
                if context_path.exists()
                else collect(home, policy, card, claim, process_check=process_check)
            )
            _sealed_context_policy(home, context, policy)

            def guard():
                return terminal_guard(home, context, process_check)

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


def finish_remote_disposition(home, policy, card_id, claim):
    """Accept real negative review evidence without completing its producer."""
    from ..review_verdict import recorded_verdict
    from .builder_dispatch import request_path, status_path
    from .paths import FleetPaths
    from .production_review_finish import native_command
    from .review_dispatch import proposal_binding
    from .source_bundle import import_review_packet, inspect_remote_review, load_review_packet

    home = Path(home)
    if socket.gethostname().split(".")[0].lower() != policy["authority_host"]:
        raise ReviewEvidenceError("review disposition is authority-only")
    cards = CardStore(home)
    card = cards.fold(card_id)
    terminal = read_exit(home, card_id, claim)
    events = [
        e
        for e in cards._read_events(card_id)
        if e.get("action") == "review_assignment_launch"
        and e.get("claim_revision") == claim
        and e.get("launched") is True
    ]
    launch = dict(
        host=terminal["host"],
        owner=terminal["owner"],
        revision=claim,
        model=terminal["model"],
        lane=terminal["lane"],
    )
    if (
        len(events) != 1
        or events[0].get("schema") != "skfleet.review-assignment-launch/v3"
        or not production_receipt_allowed(home, policy, launch, card, events[0])
    ):
        raise ReviewEvidenceError("negative review launch provenance unavailable")
    execution = events[0]["execution"]
    paths = FleetPaths(home / "fleet")
    request = read_json(request_path(paths, execution["node"], card_id))
    status = read_json(status_path(paths, execution["node"], card_id))
    if (
        status.get("state") not in {"review-fail", "review-blocked"}
        or status.get("terminal") != terminal
        or terminal.get("execution") != execution
        or card.archived
        or {"hold", "do-not-claim"}.intersection(card.labels)
    ):
        raise ReviewEvidenceError("negative review terminal custody unavailable")
    unit_terminal(execution["unit"], execution["invocation"], host=execution["host"])
    directory = review_directory(home, card_id, claim)
    workspace = directory / "imported-review"
    if not workspace.exists() and not import_review_source(
        card.model_dump(mode="json"), request["repository"], request["source"]["head"], workspace
    ):
        raise ReviewEvidenceError("negative review candidate unavailable")
    import_review_packet(
        home,
        load_review_packet(home, status["review_packet"], card_id),
        workspace,
        proposal_binding(request),
        execution=execution,
    )
    proposal = inspect_remote_review(workspace, **proposal_binding(request))
    verdict = recorded_verdict(card_id, home)
    expected = proposal["proposal"]["verdict"].split()[0]
    _current_outcome(cards, card)
    if (
        expected not in {"FAIL", "BLOCKED"}
        or not verdict
        or (verdict != "FAIL" if expected == "FAIL" else not verdict.startswith("BLOCKED "))
        or card.links.get("reviewer_evidence_sha256") != proposal["report_sha256"]
    ):
        raise ReviewEvidenceError("negative native verdict differs from committed evidence")
    source_before = native_state(home, request["source"]["card"])
    before = native_state(home, card_id)
    # Existing native completion validates FAIL/structured BLOCKED. No link or
    # completion command is issued against the producer in this branch.
    native_command(
        home,
        [
            "complete",
            card_id,
            "--agent",
            card.owner,
            "--expected-source-revision",
            before["revision"],
            "--expected-claim-revision",
            claim,
        ],
    )
    if native_state(home, request["source"]["card"]) != source_before:
        raise ReviewEvidenceError("producer changed during negative review acceptance")
    result = dict(
        card=card_id,
        state="review-" + expected.lower(),
        source_revision=source_before["revision"],
        execution=execution,
        report_sha256=proposal["report_sha256"],
    )
    once(directory / "negative-disposition.json", result)
    return result
