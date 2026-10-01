"""One-use continuation of exact stopped source custody, never a new offer."""

import json
import os
import re
import socket
import subprocess
import sys
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

from skcoord.card_store import card_mutation_lock

from ..seraph_review_cardstore import _latest_outcome, card_revision
from . import builder_dispatch as builder
from . import builder_retire as custody
from . import builder_retry, source_bundle
from .paths import FleetPaths, default_paths, valid_name
from .worker_git import identity

SCHEMA = "skfleet.builder-continuation/v1"


def directory(home, request):
    """Address a sidecar without changing the immutable source offer."""
    return custody.directory(home, request["card_id"], request["request_id"]).parent.parent / (
        "continuations/" + request["request_id"]
    )


def original(request):
    """Remove only the node's in-memory continuation attachment."""
    return {key: value for key, value in request.items() if key != "_continuation"}


def binding(home, request, status):
    """Pin native source, original outcome and stable terminal process status."""
    raw_request = source_bundle._read(
        builder.request_path(FleetPaths(home / "fleet"), request["node"], request["card_id"]),
        source_bundle.MAX_EVIDENCE,
    )
    if json.loads(raw_request) != original(request):
        raise ValueError("immutable source request changed")
    native, card = builder_retry.check_custody(home, request, status)
    outcome = _latest_outcome(native, request["card_id"])
    if (
        not outcome
        or outcome.get("action") != "verdict"
        or outcome.get("writer") != status["owner"]
        or not str(outcome.get("verdict", "")).startswith("BLOCKED ")
        or outcome.get("candidate_commit") != request["base_revision"]
        or status.get("source_artifact")
        or request.get("operator_retry")
        or card.meta.get("claim_conflicts")
    ):
        raise ValueError("exact retained BLOCKED source generation required")
    claims = [
        event
        for event in native._read_events(request["card_id"])
        if event.get("action") == "claim"
        and (event.get("claim_revision") or event.get("event_id")) == status["claim_revision"]
    ]
    if len(claims) != 1 or str(outcome.get("ts", "")) < str(claims[0].get("ts", "")):
        raise ValueError("blocked outcome predates exact claim")
    if not re.fullmatch(
        r"[0-9a-f]{40}", str(outcome.get("candidate_tree", ""))
    ) or not re.fullmatch(
        r"refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]{0,180}", str(outcome.get("candidate_ref", ""))
    ):
        raise ValueError("blocked source identities unavailable")
    report = Path(str(outcome.get("candidate_path", "")))
    card_directory = source_bundle._root(home, request["card_id"]).parent
    info = card_directory.stat()
    if (card_directory.resolve() != card_directory or not card_directory.is_dir()
            or info.st_uid != os.getuid() or info.st_mode & 0o777 != 0o700):
        raise ValueError("private owned card evidence directory required")
    if card_directory not in report.parents:
        raise ValueError("blocked evidence custody differs")
    if report.stat().st_mode & 0o777 != 0o600:
        raise ValueError("private blocked evidence required")
    if custody.sha(source_bundle._read(report, source_bundle.MAX_EVIDENCE)) != outcome.get(
        "candidate_sha256"
    ):
        raise ValueError("blocked evidence changed")
    artifacts = source_bundle._root(home, request["card_id"])
    if artifacts.exists() and any(artifacts.iterdir()):
        raise ValueError("review source custody forbids continuation")
    return {
        "request_sha256": custody.sha(raw_request),
        "status": {key: value for key, value in status.items() if key != "heartbeat_at"},
        "card_revision": card_revision(card),
        "outcome": outcome,
        "identity": identity(status["owner"]),
    }


def source_proof(paths, request, status, target, *, apply=False):
    """Preserve every workspace byte and inspect Git only in the read-only sandbox."""
    if request["production"]["host"] != socket.gethostname().split(".")[0].lower():
        raise ValueError("continuation process belongs to another host")
    custody.prove_dead(status)
    workspace = paths.root / "workspaces" / status["owner"]
    source = source_bundle._inspect(
        workspace,
        source_bundle._INSPECT_SETUP + """
assert git('rev-parse','HEAD^{commit}').decode().strip()==base
ref=git('symbolic-ref','HEAD').decode().strip()
assert ref.startswith('refs/heads/') and ref not in ('refs/heads/main','refs/heads/master')
assert git('status','--porcelain','--untracked-files=all')
print(json.dumps({'head':base,'tree':git('rev-parse','HEAD^{tree}').decode().strip(),
                  'ref':ref,'index_sha256':hashlib.sha256((root/'.git/index').read_bytes()).hexdigest()}))
""",
        request["card_id"],
        request["base_revision"],
        "unused",
        "unused",
        "unused",
    )
    preserved = custody.preserve(workspace, target, apply=apply)
    custody.prove_dead(status)
    return {"source": source, **preserved}


def node_check(payload):
    """Run the fixed native custody probe on the execution node."""
    paths, home = default_paths(), Path.home() / ".skcapstone"
    node, card = payload["node"], payload["card_id"]
    if not valid_name(node) or not re.fullmatch(r"[0-9a-f]{8}", card):
        raise ValueError("invalid continuation identity")
    with builder._request_exclusion(builder.request_path(paths, node, card)):
        request = builder._load(builder.request_path(paths, node, card)) or {}
        status = (
            builder._validated_status(builder.status_path(paths, node, card), paths, node) or {}
        )
        if binding(home, request, status) != payload["binding"]:
            raise ValueError("node continuation generation changed")
        proof = source_proof(
            paths, request, status, directory(home, request), apply=payload["apply"]
        )
        expected = payload["binding"]["outcome"]
        if any(
            proof["source"][key] != expected.get("candidate_" + key) for key in ("tree", "ref")
        ):
            raise ValueError("preserved Git identities differ from blocked outcome")
        if binding(home, request, status) != payload["binding"]:
            raise ValueError("node continuation claim changed")
        return proof


def remote_check(host, payload):
    """Use the existing strict operator SSH transport and a fixed native entrypoint."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,100}", host):
        raise ValueError("invalid execution host")
    result = subprocess.run(
        [
            "ssh",
            "-T",
            "-oBatchMode=yes",
            "-oConnectTimeout=5",
            "-oStrictHostKeyChecking=yes",
            "-oClearAllForwardings=yes",
            host,
            "~/.skenv/bin/python -m skcapstone.fleet.builder_continue",
        ],
        input=custody.encoded(payload),
        capture_output=True,
        timeout=300,
    )
    if result.returncode or not 0 < len(result.stdout) <= source_bundle.MAX_EVIDENCE:
        raise ValueError("node continuation proof unavailable")
    return json.loads(result.stdout)


def authorize(
    paths,
    home,
    node,
    card,
    *,
    request_id,
    claim,
    invocation,
    actor,
    reason,
    apply=False,
    probe=remote_check
):
    """Authorize one explicit fresh session while retaining the offer and claim."""
    policy = builder.production_builder.policy()
    if not policy or policy["authority_host"] != socket.gethostname().split(".")[0].lower():
        raise ValueError("production authority required")
    if not valid_name(node) or not re.fullmatch(r"[0-9a-f]{8}", card):
        raise ValueError("invalid continuation identity")
    if (
        not valid_name(actor)
        or not isinstance(reason, str)
        or not 1 <= len(reason.strip()) <= 1024
    ):
        raise ValueError("attributed continuation reason required")
    path = builder.request_path(paths, node, card)
    with builder._request_exclusion(path), card_mutation_lock(home, card):
        request = builder._load(path) or {}
        status = (
            builder._validated_status(builder.status_path(paths, node, card), paths, node) or {}
        )
        if (request.get("request_id"), status.get("claim_revision"), status.get("invocation")) != (
            request_id,
            claim,
            invocation,
        ):
            raise ValueError("continuation generation changed")
        bound = binding(home, request, status)
        target = directory(home, request)
        if (target / "grant.json").exists() or (target / "consumed.json").exists():
            raise ValueError("continuation already authorized or consumed")
        proof = probe(
            request["production"]["host"],
            {
                "node": node,
                "card_id": card,
                "binding": bound,
                "apply": apply,
            },
        )
        if not re.fullmatch(r"[0-9a-f]{64}", str(proof.get("inventory_sha256", ""))) or (
            apply and not re.fullmatch(r"[0-9a-f]{64}", str(proof.get("archive_sha256", "")))
        ):
            raise ValueError("complete source preservation required")
        if binding(home, request, status) != bound or builder._load(path) != request:
            raise ValueError("continuation source changed during authorization")
        grant = {
            "schema": SCHEMA,
            "binding": bound,
            "proof": proof,
            "actor": actor,
            "reason": reason,
            "expires_at": builder._iso(builder._now() + timedelta(hours=1)),
        }
        grant["id"] = custody.sha(custody.encoded(grant))
        if not apply:
            return {"state": "qualified-check-only", "grant": grant}
        custody.private_directory(target)
        source_bundle._once(target / "grant.json", custody.encoded(grant))
        return {
            "state": "authorized-no-launch",
            "grant": grant,
            "receipt": str(target / "grant.json"),
        }


def attach(home, request, status):
    """Load only an unconsumed exact grant into memory; never rewrite the request."""
    if request.get("production") is None or status.get("state") != "awaiting-evidence":
        return False
    target = directory(home, request)
    if status.get("state") != "awaiting-evidence" or not (target / "grant.json").exists():
        return False
    if (target / "consumed.json").exists():
        return False
    custody.private_directory(target)
    if (target / "grant.json").stat().st_mode & 0o777 != 0o600:
        raise ValueError("private continuation grant required")
    grant = json.loads(source_bundle._read(target / "grant.json", source_bundle.MAX_EVIDENCE))
    if grant.get("schema") != SCHEMA or grant.get("id") != custody.sha(
        custody.encoded({key: value for key, value in grant.items() if key != "id"})
    ):
        raise ValueError("continuation grant changed")
    request["_continuation"] = grant
    return True


def check_attempt(paths, home, request, status):
    """Recheck exact grant, source bytes, expiry and death before every consumption."""
    grant = request["_continuation"]
    if builder._iso(builder._now()) > grant["expires_at"]:
        raise ValueError("continuation expired")
    if binding(home, request, status) != grant["binding"]:
        raise ValueError("continuation custody changed")
    target = directory(home, request)
    if (target / "consumed.json").exists():
        raise ValueError("continuation consumed")
    custody.private_directory(target)
    if json.loads(source_bundle._read(target / "grant.json", source_bundle.MAX_EVIDENCE)) != grant:
        raise ValueError("continuation grant changed after attachment")
    proof = source_proof(paths, request, status, target)
    if any(proof[key] != grant["proof"][key] for key in ("source", "inventory_sha256")):
        raise ValueError("continuation workspace changed")
    archive = target / "workspace.tar.gz"
    if archive.stat().st_mode & 0o777 != 0o600:
        raise ValueError("private preserved archive required")
    if custody.file_digest(archive) != grant["proof"]["archive_sha256"]:
        raise ValueError("continuation preservation changed")


@contextmanager
def consume(paths, home, request, status, *, route_preflight=None):
    """Persist one-use consumption before invoking the existing native launcher."""
    with card_mutation_lock(home, request["card_id"]):
        check_attempt(paths, home, request, status)
        grant = request["_continuation"]
        source_bundle._once(directory(home, request) / "consumed.json", custody.encoded(grant))
        builder._write_status(
            paths,
            request["node"],
            request,
            "running",
            owner=status["owner"],
            claim_revision=status["claim_revision"],
            attempt=status["attempt"] + 1,
            unit=builder.production_builder.unit_name(request, status["attempt"] + 1),
            invocation=None,
            continuation_consumed=grant["id"],
            claim_released=False,
            route_preflight=route_preflight or status.get("route_preflight"),
            liveness="unknown",
        )
        yield


def original_outcome_pending(home, request, status):
    """An old BLOCKED outcome cannot release a continued generation's custody."""
    if not status.get("continuation_consumed"):
        return False
    try:
        grant = json.loads(
            source_bundle._read(
                directory(home, request) / "consumed.json", source_bundle.MAX_EVIDENCE
            )
        )
        if grant.get("id") != status["continuation_consumed"]:
            return True
        return (
            _latest_outcome(builder.CardStore(home), request["card_id"])
            == grant["binding"]["outcome"]
        )
    except (OSError, ValueError, KeyError, TypeError):
        return True  # Lost receipts retain this card without stopping unrelated work.


if __name__ == "__main__":
    raw = sys.stdin.buffer.read(source_bundle.MAX_EVIDENCE + 1)
    if not 0 < len(raw) <= source_bundle.MAX_EVIDENCE:
        raise SystemExit("continuation request exceeds bound")
    print(json.dumps(node_check(json.loads(raw))))
