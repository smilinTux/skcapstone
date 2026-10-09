"""Native, receipt-backed refresh for execution-only stale test profiles."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import subprocess
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


@contextmanager
def _job_lock(root: Path, job_id: str):
    """Serialize one remote qualification without blocking other hosts."""
    fd = os.open(root / ("." + job_id + ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def _execution_host(home: Path, policy: dict, card_id: str) -> str:
    """Choose the least-loaded ready remote test host, with authority fallback."""
    from . import builder_dispatch, production_builder
    from .paths import paths_for_home

    paths = paths_for_home(home)
    ready = builder_dispatch._ready_builders(paths)
    candidates = production_builder.ready_nodes(paths, ready, policy, card_id)
    authority = policy["authority_host"]
    candidates = [
        view
        for view in candidates
        if production_builder.node_binding(paths, view.name, policy)["host"] != authority
    ]
    if not candidates:
        return authority
    pending = {}
    for _, job in _records(home / "fleet/profile-requalifications"):
        target = job.get("execution_host")
        if isinstance(target, str):
            pending[target] = pending.get(target, 0) + 1
    selected = min(
        candidates,
        key=lambda view: (
            pending.get(production_builder.node_binding(paths, view.name, policy)["host"], 0),
            builder_dispatch.production_load_key(paths, view, policy),
        ),
    )
    return production_builder.node_binding(paths, selected.name, policy)["host"]


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


def _claim_is_current(home: Path, job: dict) -> bool:
    card = CardStore(home).fold(job.get("card"))
    return bool(
        card is not None
        and card.status.value == "doing"
        and card.owner == job.get("owner")
        and card.meta.get("_claim_revision") == job.get("claim_revision")
    )


def harvest_completed(
    home: Path, policy: dict, card_id: str | None = None, *, limit: int = 4
) -> str:
    """Publish a bounded batch of validated completed refreshes."""
    home = Path(home)
    if socket.gethostname().split(".")[0].lower() != policy.get("authority_host"):
        return "not-authority"
    root = home / "fleet/profile-requalifications"
    try:
        plan.private_dir(root)
    except FileNotFoundError:
        return "idle"
    held_state = None
    qualified = []
    limit = 1 if card_id is not None else max(1, limit)
    with _lock(root) as acquired:
        if not acquired:
            return "busy"
        for path in sorted(root.glob("*.job.json")):
            try:
                job = json.loads(plan.read_private(path))
                if job.get("schema") != "skfleet.profile-requalification/v1" or (
                    card_id is not None and job.get("card") != card_id
                ):
                    continue
                binding = job["binding"]
                workspace = Path(job["workspace"])
                sealed, plan_path, plan_sha = plan.load_plan(
                    home, binding, allow_completed=True, allow_remote_host=True
                )
                directory = plan.run_directory(home, plan_sha)
                if not (directory / "receipt.json").is_file():
                    continue
                card = CardStore(home).fold(job["card"])
                if card is None or card.status.value not in {"backlog", "ready"} or card.owner:
                    if card_id is not None:
                        return "held:card-owned-or-not-ready"
                    continue
                current, predecessor = profile.read_profile(home, job["card"])
                if predecessor != job["profile_sha256"]:
                    continue
                if (
                    sealed.get("profile_requalification") is not True
                    or sealed.get("authority_host", policy["authority_host"])
                    != policy["authority_host"]
                    or sealed.get("profile_predecessor_sha256") != predecessor
                    or sealed.get("runtime_sha256") != plan.runtime_fingerprint()
                    or sealed.get("policy_sha256")
                    != plan.execution_policy_fingerprint(
                        policy, sealed.get("host", policy["authority_host"])
                    )
                ):
                    continue
                plan.source_state(workspace, binding)
                receipt = tests.validate_test_receipt(home, binding, workspace)
                if not profile.fingerprint_only_stale(
                    current,
                    {
                        "card": job["card"],
                        "criteria_sha256": binding["criteria_sha256"],
                        "repository": current["repository"],
                    },
                    policy,
                    source_sha256=job.get("source_sha256"),
                ):
                    continue
                profile.supersede_profile(
                    home,
                    card.model_dump(mode="json"),
                    policy,
                    current["recipe"],
                    current["qualified_by"],
                    receipt["receipt_sha256"],
                    predecessor_sha256=predecessor,
                    runtime_sha256=plan.runtime_fingerprint(),
                    source_sha256=job.get("source_sha256"),
                    unclaimed=True,
                )
                refreshed_sha = profile.read_profile(home, job["card"])[1]
                plan.write_once(
                    path.with_name(path.stem + ".done.json"),
                    {
                        "schema": "skfleet.profile-requalification-receipt/v1",
                        "card": job["card"],
                        "profile_sha256": refreshed_sha,
                        "test_receipt_sha256": receipt["receipt_sha256"],
                        "harvested": True,
                    },
                )
                qualified.append(job["card"])
                if len(qualified) >= limit:
                    return "qualified:" + ",".join(qualified)
                continue
            except (OSError, ValueError, KeyError, TypeError) as exc:
                if card_id is not None:
                    return "blocked:" + str(exc)[:160]
                continue
        # A prior cycle may have completed and sealed the native test plan,
        # then lost the in-memory/job handoff before harvesting its receipt.
        # Discover those plans directly so selection (and a new claim) is not
        # required to finish publication.
        plans = home / "fleet/test-plans"
        if plans.is_dir():
            for plan_path in sorted(plans.glob("*.json")):
                try:
                    sealed_candidate = json.loads(plan.read_private(plan_path))
                    binding = sealed_candidate.get("binding")
                    candidate_id = (
                        binding.get("source_card") if isinstance(binding, dict) else None
                    )
                    if (
                        sealed_candidate.get("profile_requalification") is not True
                        or not isinstance(candidate_id, str)
                        or (card_id is not None and candidate_id != card_id)
                    ):
                        continue
                    sealed, _, plan_sha = plan.load_plan(
                        home, binding, allow_completed=True, allow_remote_host=True
                    )
                    done_path = root / ("harvested-" + plan_sha + ".json")
                    if done_path.exists():
                        continue
                    workspace = home / "fleet/workspaces" / str(binding.get("source_owner") or "")
                    directory = plan.run_directory(home, plan_sha)
                    if not (directory / "receipt.json").is_file():
                        continue
                    card = CardStore(home).fold(candidate_id)
                    if card is None or card.status.value not in {"backlog", "ready"} or card.owner:
                        if card_id is not None:
                            return "held:card-owned-or-not-ready"
                        held_state = held_state or (
                            "held:" + candidate_id + ":card-owned-or-not-ready"
                        )
                        continue
                    current, predecessor = profile.read_profile(home, candidate_id)
                    profile_predecessor = sealed.get("profile_predecessor_sha256")
                    if predecessor != profile_predecessor:
                        continue
                    if (
                        sealed.get("runtime_sha256") != plan.runtime_fingerprint()
                        or sealed.get("authority_host", policy["authority_host"])
                        != policy["authority_host"]
                        or sealed.get("policy_sha256")
                        != plan.execution_policy_fingerprint(
                            policy, sealed.get("host", policy["authority_host"])
                        )
                    ):
                        continue
                    plan.source_state(workspace, binding)
                    receipt = tests.validate_test_receipt(home, binding, workspace)
                    source_sha = plan.workspace_source_fingerprint(
                        current["repository"], workspace
                    )
                    if not profile.fingerprint_only_stale(
                        current,
                        {
                            "card": candidate_id,
                            "criteria_sha256": binding["criteria_sha256"],
                            "repository": current["repository"],
                        },
                        policy,
                        source_sha256=source_sha,
                    ):
                        continue
                    profile.supersede_profile(
                        home,
                        card.model_dump(mode="json"),
                        policy,
                        current["recipe"],
                        current["qualified_by"],
                        receipt["receipt_sha256"],
                        predecessor_sha256=predecessor,
                        runtime_sha256=plan.runtime_fingerprint(),
                        source_sha256=source_sha,
                        unclaimed=True,
                    )
                    refreshed_sha = profile.read_profile(home, candidate_id)[1]
                    plan.write_once(
                        done_path,
                        {
                            "schema": "skfleet.profile-requalification-receipt/v1",
                            "card": candidate_id,
                            "profile_sha256": refreshed_sha,
                            "test_receipt_sha256": receipt["receipt_sha256"],
                            "plan_sha256": plan_sha,
                            "harvested": True,
                        },
                    )
                    qualified.append(candidate_id)
                    if len(qualified) >= limit:
                        return "qualified:" + ",".join(qualified)
                    continue
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    if card_id is not None:
                        return "blocked:" + str(exc)[:160]
    if qualified:
        return "qualified:" + ",".join(qualified)
    return held_state or "idle"


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
        pending = [(path, job) for path, job in _records(root) if _claim_is_current(home, job)]
        pending_job = next(
            (
                (path, job)
                for path, job in pending
                if card_id in (None, job.get("card"))
                and (owner is None or job.get("owner") == owner)
                and (claim_revision is None or job.get("claim_revision") == claim_revision)
            ),
            None,
        )
        if pending_job is not None:
            targets = [pending_job] if card_id is not None else pending[:4]
            states = []
            for path, job in targets:
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
                    state = "failed:" + str(exc)[:160]
                states.append(str(job.get("card")) + "=" + state)
            if card_id is not None:
                return states[0].split("=", 1)[1]
            return "batch:" + ",".join(states)
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
                    return "failed:" + str(exc)[:160]
            return "blocked:" + str(exc)[:180]


def offer_stale_candidate(
    home: Path,
    policy: dict,
    skc: str,
    actor: str,
    core: dict,
    labels: list[str] | tuple[str, ...],
    prepare_workspace,
) -> str:
    """Claim and queue one stale profile for remote native qualification."""
    home = Path(home)
    if socket.gethostname().split(".")[0].lower() != policy.get("authority_host"):
        return "not-authority"
    from . import production_test_profile as profile

    card_id = str(core.get("id") or "").lower()
    try:
        profile.preflight(home, dict(core, id=card_id), labels, policy)
    except profile.ProfileRequalificationRequired:
        pass
    except (OSError, ValueError) as exc:
        return "ineligible:" + str(exc)[:120]
    else:
        return "current"

    root = home / "fleet/profile-requalifications"
    card = CardStore(home).fold(card_id)
    if card is None or card.status.value != "ready" or card.owner:
        return "deferred:card-not-ready-or-owned"
    try:
        current, _predecessor = profile.read_profile(home, card_id)
        repository = current["repository"]
        workspace = Path(prepare_workspace(core, labels))
        source_sha = plan.workspace_source_fingerprint(repository, workspace)
        expected = profile.contract(dict(core, id=card_id))
        if not (
            profile.fingerprint_only_stale(current, expected, policy, source_sha256=source_sha)
            or profile.legacy_full_qualification_required(current, expected, policy)
        ):
            return "ineligible:source-or-contract-changed"
        if _execution_host(home, policy, card_id) == policy["authority_host"]:
            return "deferred:no-ready-remote-host"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return "ineligible:" + str(exc)[:120]

    import subprocess

    claim = subprocess.run(
        [skc, "coord", "claim", card_id, "--agent", actor],
        capture_output=True,
        text=True,
        timeout=15,
    )
    card = CardStore(home).fold(card_id)
    claim_revision = (card.meta or {}).get("_claim_revision") if card else None
    if claim.returncode or card is None or card.owner != actor or not claim_revision:
        return "deferred:claim-refused"
    state = requalify_or_advance(
        home,
        policy,
        skc,
        actor,
        card_id=card_id,
        core=dict(core, id=card_id),
        workspace=str(workspace),
        owner=actor,
        claim_revision=claim_revision,
    )
    if state == "pending":
        target = next(
            (
                job.get("execution_host")
                for _, job in _records(root)
                if job.get("card") == card_id
                and job.get("owner") == actor
                and job.get("claim_revision") == claim_revision
            ),
            "unknown-host",
        )
        return "pending:" + str(target)
    if state not in {"pending", "qualified"}:
        latest = CardStore(home).fold(card_id)
        if (
            latest is not None
            and latest.owner == actor
            and latest.meta.get("_claim_revision") == claim_revision
        ):
            released = subprocess.run(
                [
                    skc,
                    "coord",
                    "release-claim",
                    card_id,
                    "--owner",
                    actor,
                    "--expected-claim-revision",
                    claim_revision,
                    "--agent",
                    actor,
                    "--abandon-reason",
                    "not-abandoned",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            after = CardStore(home).fold(card_id)
            if (
                released.returncode
                or after is None
                or after.owner == actor
                and after.meta.get("_claim_revision") == claim_revision
            ):
                return "blocked:qualification-failed-claim-release-refused"
    return state


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
        "execution_host": _execution_host(home, policy, card_id),
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
        current_plan, _, _ = plan.load_plan(
            home, job["binding"], allow_completed=True, allow_remote_host=True
        )
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
            execution_host=job.get("execution_host"),
        )
    except plan.TestEvidenceError as exc:
        if str(exc) != "operator test plan is invalid or stale":
            raise
        current_plan, _, plan_predecessor = plan.load_plan(
            home, job["binding"], require_current=False, allow_remote_host=True
        )
        if (
            current_plan.get("profile_requalification") is not True
            or current_plan.get("profile_predecessor_sha256") != job["profile_sha256"]
        ):
            raise plan.TestEvidenceError("another test plan owns this source revision") from exc
        value, profile_predecessor = profile.read_profile(home, job["card"])
        if profile_predecessor != job["profile_sha256"]:
            raise plan.TestEvidenceError("profile predecessor changed") from exc
        plan.seal_plan(
            home,
            job["binding"],
            workspace,
            policy,
            value["qualified_by"],
            value["qualification_sha256"],
            profile=value,
            predecessor_sha256=plan_predecessor,
            requalification=True,
            profile_predecessor_sha256=profile_predecessor,
            execution_host=job.get("execution_host"),
        )
    # The authority process also reads the remote worker's sealed receipt.
    # run_or_read_tests returns pending until that host writes terminal evidence.
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


def consume_remote(home: Path, policy: dict, host: str) -> list[str]:
    """Run exact authority-sealed qualification jobs assigned to this host."""
    if host != socket.gethostname().split(".")[0].lower():
        return ["refused:host-mismatch"]
    if host == policy.get("authority_host"):
        return []
    root = Path(home) / "fleet/profile-requalifications"
    try:
        plan.private_dir(root)
    except FileNotFoundError:
        return []
    from . import store
    from .builder_dispatch import materialize_source
    from .paths import paths_for_home

    if not store.actuation_allowed(paths_for_home(home)):
        return ["frozen"]

    states = []
    for path, job in _records(root):
        if job.get("execution_host") != host:
            continue
        job_id = path.name.removesuffix(".job.json")
        with _job_lock(root, job_id) as acquired:
            if not acquired:
                continue
            try:
                card = CardStore(home).fold(job["card"])
                if (
                    card is None
                    or card.archived
                    or card.status.value != "doing"
                    or card.owner != job["owner"]
                    or card.meta.get("_claim_revision") != job["claim_revision"]
                ):
                    raise plan.TestEvidenceError("remote qualification claim changed")
                sealed, _, _ = plan.load_plan(home, job["binding"])
                if (
                    sealed.get("remote_requalification") is not True
                    or sealed.get("authority_host") != policy.get("authority_host")
                    or sealed.get("host") != host
                ):
                    raise plan.TestEvidenceError("remote qualification plan binding changed")
                value, predecessor = profile.read_profile(home, job["card"])
                if predecessor != job["profile_sha256"]:
                    raise plan.TestEvidenceError("remote qualification profile changed")
                workspace = Path(job["workspace"])
                request = {
                    "repository": value["repository"],
                    "base_ref": "main",
                    "base_revision": job["binding"]["source_head"],
                }
                materialize_source(request, workspace)
                plan.source_state(workspace, job["binding"])
                receipt = tests.run_or_read_tests(home, job["binding"], workspace, policy)
                states.append(
                    "running:" + job["card"] if receipt is None else "receipt:" + job["card"]
                )
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
                plan.write_once(
                    path.with_name(path.stem + ".failed.json"),
                    {
                        "schema": "skfleet.profile-requalification-failure/v1",
                        "card": job.get("card"),
                        "host": host,
                        "reason": str(exc)[:200],
                    },
                )
                states.append("failed:" + str(job.get("card")) + ":" + str(exc)[:100])
    return states
