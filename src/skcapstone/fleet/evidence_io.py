"""Evidence and custody helpers shared by review, production and recovery code.

Zero-behavior extraction of the helpers that the retired finish and
acceptance modules used to export. Kept small on purpose: bounded evidence
reads, native card-state prediction, and the exact producer and reviewer
terminal custody checks that surviving callers still need.
"""

import hashlib
import json
import re
import socket
import stat
import subprocess
import sys
from pathlib import Path

from skcoord.card_store import CardStore, card_mutation_lock

from ..blocked_verdict import is_outcome_key
from ..seraph_review_cardstore import LiveCardStoreGateway, _latest_outcome, card_revision
from ..seraph_review_contracts import _digest
from .production_review_evidence import ReviewEvidenceError
from .source_bundle import MAX_EVIDENCE, _once, _read, _root, _sha


def read_json(path):
    """Read only bounded, private, owned, regular evidence."""
    return json.loads(_read(Path(path), MAX_EVIDENCE))


def once(path, value):
    """Retain complete immutable structured evidence."""
    _once(Path(path), json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def native_state(home, card):
    """Read exact current custody and predict native completion for lost replies."""
    with card_mutation_lock(home, card):
        store = CardStore(home)
        row = store.fold(card)
        if row is None:
            raise ReviewEvidenceError("native card disappeared")
        snapshot = LiveCardStoreGateway(home).read_card(card, _store=store)
        after = row.model_copy(deep=True)
        after.status = type(row.status)("done")
        after.owner = None
        after.meta.pop("_claim_revision", None)
        bindings = {
            key: getattr(snapshot, key)
            for key in (
                "repository",
                "number",
                "head_sha",
                "producer_identity",
                "reviewer_identity",
                "unresolved_review",
                "candidate_evidence_sha256",
            )
        }
        native = store._read_events(card)
        events = native + store._legacy_events(card)
        events.sort(
            key=lambda e: (
                e.get("ts", ""),
                e.get("writer", ""),
                e.get("seq", 0),
                bool(e.get("expected_claim_revision")),
            )
        )
        outcomes = [
            e
            for e in events
            if e.get("action") == "verdict"
            or (e.get("action") == "link" and is_outcome_key(e.get("link_key") or e.get("key")))
        ]
        predicted = _digest(
            {
                "card": card_revision(after),
                "bindings": bindings,
                "outcome": outcomes[-1] if outcomes else {},
            }
        )
        completions = [e for e in native if e.get("action") == "complete"]
        return {
            "revision": snapshot.revision,
            "completion_revision": predicted,
            "complete_count": len(completions),
            "last_complete": completions[-1] if completions else None,
            "owner": row.owner,
            "claim_revision": row.meta.get("_claim_revision"),
            "status": row.status.value,
        }


def native_command(home, args):
    """Use this interpreter's native CLI with no unguarded fallback."""
    result = subprocess.run(
        [sys.executable, "-m", "skcapstone", "coord", *args, "--home", str(home)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        suffix = ": " + detail[-1][:180] if detail else ""
        raise ReviewEvidenceError("guarded native acceptance refused" + suffix)
    return json.loads(result.stdout) if "--json" in args else None


def artifacts(context):
    """Rehash exact collected source evidence and independent decision bytes."""
    for role in ("source", "review"):
        item = context[role]
        if _sha(_read(Path(item["evidence_path"]), MAX_EVIDENCE)) != item["evidence_sha256"]:
            raise ReviewEvidenceError("retained report changed")
    review = context["review"]
    raw = _read(Path(review["decision_path"]), MAX_EVIDENCE)
    if (
        _sha(raw) != review["decision_sha256"]
        or json.loads(raw) != review["proposal"]
        or review["proposal"]["verdict"] != "PASS"
    ):
        raise ReviewEvidenceError("independent review is not exact PASS")


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
    producer_owner = manifest.get("owner")
    producer_claim = manifest.get("claim_revision")
    if not isinstance(producer_owner, str) or not isinstance(producer_claim, str):
        raise ReviewEvidenceError("sealed producer generation is incomplete")
    matches = []
    for path in (home / "fleet/status").glob("*/dispatch/" + source.id + ".json"):
        status = read_json(path)
        if status.get("owner") != producer_owner or status.get("claim_revision") != producer_claim:
            continue
        if status.get("state") == "blocked" and status.get("claim_released") is True:
            from .released_source import terminal

            matches.append(terminal(home, source, manifest, status, path))
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
        if source.owner is None:
            from .released_source import direct_review_view

            view, _, _ = direct_review_view(home, source, manifest)
            source = view.fold(source.id)
        return _direct_seat_terminal(home, source, manifest, producer_owner, producer_claim)
    if len(matches) != 1:
        raise ReviewEvidenceError("one native producer terminal receipt required")
    return matches[0]


def deferred_direct_terminal(receipt):
    """Recognize a native successful exit awaiting fresh process-death proof.

    This is not terminal proof. Publication recovery must independently check
    the exact managed unit and absence of card processes before using it.
    """
    disposition = receipt.get("source_disposition")
    return (
        receipt.get("schema") == "skfleet.direct-seat/v1"
        and receipt.get("completion_state") == "running"
        and type(receipt.get("exit_code")) is int
        and receipt["exit_code"] == 0
        and isinstance(disposition, dict)
        and disposition.get("state") == "awaiting-evidence"
        and disposition.get("process_terminal") is False
        and disposition.get("claim_released") is False
        and disposition.get("reason") == "exact-process-and-cgroup-death-unproven"
    )


def _direct_seat_terminal(
    home, source, manifest, producer_owner, producer_claim, *, publication_pending=False
):
    """Validate a completed direct worker's exact native source receipt."""
    path = Path(home) / "fleet/direct-seats" / (producer_owner + ".json")
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
        expected_state = "awaiting-evidence" if publication_pending else "awaiting-review"
        deferred = publication_pending and deferred_direct_terminal(receipt)
        parts = source.owner.split("-")
        owner_host = parts[-2] if len(parts) >= 4 else ""
        host = str(receipt.get("host") or owner_host)
        domains = receipt.get("capacity_domains")
        if (
            schema not in (None, "skfleet.direct-seat/v1")
            or receipt.get("card") != source.id
            or receipt.get("owner") != producer_owner
            or receipt.get("claim_revision") != producer_claim
            or (receipt.get("completion_state") != expected_state and not deferred)
            or type(receipt.get("pid")) is not int
            or receipt["pid"] <= 0
            or host != owner_host
            or disposition.get("state") != expected_state
            or disposition.get("claim_released") is not False
            or (disposition.get("process_terminal") is not True and not deferred)
            or not isinstance(artifact, dict)
            or (publication_pending and (not explicit_receipt or artifact or manifest))
            or (publication_pending and type(receipt.get("exit_code")) is not int)
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
        if host != socket.gethostname().split(".")[0].lower():
            # A synced receipt is portable; terminal proof still belongs to
            # the producer's host, never the consuming review node.
            if not explicit_receipt or type(receipt.get("exit_code")) is not int:
                raise ReviewEvidenceError(
                    "remote direct producer requires modern terminal receipt"
                )
            from .production_review_custody import unit_terminal

            unit_terminal(unit, invocation, host=host)
        return {
            "receipt_kind": "direct-seat",
            "receipt_path": str(path),
            "receipt_sha256": hashlib.sha256(raw).hexdigest(),
            "card": source.id,
            "owner": producer_owner,
            "claim_revision": producer_claim,
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


def _sealed_review_policy(home, launch, card, event, current_policy):
    """Resolve the policy snapshot authenticated by this exact review launch."""
    if event.get("schema") == "skfleet.review-assignment-launch/v3":
        from .builder_dispatch import request_path
        from .paths import FleetPaths
        from .review_dispatch import _recorded, validate_contract

        execution = event.get("execution") or {}
        node = execution.get("node")
        if not isinstance(node, str):
            raise ReviewEvidenceError("sealed review request identity invalid")
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
        return sealed
    recorded = route.get("policy_sha256")
    if recorded is None:
        # Legacy local launches have no policy digest; bind acceptance to the
        # live policy and its digest in the persisted acceptance context.
        return current_policy
    if not isinstance(recorded, str) or recorded != digest(current_policy):
        raise ReviewEvidenceError("sealed review policy provenance unavailable")
    return current_policy
