"""Native, receipt-backed refresh for execution-only stale test profiles."""

from __future__ import annotations

import fcntl
import json
import os
import socket
from contextlib import contextmanager
from pathlib import Path

from skcoord.card_store import CardStore

from ..seraph_review_cardstore import card_revision
from . import production_test_plan as plan
from . import production_test_profile as profile
from . import production_tests as tests


def _private_root(home: Path) -> Path:
    root = home / "fleet/profile-requalifications"
    plan.private_dir(root, create=True)
    return root


@contextmanager
def _lock(root: Path):
    path = root / ".lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def _records(root: Path):
    for path in sorted(root.glob("*.job.json")):
        value = json.loads(plan.read_private(path))
        done = path.with_name(path.stem + ".done.json")
        failed = path.with_name(path.stem + ".failed.json")
        if not done.exists() and not failed.exists():
            yield path, value


def retains_pending_claim(home: Path, card_id: str, owner: str, claim_revision: str) -> bool:
    """Preserve only the exact card claim bound to an unfinished refresh job."""
    root = Path(home) / "fleet/profile-requalifications"
    try:
        plan.private_dir(root)
    except FileNotFoundError:
        return False
    for _, job in _records(root):
        if (
            job.get("schema") == "skfleet.profile-requalification/v1"
            and job.get("card") == card_id
            and job.get("owner") == owner
            and job.get("claim_revision") == claim_revision
        ):
            return True
    return False


def requalify_or_advance(
    home: Path,
    policy: dict,
    skc: str,
    actor: str,
    *,
    card_id: str | None = None,
    core: dict | None = None,
    workspace: str | None = None,
    owner: str | None = None,
    claim_revision: str | None = None,
) -> str:
    """Start or advance at most one exact, fingerprint-only qualification."""
    home = Path(home)
    if socket.gethostname().split(".")[0].lower() != policy.get("authority_host"):
        return "not-authority"
    root = _private_root(home)
    with _lock(root) as acquired:
        if not acquired:
            return "busy"
        pending = list(_records(root))
        if pending:
            path, job = pending[0]
            try:
                state = _advance(home, policy, skc, actor, path, job)
            except (OSError, ValueError) as exc:
                plan.write_once(
                    path.with_name(path.stem + ".failed.json"),
                    {
                        "schema": "skfleet.profile-requalification-failure/v1",
                        "card": job.get("card"),
                        "reason": str(exc)[:240],
                    },
                )
                state = "failed"
            if card_id is not None and card_id != job["card"]:
                return "busy"
            return state
        if card_id is None or not all((core, workspace, owner, claim_revision)):
            return "idle"
        try:
            return _begin(
                home, policy, skc, actor, card_id, core, Path(workspace), owner, claim_revision
            )
        except (OSError, ValueError) as exc:
            for path, job in _records(root):
                if job.get("card") == card_id and job.get("claim_revision") == claim_revision:
                    plan.write_once(
                        path.with_name(path.stem + ".failed.json"),
                        {
                            "schema": "skfleet.profile-requalification-failure/v1",
                            "card": card_id,
                            "reason": str(exc)[:240],
                        },
                    )
                    return "failed"
            return "blocked:" + str(exc)[:180]


def _begin(home, policy, skc, actor, card_id, core, workspace, owner, claim_revision):
    expected = profile.contract(dict(core, id=card_id))
    value, predecessor = profile.read_profile(home, card_id)
    card = CardStore(home).fold(card_id)
    if (
        card is None
        or card.status.value != "doing"
        or card.owner != owner
        or card.meta.get("_claim_revision") != claim_revision
    ):
        raise plan.TestEvidenceError("profile source claim changed")
    spec = {
        "source_card": card_id,
        "source_owner": owner,
        "source_claim_revision": claim_revision,
        "source_head": _git(workspace, "rev-parse", "--verify", "HEAD"),
        "source_tree": _git(workspace, "rev-parse", "--verify", "HEAD^{tree}"),
        "source_revision": card_revision(card),
        "criteria_sha256": expected["criteria_sha256"],
    }
    plan.source_state(workspace, spec)
    source_sha256 = plan.workspace_source_fingerprint(value["repository"], workspace)
    legacy = profile.legacy_full_qualification_required(value, expected, policy)
    if any(value.get(key) != expected.get(key) for key in expected) or (
        not legacy
        and not profile.fingerprint_only_stale(
            value, expected, policy, source_sha256=source_sha256
        )
    ):
        return "ineligible"
    binding = spec
    job = {
        "schema": "skfleet.profile-requalification/v1",
        "card": card_id,
        "owner": owner,
        "claim_revision": claim_revision,
        "workspace": str(workspace.resolve()),
        "binding": binding,
        "profile_sha256": predecessor,
        "source_sha256": source_sha256,
    }
    root = _private_root(home)
    job_id = plan.sha(json.dumps(job, sort_keys=True, separators=(",", ":")).encode())
    path = root / (job_id + ".job.json")
    plan.write_once(path, job)
    return _advance(home, policy, skc, actor, path, job)


def _git(workspace: Path, *args: str) -> str:
    import subprocess

    result = subprocess.run(
        ["git", "-C", str(workspace), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.stdout.strip()


def _advance(home, policy, skc, actor, path, job):
    if not skc or not actor:
        return "pending"
    card = CardStore(home).fold(job["card"])
    if (
        card is None
        or card.status.value != "doing"
        or card.owner != job["owner"]
        or card.meta.get("_claim_revision") != job["claim_revision"]
    ):
        raise plan.TestEvidenceError("profile source claim changed")
    workspace = Path(job["workspace"])
    plan.source_state(workspace, job["binding"])
    try:
        current_plan, _, _ = plan.load_plan(home, job["binding"], allow_completed=True)
        if (
            current_plan.get("profile_requalification") is not True
            or current_plan.get("profile_predecessor_sha256") != job["profile_sha256"]
        ):
            raise plan.TestEvidenceError("another test plan owns this source revision")
    except FileNotFoundError:
        value, predecessor = profile.read_profile(home, job["card"])
        if predecessor != job["profile_sha256"]:
            raise plan.TestEvidenceError("profile predecessor changed")
        plan.seal_plan(
            home,
            job["binding"],
            workspace,
            policy,
            value["qualified_by"],
            value["qualification_sha256"],
            profile=value,
            requalification=True,
            profile_predecessor_sha256=predecessor,
        )
    receipt = tests.run_or_read_tests(home, job["binding"], workspace, policy)
    if receipt is None:
        return "pending"
    value, predecessor = profile.read_profile(home, job["card"])
    if predecessor == job["profile_sha256"]:
        profile.supersede_profile(
            home,
            card.model_dump(mode="json"),
            policy,
            value["recipe"],
            value["qualified_by"],
            receipt["receipt_sha256"],
            predecessor_sha256=predecessor,
            runtime_sha256=plan.runtime_fingerprint(),
            source_sha256=job["source_sha256"],
            source_claim={"owner": job["owner"], "claim_revision": job["claim_revision"]},
        )
    elif value.get("qualification_sha256") != receipt["receipt_sha256"]:
        raise plan.TestEvidenceError("profile predecessor changed")
    import subprocess

    latest = CardStore(home).fold(job["card"])
    if latest is None:
        raise plan.TestEvidenceError("profile qualified but source card disappeared")
    if (
        latest.owner == job["owner"]
        and latest.meta.get("_claim_revision") == job["claim_revision"]
    ):
        released = subprocess.run(
            [
                skc,
                "coord",
                "release-claim",
                job["card"],
                "--owner",
                job["owner"],
                "--expected-claim-revision",
                job["claim_revision"],
                "--agent",
                actor,
                "--abandon-reason",
                "not-abandoned",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if released.returncode:
            raise plan.TestEvidenceError("profile qualified but exact claim release was refused")
        latest = CardStore(home).fold(job["card"])
    if latest.owner is not None:
        raise plan.TestEvidenceError("profile qualified but source claim changed")
    if latest.status.value != "ready":
        moved = subprocess.run(
            [skc, "coord", "move", job["card"], "ready", "--agent", actor],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if moved.returncode:
            raise plan.TestEvidenceError("profile qualified but card did not return to ready")
    latest = CardStore(home).fold(job["card"])
    if latest is None or latest.status.value != "ready" or latest.owner is not None:
        raise plan.TestEvidenceError("profile qualified but ready-state readback failed")
    plan.write_once(
        path.with_name(path.stem + ".done.json"),
        {
            "schema": "skfleet.profile-requalification-receipt/v1",
            "card": job["card"],
            "profile_sha256": profile.read_profile(home, job["card"])[1],
            "test_receipt_sha256": receipt["receipt_sha256"],
        },
    )
    return "qualified"
