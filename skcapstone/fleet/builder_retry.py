"""Explicit one-use retry of a stopped, unchanged production builder attempt."""

import copy
import hashlib
import json
import re
import socket
import subprocess
import sys
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from skcoord.card_store import CardStore, card_mutation_lock

from ..atomic_io import atomic_write_text
from ..seraph_review_cardstore import _latest_outcome
from . import builder_dispatch as builder
from . import source_bundle, store
from .paths import default_paths, valid_name

SCHEMA = "skfleet.builder-operator-retry/v1"


def encoded(value):
    """Encode immutable operator evidence deterministically."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def binding(request, status):
    """Bind the source and exact stopped attempt without mutable heartbeats."""
    return {
        **{
            key: request.get(key)
            for key in (
                "card_id",
                "request_id",
                "node",
                "repository",
                "base_ref",
                "base_revision",
                "labels",
                "production",
            )
        },
        **{
            key: status.get(key)
            for key in ("owner", "claim_revision", "attempt", "unit", "invocation")
        },
    }


def pending(request, status):
    """Recognize only an unused authorization for this awaiting-evidence attempt."""
    grant = request.get("operator_retry")
    return bool(
        isinstance(grant, dict)
        and grant.get("schema") == SCHEMA
        and re.fullmatch(r"[0-9a-f]{64}", str(grant.get("id", "")))
        and grant["id"]
        == hashlib.sha256(encoded({k: v for k, v in grant.items() if k != "id"})).hexdigest()
        and status.get("state") == "awaiting-evidence"
        and status.get("operator_retry_consumed") != grant["id"]
        and grant.get("binding") == binding(request, status)
    )


def check_attempt(paths, home, request, status, *, local=True):
    """Refuse changed custody, proposals, artifacts, workspaces or process identity."""
    card_id, node = request.get("card_id", ""), request.get("node", "")
    if (
        not re.fullmatch(r"[0-9a-f]{8}", card_id)
        or not valid_name(node)
        or request.get("schema") != "skfleet.builder-dispatch/v1"
        or status.get("schema") != "skfleet.builder-dispatch-status/v1"
        or not isinstance(request.get("production"), dict)
        or status.get("production") != request["production"]
        or any(status.get(key) != request.get(key) for key in ("card_id", "node", "request_id"))
        or status.get("state") != "awaiting-evidence"
        or status.get("claim_released")
        or type(status.get("attempt")) is not int
        or not 1 <= status["attempt"] < builder.MAX_ATTEMPTS
        or not re.fullmatch(r"[0-9a-f]{32}", str(status.get("invocation", "")))
        or status.get("unit") != builder.production_builder.unit_name(request, status["attempt"])
    ):
        raise ValueError("exact stopped production attempt required")
    native = CardStore(home)
    card = native.fold(card_id)
    if (
        card is None
        or card.archived
        or getattr(card.status, "value", card.status) != "doing"
        or "source-only" not in card.labels
        or card.owner != status.get("owner")
        or card.meta.get("_claim_revision") != status.get("claim_revision")
        or not re.fullmatch(r"[0-9a-f]{32}", str(status.get("claim_revision", "")))
        or sorted(card.labels) != request.get("labels")
        or any(
            source_bundle._binding({"meta": card.meta, "links": card.links}, key)
            != request.get(key)
            for key in ("repository", "base_ref", "base_revision")
        )
    ):
        raise ValueError("current source claim differs")
    if (
        _latest_outcome(native, card_id)
        or status.get("source_artifact")
        or any("candidate" in key or key in {"evidence", "commit_sha"} for key in card.links)
    ):
        raise ValueError("candidate or outcome must retain review custody")
    artifacts = source_bundle._root(home, card_id)
    if artifacts.exists() and any(artifacts.iterdir()):
        raise ValueError("retained source artifact forbids retry")
    if local:
        if request["production"].get("host") != socket.gethostname().split(".")[0].lower():
            raise ValueError("retry process proof belongs to another node")
        if builder._process_state(copy.deepcopy(status))[0] is not False:
            raise ValueError("exact unit death unavailable")
        source_bundle.inspect_clean_base(
            paths.root / "workspaces" / card.owner, request["base_revision"]
        )
    return binding(request, status)


def node_check(payload):
    """Read an exact frozen node attempt for the existing trusted SSH operator."""
    paths, home = default_paths(), Path.home() / ".skcapstone"
    if not store.is_frozen(paths):
        raise ValueError("freeze fleet before authorizing retry")
    node, card = payload["node"], payload["card_id"]
    if not valid_name(node) or not re.fullmatch(r"[0-9a-f]{8}", card):
        raise ValueError("invalid retry identity")
    with builder._request_exclusion(builder.request_path(paths, node, card)):
        request = builder._load(builder.request_path(paths, node, card)) or {}
        status = (
            builder._validated_status(builder.status_path(paths, node, card), paths, node) or {}
        )
        proof = check_attempt(paths, home, request, status)
        if proof != payload["binding"]:
            raise ValueError("node attempt changed")
        return {"binding": proof, "request": request, "status": status}


def remote_check(host, payload):
    """Call one fixed read-only module through existing strict operator SSH."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,100}", host):
        raise ValueError("invalid node host")
    result = subprocess.run(
        [
            "ssh",
            "-T",
            "-oBatchMode=yes",
            "-oConnectTimeout=5",
            "-oStrictHostKeyChecking=yes",
            "-oClearAllForwardings=yes",
            host,
            "~/.skenv/bin/python -m skcapstone.fleet.builder_retry",
        ],
        input=encoded(payload),
        capture_output=True,
        timeout=60,
    )
    if result.returncode or not 0 < len(result.stdout) <= source_bundle.MAX_EVIDENCE:
        raise ValueError("node retry proof unavailable")
    return json.loads(result.stdout)


def authorize(
    paths,
    home,
    node,
    card_id,
    *,
    request_id,
    claim,
    invocation,
    actor,
    reason,
    evidence,
    apply=False,
    probe=remote_check,
):
    """Renew one request with explicit one-use authority; retain its exact claim."""
    policy = builder.production_builder.policy()
    if (
        policy is None
        or policy["authority_host"] != socket.gethostname().split(".")[0].lower()
        or not store.is_frozen(paths)
    ):
        raise ValueError("frozen production authority required")
    if (
        not valid_name(node)
        or not re.fullmatch(r"[0-9a-f]{8}", card_id)
        or not re.fullmatch(r"[a-z][a-z0-9-]{0,95}", actor)
        or not isinstance(reason, str)
        or not 1 <= len(reason.strip()) <= 1024
    ):
        raise ValueError("attributed retry reason and identity required")
    evidence = Path(evidence).expanduser().absolute()
    evidence_bytes = source_bundle._read(evidence, source_bundle.MAX_EVIDENCE)
    path = builder.request_path(paths, node, card_id)
    with (
        builder._request_exclusion(paths.root / "dispatch" / ".production-offer"),
        builder._request_exclusion(path),
        store.actuation_exclusion(paths),
    ):
        request = builder._load(path) or {}
        status = (
            builder._validated_status(builder.status_path(paths, node, card_id), paths, node) or {}
        )
        if (
            request.get("request_id") != request_id
            or status.get("claim_revision") != claim
            or status.get("invocation") != invocation
            or request.get("operator_retry")
        ):
            raise ValueError("retry generation changed or already authorized")
        proof = check_attempt(paths, home, request, status, local=False)
        node_proof = probe(
            request["production"]["host"], {"node": node, "card_id": card_id, "binding": proof}
        )
        if node_proof.get("binding") != proof:
            raise ValueError("node retry proof differs")
        if not store.is_frozen(paths):
            raise ValueError("fleet freeze changed during qualification")
        check_attempt(paths, home, request, status, local=False)
        grant = {
            "schema": SCHEMA,
            "binding": proof,
            "actor": actor,
            "reason": reason,
            "evidence": str(evidence),
            "evidence_sha256": hashlib.sha256(evidence_bytes).hexdigest(),
            "authorized_at": builder._iso(builder._now()),
        }
        grant["id"] = hashlib.sha256(encoded(grant)).hexdigest()
        if not apply:
            return {"state": "qualified-check-only", "authorization": grant}
        receipt = home / "evidence/work" / card_id / "operator-retries" / (grant["id"] + ".json")
        source_bundle._once(
            receipt,
            encoded(
                {
                    "authorization": grant,
                    "request": request,
                    "status": status,
                    "node_proof": node_proof,
                }
            ),
        )
        updated = {
            **request,
            "operator_retry": grant,
            "lease_expires_at": builder._iso(
                builder._now() + timedelta(seconds=builder.LEASE_SECONDS)
            ),
        }
        atomic_write_text(path, json.dumps(updated, indent=2, sort_keys=True) + "\n")
        return {"state": "authorized-no-launch", "authorization": grant, "receipt": str(receipt)}


@contextmanager
def consume(paths, home, request, status, *, route_preflight=None):
    """Fence exact native custody through consumption and the launch call."""
    with card_mutation_lock(home, request["card_id"]):
        _consume(paths, home, request, status, route_preflight=route_preflight)
        yield


def _consume(paths, home, request, status, *, route_preflight=None):
    """Persist one-use consumption before a launcher can create its fresh unit."""
    if not pending(request, status):
        raise ValueError("unused exact retry authorization required")
    check_attempt(paths, home, request, status)
    grant = request["operator_retry"]
    receipt = (
        home
        / "evidence/work"
        / request["card_id"]
        / "operator-retries"
        / (grant["id"] + "-consumed.json")
    )
    if receipt.exists():
        raise ValueError("retry authorization already consumed")
    source_bundle._once(receipt, encoded({"authorization": grant, "stopped_status": status}))
    return builder._write_status(
        paths,
        request["node"],
        request,
        "running",
        owner=status["owner"],
        claim_revision=status["claim_revision"],
        attempt=status["attempt"] + 1,
        unit=builder.production_builder.unit_name(request, status["attempt"] + 1),
        invocation=None,
        operator_retry_consumed=grant["id"],
        route_preflight=route_preflight or status.get("route_preflight"),
        claim_released=False,
        liveness="unknown",
    )


if __name__ == "__main__":
    try:
        raw = sys.stdin.buffer.read(source_bundle.MAX_EVIDENCE + 1)
        if not 0 < len(raw) <= source_bundle.MAX_EVIDENCE:
            raise ValueError("retry request exceeds bound")
        print(json.dumps(node_check(json.loads(raw))))
    except Exception:
        raise SystemExit(1)
