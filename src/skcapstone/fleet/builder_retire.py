"""Exact terminal offer retirement with immutable complete workspace custody."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

from skcoord.card_store import CardStore, card_mutation_lock

from ..seraph_review_cardstore import card_revision
from . import builder_dispatch as builder
from . import source_bundle
from .paths import default_paths, valid_name

SCHEMA = "skfleet.builder-retirement/v1"
MAX_SOURCE = 2 * 1024**3


def encoded(value):
    """Encode stable receipt bytes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha(raw):
    """Address exact bytes."""
    return hashlib.sha256(raw).hexdigest()


def private_directory(path):
    """Create or require an owned private nonredirected evidence directory."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.stat()
    if path.resolve() != path or info.st_uid != os.getuid() or info.st_mode & 0o777 != 0o700:
        raise ValueError("private owned evidence directory required")


def directory(home, card, request_id):
    """Constrain receipts to one exact request generation."""
    if not re.fullmatch(r"[0-9a-f]{8}", card) or not re.fullmatch(r"[0-9a-f]{64}", request_id):
        raise ValueError("invalid retirement identity")
    return home / "evidence/work" / card / "terminal-offers" / request_id


def file_digest(path):
    """Hash a bounded owned regular file without following its final symlink."""
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_size > MAX_SOURCE
        ):
            raise ValueError("unsafe or oversized source file")
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inventory(workspace):
    """Hash every source entry, including Git metadata and ignored files."""
    if workspace.resolve() != workspace or not workspace.is_dir():
        raise ValueError("workspace absent or redirected")
    rows, total = {}, 0
    for root, dirs, files in os.walk(workspace, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(root) / name
            info = path.lstat()
            relative = path.relative_to(workspace).as_posix()
            row = {"mode": stat.S_IMODE(info.st_mode)}
            if info.st_uid != os.getuid():
                raise ValueError("source entry has foreign owner")
            if stat.S_ISLNK(info.st_mode):
                row.update(type="symlink", target=os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                row.update(type="directory")
            elif stat.S_ISREG(info.st_mode):
                total += info.st_size
                if total > MAX_SOURCE or len(rows) > 200000:
                    raise ValueError("workspace exceeds preservation bound")
                row.update(type="file", size=info.st_size, sha256=file_digest(path))
            else:
                raise ValueError("special source entry cannot be preserved")
            rows[relative] = row
    return rows


def archive_inventory(path):
    """Verify archive content directly without extracting any paths."""
    rows, total = {}, 0
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            total += member.size
            if total > MAX_SOURCE or len(rows) >= 200000:
                raise ValueError("archive exceeds preservation bound")
            if member.name in rows:
                raise ValueError("duplicate archive entry")
            row = {"mode": member.mode}
            if member.isfile():
                with archive.extractfile(member) as stream:
                    row.update(
                        type="file",
                        size=member.size,
                        sha256=hashlib.file_digest(stream, "sha256").hexdigest(),
                    )
            elif member.isdir():
                row.update(type="directory")
            elif member.issym():
                row.update(type="symlink", target=member.linkname)
            else:
                raise ValueError("unexpected archive entry")
            rows[member.name] = row
    return rows


def preserve(workspace, target, *, apply):
    """Preserve all workspace bytes; refuse changed or conflicting replay."""
    before = inventory(workspace)
    digest = sha(encoded(before))
    if not apply:
        return {"inventory_sha256": digest, "archive_sha256": None}
    private_directory(target)
    archive = target / "workspace.tar.gz"
    if not archive.exists():
        fd, name = tempfile.mkstemp(prefix=".archive-", dir=target)
        os.close(fd)
        temporary = Path(name)
        try:
            with tarfile.open(temporary, "w:gz", dereference=False) as tar:
                # No repository command runs and symlink targets are never read.
                for relative in sorted(before):
                    tar.add(workspace / relative, arcname=relative, recursive=False)
            if archive_inventory(temporary) != before or inventory(workspace) != before:
                raise ValueError("workspace changed during preservation")
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            os.link(temporary, archive)
        finally:
            temporary.unlink()
    if archive.is_symlink() or archive.stat().st_mode & 0o777 != 0o600:
        raise ValueError("private regular source archive required")
    archive_sha = file_digest(archive)
    if archive_inventory(archive) != before or inventory(workspace) != before:
        raise ValueError("preserved workspace differs")
    source_bundle._once(target / "inventory.json", encoded(before))
    return {"inventory_sha256": digest, "archive_sha256": archive_sha}


def preserve_absent_workspace(workspace, target, *, apply):
    """Record that failed preclaim reconstruction left no workspace to archive."""
    if workspace.exists() or workspace.is_symlink() or workspace.resolve() != workspace:
        raise ValueError("preclaim workspace is no longer absent")
    parent = workspace.parent
    if parent.exists():
        info = parent.stat()
        if parent.resolve() != parent or not parent.is_dir() or info.st_uid != os.getuid():
            raise ValueError("preclaim workspace parent is unsafe")
        if next(parent.glob(f".{workspace.name}.*"), None) is not None:
            raise ValueError("source reconstruction staging remains")
    inventory = {"workspace": "absent"}
    digest = sha(encoded(inventory))
    if apply:
        private_directory(target)
        source_bundle._once(target / "inventory.json", encoded(inventory))
    return {"inventory_sha256": digest, "archive_sha256": None, "workspace_absent": True}


def prove_dead(status):
    """Require exact process death, distinguishing denied and absent PIDs."""
    if status.get("production") is not None:
        if builder._process_state(dict(status))[0] is not False:
            raise ValueError("production process death unavailable")
        return
    pid, start = status.get("pid"), status.get("pid_start_ticks")
    if type(pid) is not int or pid < 2 or not re.fullmatch(r"[0-9]+", str(start or "")):
        raise ValueError("exact process identity unavailable")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    except OSError as exc:
        raise ValueError("process death unavailable") from exc
    current = builder._proc_start_ticks(pid)
    if current is None or current == start:
        raise ValueError("process live or identity unavailable")


def preclaim_source_failure(request, status):
    """Recognize only exhausted source preparation with no execution identity."""
    return (
        isinstance(request.get("production"), dict)
        and request["production"].get("family") in {"codex", "glm", "deepseek", "qwen"}
        and valid_name(request["production"].get("host", ""))
        and status.get("state") == "failed"
        and status.get("attempt") == builder.MAX_ATTEMPTS
        and status.get("error") == "exact source reconstruction failed"
        and all(
            status.get(key) is None
            for key in (
                "owner",
                "claim_revision",
                "pid",
                "pid_start_ticks",
                "unit",
                "invocation",
                "admission_contract",
            )
        )
    )


def claims_released_since_offer(home, request):
    """Require every claim after this offer to have an exact release event."""
    try:
        offered = datetime.fromisoformat(request["offered_at"].replace("Z", "+00:00"))
        if offered.tzinfo is None or offered > datetime.now(timezone.utc):
            raise ValueError("offer time unavailable")
        claims = []
        releases = []
        for event in CardStore(home)._read_events(request["card_id"]):
            action = event.get("action")
            if action not in {"claim", "release_claim"}:
                continue
            at = datetime.fromisoformat(event["ts"].replace("Z", "+00:00"))
            if at.tzinfo is None:
                raise ValueError("claim history unavailable")
            if action == "claim" and at >= offered:
                owner = event.get("owner")
                revision = event.get("claim_revision", event.get("event_id"))
                if (
                    not isinstance(owner, str)
                    or not owner
                    or not isinstance(revision, str)
                    or not revision
                ):
                    raise ValueError("claim history unavailable")
                claims.append((owner, revision, at))
            elif action == "release_claim" and at >= offered:
                releases.append(
                    (
                        event.get("released_owner"),
                        event.get("expected_claim_revision"),
                        at,
                    )
                )
        for owner, revision, claimed_at in claims:
            matches = [
                released_at
                for released_owner, expected_revision, released_at in releases
                if released_owner == owner
                and expected_revision == revision
                and released_at > claimed_at
            ]
            if len(matches) != 1:
                raise ValueError("post-offer claim lacks one exact release")
    except (AttributeError, KeyError, TypeError) as exc:
        raise ValueError("claim history unavailable") from exc
    return len(claims)


def prove_preclaim(home, request):
    """Require resolved claims and no service history or admission intent."""
    from . import production_admission as admission

    released_claims = claims_released_since_offer(home, request)
    units = [
        builder.production_builder.unit_name(request, attempt)
        for attempt in range(1, builder.MAX_ATTEMPTS + 1)
    ]
    for unit in units:
        state = admission.unit_state(unit, terminal=True)
        if any(
            state.get(key) != value
            for key, value in {
                "LoadState": "not-found",
                "ActiveState": "inactive",
                "SubState": "dead",
                "MainPID": "0",
                "ControlPID": "0",
                "InvocationID": "",
            }.items()
        ):
            raise ValueError("preclaim unit identity is not absent")
        history = subprocess.run(
            [
                "journalctl",
                "--user",
                "--quiet",
                "--no-pager",
                "--output=json",
                "--since=" + request["offered_at"],
                "--user-unit=" + unit,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if history.returncode or history.stdout.strip() or history.stderr.strip():
            raise ValueError("preclaim unit history exists or is unavailable")
    root = home / "fleet/resource-admission" / request["production"]["host"]
    if root.exists():
        if root.resolve() != root or not root.is_dir():
            raise ValueError("admission evidence directory is redirected")
        records = list(root.iterdir())
        if len(records) > 10000:
            raise ValueError("admission evidence exceeds bound")
        for record in records:
            if not re.fullmatch(r"[0-9a-f]{64}", record.name):
                continue
            path = record / "intent.json"
            if path.exists() or path.is_symlink():
                intent = json.loads(source_bundle._read(path, source_bundle.MAX_EVIDENCE))
                if not isinstance(intent, dict):
                    raise ValueError("admission intent is malformed")
                binding = intent.get("binding") or {}
                if (
                    not isinstance(binding, dict)
                    or intent.get("unit") in units
                    or binding.get("request_id") == request["request_id"]
                ):
                    raise ValueError("preclaim admission intent exists or is unavailable")
    return dict(
        units_absent=units,
        journal_empty=True,
        no_admission_intent=True,
        claims_resolved_since_offer=True,
        released_claim_generations=released_claims,
    )


def read_attempt(paths, node, card, request_sha256, status_sha256):
    """Read hash-pinned terminal bytes without modifying node-owned status."""
    raw_request = source_bundle._read(
        builder.request_path(paths, node, card), source_bundle.MAX_EVIDENCE
    )
    raw_status = source_bundle._read(
        builder.status_path(paths, node, card), source_bundle.MAX_EVIDENCE
    )
    if sha(raw_request) != request_sha256 or sha(raw_status) != status_sha256:
        raise ValueError("request or status hash changed")
    request, status = json.loads(raw_request), json.loads(raw_status)
    if (
        request.get("schema") != "skfleet.builder-dispatch/v1"
        or status.get("schema") != "skfleet.builder-dispatch-status/v1"
        or request.get("card_id") != card
        or request.get("node") != node
        or any(
            status.get(k) != request.get(k)
            for k in ("card_id", "node", "request_id", "production")
        )
        or status.get("state") not in {"blocked", "failed", "stale"}
        or type(status.get("attempt")) is not int
        or status["attempt"] < 1
        or (status.get("state") == "failed" and status.get("attempt", 0) < builder.MAX_ATTEMPTS)
        or not re.fullmatch(r"[0-9a-f]{64}", str(request.get("request_id", "")))
        or (
            not preclaim_source_failure(request, status)
            and (
                not re.fullmatch(r"[0-9a-f]{32}", str(status.get("claim_revision", "")))
                or not valid_name(status.get("owner", ""))
                or not status["owner"].endswith("-" + node + "-" + card)
            )
        )
    ):
        raise ValueError("exact terminal attempt required")
    return request, status, raw_request, raw_status


def check_card(home, card, request, expected):
    """Require an unchanged unclaimed native source contract under its lock."""
    row = CardStore(home).fold(card)
    if (
        row is None
        or row.owner
        or row.archived
        or row.meta.get("claim_conflicts")
        or row.status.value not in {"backlog", "ready"}
        or card_revision(row) != expected
        or sorted(row.labels) != request.get("labels")
        or builder._source({"meta": row.meta, "links": row.links})
        != tuple(request.get(k) for k in ("repository", "base_ref", "base_revision"))
    ):
        raise ValueError("unclaimed card or exact source contract changed")


def node_check(payload, *, paths=None, home=None, locked=False):
    """Preserve an exact stopped local attempt under the native request lock."""
    from . import builder_terminal

    paths, home = paths or default_paths(), home or Path.home() / ".skcapstone"
    node, card = payload["node"], payload["card_id"]
    if not valid_name(node) or not re.fullmatch(r"[0-9a-f]{8}", card):
        raise ValueError("invalid node retirement identity")
    if socket.gethostname().split(".")[0].lower() != payload["host"]:
        raise ValueError("process proof belongs to another host")
    exclusion = (
        nullcontext()
        if locked
        else builder._request_exclusion(builder.request_path(paths, node, card))
    )
    with exclusion:
        request, status, req, sts = read_attempt(
            paths, node, card, payload["request_sha256"], payload["status_sha256"]
        )
        preclaim = preclaim_source_failure(request, status)
        if preclaim:
            if request["production"]["host"] != payload["host"]:
                raise ValueError("preclaim admission belongs to another host")
            check_card(home, card, request, payload["card_sha256"])
            absence = prove_preclaim(home, request)
        else:
            builder_terminal.prove(home, status)
        target = directory(home, card, request["request_id"])
        owner = (
            f"pi-{request['production']['family']}-builder-{node}-{card}"
            if preclaim
            else status["owner"]
        )
        workspace = paths.root / "workspaces" / owner
        inspection = source_bundle._INSPECT_SETUP + """
git('merge-base','--is-ancestor',base,'HEAD')
print(json.dumps({'head':git('rev-parse','HEAD^{commit}').decode().strip(),
                  'tree':git('rev-parse','HEAD^{tree}').decode().strip()}))
"""
        if preclaim:
            inspection = source_bundle._INSPECT_SETUP + """
assert git('rev-parse','HEAD^{commit}').decode().strip()==base
assert git('remote','get-url','origin').decode().strip()==ref
assert not git('status','--porcelain','--untracked-files=all')
assert not git('ls-files','--others','--ignored','--exclude-standard')
print(json.dumps({'head':base,'tree':git('rev-parse','HEAD^{tree}').decode().strip()}))
"""
        workspace_absent = preclaim and not workspace.exists() and not workspace.is_symlink()
        if workspace_absent:
            source = {"workspace": "absent"}
            proof = preserve_absent_workspace(workspace, target, apply=payload["apply"])
        else:
            source = source_bundle._inspect(
                workspace,
                inspection,
                card,
                request["base_revision"],
                "unused",
                "unused",
                request["repository"] if preclaim else "unused",
            )
            if preclaim and source.get("head") != request["base_revision"]:
                raise ValueError("preclaim source is not the exact clean base")
            proof = preserve(workspace, target, apply=payload["apply"])
        proof["source"] = source
        if preclaim:
            check_card(home, card, request, payload["card_sha256"])
            if prove_preclaim(home, request) != absence:
                raise ValueError("preclaim absence proof changed")
            if workspace_absent:
                preserve_absent_workspace(workspace, target, apply=False)
            proof.update(preclaim=True, **absence)
        else:
            builder_terminal.prove(home, status)
        read_attempt(paths, node, card, payload["request_sha256"], payload["status_sha256"])
        if payload["apply"]:
            source_bundle._once(target / "request.json", req)
            source_bundle._once(target / "status.json", sts)
        return dict(proof, request_sha256=sha(req), status_sha256=sha(sts), process_dead=True)


def remote_check(host, payload):
    """Use strict native SSH to a fixed installed node preservation module."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,100}", host):
        raise ValueError("invalid node host")
    if host == socket.gethostname().split(".")[0].lower():
        return node_check(payload, locked=True)
    result = subprocess.run(
        [
            "ssh",
            "-T",
            "-oBatchMode=yes",
            "-oConnectTimeout=5",
            "-oStrictHostKeyChecking=yes",
            "-oClearAllForwardings=yes",
            host,
            "~/.skenv/bin/python -m skcapstone.fleet.builder_retire",
        ],
        input=encoded(payload),
        capture_output=True,
        timeout=300,
    )
    if result.returncode or not 0 < len(result.stdout) <= source_bundle.MAX_EVIDENCE:
        raise ValueError("node preservation proof unavailable")
    return json.loads(result.stdout)


def retire(
    paths,
    home,
    node,
    card,
    *,
    request_sha256,
    status_sha256,
    card_sha256,
    actor,
    reason,
    apply=False,
    probe=remote_check,
):
    """Retire only a stopped exact active pointer, preserving history and source."""
    policy = builder.production_builder.policy()
    if not policy or policy["authority_host"] != socket.gethostname().split(".")[0].lower():
        raise ValueError("production authority required")
    if (
        not valid_name(node)
        or not re.fullmatch(r"[0-9a-f]{8}", card)
        or not re.fullmatch(r"[a-z][a-z0-9-]{0,95}", actor)
        or not 1 <= len(reason.strip()) <= 1024
        or any(
            not re.fullmatch(r"[0-9a-f]{64}", v)
            for v in (request_sha256, status_sha256, card_sha256)
        )
    ):
        raise ValueError("exact retirement identity and attribution required")
    path = builder.request_path(paths, node, card)
    binding = dict(
        node=node,
        card_id=card,
        request_sha256=request_sha256,
        status_sha256=status_sha256,
        card_sha256=card_sha256,
        actor=actor,
        reason=reason,
    )
    # One global authority offer lock already serializes scheduling; no new scheduler.
    with (
        builder._request_exclusion(paths.root / "dispatch" / ".production-offer"),
        builder._request_exclusion(path),
        card_mutation_lock(home, card),
    ):
        if not path.exists():
            matches = list(
                (home / "evidence/work" / card / "terminal-offers").glob("*/retirement.json")
            )
            for match in matches:
                receipt = json.loads(source_bundle._read(match, source_bundle.MAX_EVIDENCE))
                if receipt.get("binding") == binding:
                    previous = source_bundle._read(
                        match.parent / "request.json", source_bundle.MAX_EVIDENCE
                    )
                    if receipt.get("schema") != SCHEMA or sha(previous) != request_sha256:
                        raise ValueError("retirement receipt changed")
                    check_card(home, card, json.loads(previous), card_sha256)
                    return {"state": "already-retired", "receipt": str(match)}
            raise ValueError("active request absent without exact retirement receipt")
        request, status, raw_request, raw_status = read_attempt(
            paths, node, card, request_sha256, status_sha256
        )
        check_card(home, card, request, card_sha256)
        preclaim = preclaim_source_failure(request, status)
        if preclaim:
            claims_released_since_offer(home, request)
        host = builder.production_builder.node_binding(paths, node, policy)["host"]
        payload = dict(binding, host=host, apply=apply)
        proof = probe(host, payload)
        if (
            proof.get("process_dead") is not True
            or proof.get("request_sha256") != request_sha256
            or proof.get("status_sha256") != status_sha256
            or not re.fullmatch(r"[0-9a-f]{64}", str(proof.get("inventory_sha256", "")))
            or (
                apply
                and proof.get("workspace_absent") is not True
                and not re.fullmatch(r"[0-9a-f]{64}", str(proof.get("archive_sha256", "")))
            )
            or (
                preclaim
                and any(
                    proof.get(key) is not True
                    for key in (
                        "preclaim",
                        "journal_empty",
                        "no_admission_intent",
                        "claims_resolved_since_offer",
                    )
                )
            )
        ):
            raise ValueError("exact node source custody proof required")
        check_card(home, card, request, card_sha256)
        if preclaim:
            claims_released_since_offer(home, request)
        read_attempt(paths, node, card, request_sha256, status_sha256)
        if not apply:
            return {"state": "qualified-check-only", "binding": binding, "proof": proof}
        target = directory(home, card, request["request_id"])
        private_directory(target)
        source_bundle._once(target / "request.json", raw_request)
        source_bundle._once(target / "status.json", raw_status)
        receipt_path = target / "retirement.json"
        receipt = dict(schema=SCHEMA, binding=binding, host=host, proof=proof)
        source_bundle._once(receipt_path, encoded(receipt))
        # Durable intent precedes pointer removal; exact replay can finish a crash.
        read_attempt(paths, node, card, request_sha256, status_sha256)
        check_card(home, card, request, card_sha256)
        path.unlink()
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return {
            "state": "retired",
            "receipt": str(receipt_path),
            "receipt_sha256": sha(encoded(receipt)),
        }


if __name__ == "__main__":
    print(json.dumps(node_check(json.loads(sys.stdin.buffer.read(source_bundle.MAX_EVIDENCE)))))
