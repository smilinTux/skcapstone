"""Receipt-backed remote initial qualification and refresh of test profiles."""

from __future__ import annotations

import fcntl
import json
import os
import re
import socket
import subprocess
import threading
import time
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from pathlib import Path

from skcoord.card_store import CardStore

from ..seraph_review_cardstore import card_revision
from . import production_test_node as node
from . import production_test_plan as plan
from . import production_test_profile as profile
from . import production_tests as tests

# Window in which a missing synced file is treated as Syncthing lag, not a failure.
SYNC_GRACE_SECONDS = 1800

_REMOTE_PREDECESSOR = ContextVar("remote_profile_predecessor", default=None)
_PLAN_LOAD_PATCH_LOCK = threading.RLock()
_REMOTE_INITIAL_WORKER = ContextVar("remote_initial_worker", default=None)
_WORKER_ARGV_PATCH_LOCK = threading.RLock()


@contextmanager
def _remote_initial_worker(home: Path, binding: dict):
    """Use the scoped worker shim when this plan descends from a remote initial plan."""
    expected_home = Path(home).resolve()
    expected_binding = json.dumps(binding, sort_keys=True, separators=(",", ":"))
    try:
        base = plan._initial_plan_base(expected_home, binding)
        initial = json.loads(plan.read_private(base))
    except (FileNotFoundError, OSError, ValueError, KeyError, TypeError):
        yield
        return
    if initial.get("remote_initial_profile_qualification") is not True:
        yield
        return
    with _WORKER_ARGV_PATCH_LOCK:
        original = tests.service_argv

        def service_argv(request, plan_path, directory, workspace):
            argv = original(request, plan_path, directory, workspace)
            active = _REMOTE_INITIAL_WORKER.get()
            if (
                active != (expected_home, expected_binding)
                or Path(plan_path).parent.parent.parent.resolve() != expected_home
                or json.dumps(request.get("binding"), sort_keys=True, separators=(",", ":"))
                != expected_binding
            ):
                return argv
            position = argv.index("--") + 1
            command = argv[position:]
            expected = [
                str(tests.PREFIX / "bin/python"),
                "-I",
                "-m",
                "skcapstone.fleet.production_test_worker",
                str(plan_path),
                str(directory),
            ]
            if command != expected:
                raise plan.TestEvidenceError("remote initial worker command changed")
            argv[position:] = [
                str(tests.PREFIX / "bin/python"),
                "-I",
                "-m",
                "skcapstone.fleet.remote_initial_test_worker",
                str(plan_path),
                str(directory),
            ]
            return argv

        token = _REMOTE_INITIAL_WORKER.set((expected_home, expected_binding))
        tests.service_argv = service_argv
        try:
            yield
        finally:
            tests.service_argv = original
            _REMOTE_INITIAL_WORKER.reset(token)


def _remote_initial_preflight_failure(home: Path, binding: dict, plan_sha: str) -> bool:
    """Prove a launched plan failed before tests only because its chain has a remote parent."""
    try:
        current, path, current_sha = plan.load_plan(
            home, binding, require_current=False, allow_remote_host=True
        )
        initial = json.loads(plan.read_private(plan._initial_plan_base(home, binding)))
        if (
            current_sha != plan_sha
            or current.get("initial_profile_qualification") is not True
            or initial.get("remote_initial_profile_qualification") is not True
            or initial.get("authority_host") != socket.gethostname().split(".")[0].lower()
            or initial.get("host") == current.get("host")
            or current.get("host") != socket.gethostname().split(".")[0].lower()
        ):
            return False
        try:
            plan.load_plan(home, binding, require_current=False)
        except plan.TestEvidenceError as exc:
            if str(exc) != "operator test plan is invalid or stale":
                return False
        else:
            return False
        directory = plan.run_directory(home, plan_sha)
        launch = tests.read_json(directory / "launch.json")
        terminal = tests.read_json(directory / "terminal.json")
        workspace = Path(launch["workspace"]).resolve(strict=True)
        plan.source_state(workspace, binding)
        if (
            (directory / "receipt.json").exists()
            or launch.get("binding") != binding
            or launch.get("request_id") != plan_sha
            or terminal.get("unit") != launch.get("unit")
            or terminal.get("ActiveState") != "failed"
            or terminal.get("SubState") != "failed"
            or terminal.get("ExecMainStatus") != "1"
            or not re.fullmatch(r"[0-9a-f]{32}", str(terminal.get("InvocationID", "")))
        ):
            return False
        expected = tests.service_argv(launch, path, directory, Path(launch["workspace"]))
        if launch.get("service_argv") != expected:
            return False
        return not any(
            child.is_file() and child.name not in {"launch.json", "terminal.json", ".run.lock"}
            for child in directory.rglob("*")
        )
    except (OSError, ValueError, KeyError, TypeError, plan.TestEvidenceError):
        return False


@contextmanager
def _permit_remote_initial_retry(home: Path, binding: dict, failed_sha: str):
    """Temporarily recognize only the proven preflight failure for successor sealing."""
    expected_home = Path(home).resolve()
    with _PLAN_LOAD_PATCH_LOCK:
        original = plan._completed_test_attempt

        def completed(request_home, request_sha):
            if (
                Path(request_home).resolve() == expected_home
                and request_sha == failed_sha
                and _remote_initial_preflight_failure(expected_home, binding, failed_sha)
            ):
                return True
            return original(request_home, request_sha)

        plan._completed_test_attempt = completed
        try:
            yield
        finally:
            plan._completed_test_attempt = original


@contextmanager
def _remote_predecessor_for_seal(home: Path, binding: dict):
    """Permit the sealed-plan writer to re-read one validated remote predecessor."""
    expected_home = Path(home).resolve()
    expected_binding = json.dumps(binding, sort_keys=True, separators=(",", ":"))
    with _PLAN_LOAD_PATCH_LOCK:
        original = plan.load_plan

        def load_plan(request_home, request_binding, *args, **kwargs):
            active = _REMOTE_PREDECESSOR.get()
            if (
                active == (expected_home, expected_binding)
                and Path(request_home).resolve() == expected_home
                and json.dumps(request_binding, sort_keys=True, separators=(",", ":"))
                == expected_binding
                and kwargs.get("require_current", True) is False
            ):
                kwargs["allow_remote_host"] = True
            return original(request_home, request_binding, *args, **kwargs)

        token = _REMOTE_PREDECESSOR.set((expected_home, expected_binding))
        plan.load_plan = load_plan
        try:
            yield
        finally:
            plan.load_plan = original
            _REMOTE_PREDECESSOR.reset(token)


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


def _write_failure(path: Path, card_id: str, reason: str) -> str:
    """Write one immutable failure, or report the prior failure on replay."""
    failure_path = path.with_name(path.stem + ".failed.json")
    failure = {
        "schema": "skfleet.profile-requalification-failure/v1",
        "card": card_id,
        "reason": str(reason)[:240],
    }
    try:
        plan.write_once(failure_path, failure)
    except FileExistsError:
        existing = json.loads(plan.read_private(failure_path))
        if existing.get("card") != card_id:
            raise plan.TestEvidenceError("profile qualification failure record conflicts")
        return str(existing.get("reason", "prior qualification failure"))[:160]
    return failure["reason"][:160]


def _all_vitest_targets(recipe: dict, workspace: Path) -> dict:
    """Match the fixed full-suite Vitest command while preserving requested floors."""
    targets = recipe.get("vitest") if isinstance(recipe, dict) else None
    if not isinstance(targets, dict):
        return recipe
    web = workspace / "apps/web"
    files = sorted(
        path.relative_to(web).as_posix()
        for path in web.rglob("*")
        if path.is_file()
        and (".test." in path.name or ".spec." in path.name)
        and path.suffix in {".ts", ".tsx", ".js", ".jsx"}
    )
    if (
        not files
        or len(files) > 256
        or any(
            not path.startswith("src/")
            or ".test.ts" not in path
            or (web / path).is_symlink()
            or not (web / path).resolve().is_relative_to(web.resolve())
            for path in files
        )
    ):
        raise plan.TestEvidenceError("Node source has no bounded Vitest suite")
    expanded = {path: 1 for path in files}
    for path, minimum in targets.items():
        if path not in expanded:
            raise plan.TestEvidenceError("requested Vitest target is outside the full suite")
        expanded[path] = minimum
    return {**recipe, "vitest": expanded}


def _compatible_initial_profile(previous: dict, desired: dict) -> bool:
    """Allow only an unchanged profile or a monotonic Vitest suite expansion."""
    if not isinstance(previous, dict) or not isinstance(desired, dict):
        return False
    old = dict(previous)
    new = dict(desired)
    old.pop("qualified_by", None)
    new.pop("qualified_by", None)
    old_recipe_sha = old.pop("qualification_sha256", None)
    new_recipe_sha = new.pop("qualification_sha256", None)
    old_recipe = old.pop("recipe", None)
    new_recipe = new.pop("recipe", None)
    if old != new or not isinstance(old_recipe, dict) or not isinstance(new_recipe, dict):
        return False
    for recipe, digest in ((old_recipe, old_recipe_sha), (new_recipe, new_recipe_sha)):
        expected = plan.sha(json.dumps(recipe, sort_keys=True, separators=(",", ":")).encode())
        if digest != expected:
            return False
    if old_recipe == new_recipe:
        return True
    if set(old_recipe) != set(new_recipe) or "vitest" not in old_recipe:
        return False
    for key in old_recipe:
        old_targets, new_targets = old_recipe[key], new_recipe[key]
        if key == "vitest":
            if (
                not isinstance(old_targets, dict)
                or not isinstance(new_targets, dict)
                or any(new_targets.get(path, 0) < minimum for path, minimum in old_targets.items())
            ):
                return False
        elif old_targets != new_targets:
            return False
    return True


def _qualification_implementation_sha256() -> str:
    """Bind retries to the exact code that prepares and validates qualification plans."""
    modules = (Path(__file__), Path(plan.__file__), Path(profile.__file__), Path(node.__file__))
    manifest = [(path.name, plan.sha(path.read_bytes())) for path in modules]
    return plan.sha(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode())


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


def _fleet_paths(home: Path):
    """The fleet tree under the SOVEREIGN home every caller of this module passes.

    skfleet-rotate and sknoded both hand in the sovereign home, and the job
    records here live under it, so the user-home resolver would double the
    sovereign directory and find no nodes (see paths_for_sovereign_home).
    """
    from .paths import paths_for_sovereign_home

    return paths_for_sovereign_home(home)


def _remote_source_reconstruction_failed(
    home: Path, authority_host: str, card_id: str, source_sha256: str
) -> bool:
    root = home / "fleet/profile-requalifications"
    for failed in root.glob("*.job.failed.json"):
        try:
            job_path = failed.with_name(failed.name.replace(".failed", ""))
            job = json.loads(plan.read_private(job_path))
            reason = json.loads(plan.read_private(failed)).get("reason")
        except (OSError, ValueError):
            continue
        if (
            job.get("card") == card_id
            and job.get("source_sha256") == source_sha256
            and job.get("execution_host") != authority_host
            and reason == "exact source reconstruction failed"
        ):
            return True
    return False


def _execution_host(
    home: Path, policy: dict, card_id: str, source_sha256: str | None = None
) -> str:
    """Choose the least-loaded ready remote test host, with authority fallback."""
    if source_sha256 and _remote_source_reconstruction_failed(
        home, policy["authority_host"], card_id, source_sha256
    ):
        return policy["authority_host"]
    from . import builder_dispatch, production_builder

    paths = _fleet_paths(home)
    ready = builder_dispatch._ready_builders(paths)
    candidates = production_builder.ready_nodes(paths, ready, policy, card_id)
    authority = policy["authority_host"]
    candidates = [
        view
        for view in candidates
        if production_builder.node_binding(paths, view.name, policy)["host"]
        in {"chiap01", "chiap02", "chiap03", "chiap04"}
        and production_builder.node_binding(paths, view.name, policy)["host"] != authority
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
    home: Path,
    policy: dict,
    card_id: str | None = None,
    *,
    limit: int = 4,
    deadline=None,
    phase: str = "profile_harvest",
) -> str:
    """Publish a bounded batch of validated completed refreshes.

    ``deadline`` (a CycleDeadline) bounds the sealed-plan scan: it stops at the
    ``phase`` share of the cycle and the next cycle resumes at the first plan not
    reached, so a long plan directory cannot starve lane selection and launch. With
    time to spare the whole directory is scanned and nothing changes.
    """
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
    # One CardStore serves this harvest's reads until it publishes. A fresh
    # store replays the whole shared legacy overlay (~0.5s on chiap08) on its
    # first fold, and a publication may write the board, so each publication
    # drops the store and the next read starts fresh.
    store = None
    job_bindings = set()
    with _lock(root) as acquired, node.artifact_digest_memo():
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
                # The orphan pass must not revisit a managed generation, including
                # failed/done jobs. Its owner-derived workspace is only a fallback
                # for plans whose job handoff was genuinely lost.
                job_bindings.add(json.dumps(binding, sort_keys=True))
                if any(
                    path.with_name(path.stem + suffix).exists()
                    for suffix in (".done.json", ".failed.json")
                ):
                    continue
                workspace = Path(job["workspace"])
                sealed, plan_path, plan_sha = plan.load_plan(
                    home, binding, allow_completed=True, allow_remote_host=True
                )
                directory = plan.run_directory(home, plan_sha)
                if not (directory / "receipt.json").is_file():
                    continue
                store = CardStore(home) if store is None else store
                card = store.fold(job["card"])
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
                    home=home,
                ):
                    continue
                store = None
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
                    node_environment=_successor_environment(current),
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
            # A job whose workspace is gone makes git raise CalledProcessError. Not
            # catching it aborted the whole harvest, and rotate's shared try then skipped
            # requalify_or_advance too: one deleted workspace (8d4e7c5f) left every
            # authority-side requalification unfinalized for hours (2026-10-11).
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
                if card_id is not None:
                    return "blocked:" + str(exc)[:160]
                continue
        # A prior cycle may have completed and sealed the native test plan,
        # then lost the in-memory/job handoff before harvesting its receipt.
        # Discover those plans directly so selection (and a new claim) is not
        # required to finish publication.
        plans = home / "fleet/test-plans"
        stopped_at = None
        if plans.is_dir():
            plan_paths = sorted(plans.glob("*.json"))
            if deadline is not None and card_id is None:
                plan_paths = deadline.rotate_from(
                    plan_paths, deadline.read_cursor(phase), key=lambda path: path.name
                )
            for scanned, plan_path in enumerate(plan_paths):
                if (
                    deadline is not None
                    and card_id is None
                    and deadline.stop(phase, scanned, len(plan_paths) - scanned, min_done=1)
                ):
                    stopped_at = plan_path.name
                    break
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
                    if json.dumps(binding, sort_keys=True) in job_bindings:
                        continue
                    # Historical plans are discovery hints, not a veto on fresh
                    # qualification. Skip only provably obsolete generations;
                    # current plans still pass every native plan/receipt check.
                    old_runtime = sealed_candidate.get("runtime_sha256")
                    old_profile = sealed_candidate.get("profile_predecessor_sha256")
                    if old_runtime is not None and old_runtime != plan.runtime_fingerprint():
                        continue
                    if old_profile is not None:
                        _, current_profile = profile.read_profile(home, candidate_id)
                        if old_profile != current_profile:
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
                    store = CardStore(home) if store is None else store
                    card = store.fold(candidate_id)
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
                        home=home,
                    ):
                        continue
                    store = None
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
                        node_environment=_successor_environment(current),
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
                except (
                    OSError,
                    ValueError,
                    KeyError,
                    TypeError,
                    subprocess.SubprocessError,
                ) as exc:
                    if card_id is not None:
                        return "blocked:" + str(exc)[:160]
        if plans.is_dir() and deadline is not None and card_id is None:
            deadline.write_cursor(phase, stopped_at)
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
        released = _release_failed_claims(home, skc, actor, root) if card_id is None else []
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
                except (OSError, ValueError, subprocess.SubprocessError) as exc:
                    reason = _write_failure(path, str(job.get("card")), str(exc))
                    state = "failed:" + reason
                states.append(str(job.get("card")) + "=" + state)
            if card_id is not None:
                return states[0].split("=", 1)[1]
            return "batch:" + ",".join(states + released)
        if released:
            return "batch:" + ",".join(released)
        if card_id is None or not all((core, workspace, owner, claim_revision)):
            return "idle"
        try:
            return _begin(
                home, policy, skc, actor, card_id, core, Path(workspace), owner, claim_revision
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            for path, job in _records(root):
                if job.get("card") == card_id and job.get("claim_revision") == claim_revision:
                    return "failed:" + _write_failure(path, card_id, str(exc))
            return "blocked:" + str(exc)[:180]


def _successor_environment(value: dict) -> dict | None:
    """Node environment a successor profile must carry: current, or runtime-refreshed.

    Every supersede here omitted it, so a Node or composite successor failed
    validation ("test recipe and profile variant disagree") and no Node profile
    could ever requalify.
    """
    if "node_environment" not in value:
        return None
    from . import production_test_node as node

    return node.current_environment(value["node_environment"])


def _finished_claim_verdict(home: Path, job: dict) -> str | None:
    """Return a PASS outcome held by this exact producer claim, if present."""
    from ..release_guard import finished_work_verdict

    return finished_work_verdict(
        home,
        job["card"],
        job["owner"],
        job["claim_revision"],
    )


def _retained_finished_claim(home: Path, job: dict, latest) -> str | None:
    """Keep a successful source claim held for its independent review and acceptance."""
    verdict = _finished_claim_verdict(home, job)
    if verdict is None:
        return None
    if (
        latest is None
        or latest.owner != job["owner"]
        or latest.meta.get("_claim_revision") != job["claim_revision"]
    ):
        raise plan.TestEvidenceError("finished source claim changed after qualification")
    return verdict


def _qualify_profile_or_replay(home, core, policy, recipe, qualified_by, receipt_sha, **kwargs):
    """Publish a profile once, or accept only an identical prior publication."""
    try:
        return profile.qualify_profile(
            home, core, policy, recipe, qualified_by, receipt_sha, **kwargs
        )
    except FileExistsError:
        existing, _ = profile.read_profile(home, core["id"])
        expected = profile.profile_value(core, policy, recipe, qualified_by, receipt_sha, **kwargs)
        if existing != expected:
            raise plan.TestEvidenceError("qualified profile changed before recovery")
        return home / "fleet/test-profiles" / (core["id"] + ".json")


# A qualification that failed on this exact source will fail again: the SKLegal
# full suite needs Postgres, systemd and real credentials, so pytest_all can
# never be green in the sandbox. Re-offering it every cycle burned the four
# offer slots and claim churn (2026-10-09). Wait for the source to change, or
# for the backoff to expire, before trying the same card again.
RECENT_FAILURE_SECONDS = 6 * 3600
OFFERABLE_COLUMNS = frozenset({"ready", "backlog"})
DETERMINISTIC_FAILURES = (
    "custody mismatch",  # the sealed tests ran and failed
    "recipe is invalid",
    "test target missing",
    "no supported initial test recipe",
    "full pytest suite failed",
)


def _recent_failure(
    root: Path, card_id: str, source_sha: str, operator_recipe: str | None = None
) -> bool:
    import time

    now = time.time()
    # A failure under an older qualification environment is not a repeat: the
    # same source and recipe in a rebuilt qualify-env is a new attempt. Every
    # skcapstone card waited out the 6h backoff after the environment gained
    # the dependencies its suite needs (chi, 2026-10-11).
    from .qualified_runtime import STATE_NAME

    try:
        environment_built = (plan.PREFIX / STATE_NAME).stat().st_mtime
    except OSError:
        environment_built = 0.0
    for failed in root.glob("*.job.failed.json"):
        try:
            recorded = failed.stat().st_mtime
            if now - recorded > RECENT_FAILURE_SECONDS or recorded < environment_built:
                continue
            job = json.loads(
                plan.read_private(failed.with_name(failed.name.replace(".failed", "")))
            )
        except (OSError, ValueError):
            continue
        if job.get("card") != card_id or job.get("source_sha256") != source_sha:
            continue
        # A changed operator recipe is a new attempt, not a repeat: SKLegal
        # cards that failed on a mis-matched baseline recipe were re-linked to
        # their own revision's recipe and must not wait out the backoff.
        if operator_recipe is not None:
            try:
                current = json.loads(operator_recipe)
            except ValueError:
                current = None
            tried = (job.get("initial_profile_value") or {}).get("recipe")
            if current is not None and tried is not None and current != tried:
                continue
        try:
            reason = str(json.loads(plan.read_private(failed)).get("reason") or "")
        except (OSError, ValueError):
            continue
        # Back off only on verdicts the same source will repeat. Plan staleness
        # after a deploy, claim races and Syncthing lag clear on their own.
        if any(marker in reason for marker in DETERMINISTIC_FAILURES):
            # A fixed trusted harness is a new attempt even when qualify-env
            # itself was not rebuilt. Validate the old plan before comparing;
            # unreadable evidence retains the conservative failure backoff.
            try:
                prior, _, _ = plan.load_plan(
                    root.parent.parent,
                    job["binding"],
                    require_current=False,
                    allow_completed=True,
                    allow_remote_host=True,
                )
                if prior["runtime_sha256"] != plan.runtime_fingerprint():
                    continue
            except (OSError, ValueError, KeyError, TypeError):
                pass
            return True
    return False


def requalification_core(core: dict, card) -> dict:
    """The pool core merged with the folded card's labels and links (see below)."""
    return dict(
        core,
        labels=sorted(getattr(card, "labels", None) or []),
        links=dict(core.get("links") or {}, **(getattr(card, "links", None) or {})),
    )


def offer_stale_candidate(
    home: Path,
    policy: dict,
    skc: str,
    actor: str,
    core: dict,
    labels: list[str] | tuple[str, ...],
    prepare_workspace,
) -> str:
    """Claim and queue one missing or stale profile for remote qualification."""
    home = Path(home)
    if socket.gethostname().split(".")[0].lower() != policy.get("authority_host"):
        return "not-authority"
    from . import production_test_profile as profile

    card_id = str(core.get("id") or "").lower()
    initial = False
    try:
        profile.preflight(home, dict(core, id=card_id), labels, policy)
    except profile.ProfileRequalificationRequired:
        pass
    except (OSError, ValueError) as exc:
        if str(exc) == "required-test-profile-unqualified":
            initial = True
        else:
            return "ineligible:" + str(exc)[:120]
    else:
        return "current"

    root = home / "fleet/profile-requalifications"
    card = CardStore(home).fold(card_id)
    # POOL_V2 dispatches approved backlog cards as well as ready ones, and local
    # lanes claim from either; refusing backlog here wasted the offer on nine
    # dispatchable SKLegal cards every cycle (2026-10-09).
    if card is None or card.status.value not in OFFERABLE_COLUMNS or card.owner:
        return "deferred:card-not-ready-or-owned"
    # The executing builder recomputes the initial recipe from its folded card,
    # which carries the board labels; the pool core passed in here does not.
    # Labels are scope (frontend/backend), so the two sides chose different
    # recipes and the builder failed the job as "remote initial qualification
    # source recipe changed" (f2ea7759 on chiap03, 2026-10-09). Bind the same
    # folded labels on this side. The same holds for links: the operator
    # test_profile_recipe link lives on the folded card only, so the authority
    # sealed pytest_all while the builder chose the operator recipe and failed
    # every such job the same way (ead91896, c33a1004, 2026-10-09).
    origin_column = card.status.value
    core = requalification_core(core, card)
    try:
        workspace = Path(prepare_workspace(core, labels))
        expected = profile.contract(dict(core, id=card_id))
        repository = expected.get("repository")
        if repository is None and not initial:
            repository = profile.read_profile(home, card_id)[0]["repository"]
        if not isinstance(repository, str):
            return "ineligible:test repository binding is invalid"
        source_sha = plan.workspace_source_fingerprint(repository, workspace)
        if _recent_failure(
            root,
            card_id,
            source_sha,
            (getattr(card, "links", None) or {}).get("test_profile_recipe"),
        ):
            return "deferred:recent-qualification-failure"
        if initial:
            profile.initial_recipe(core, workspace)
        else:
            current, _predecessor = profile.read_profile(home, card_id)
            if not (
                profile.fingerprint_only_stale(
                    current, expected, policy, source_sha256=source_sha, home=home
                )
                or profile.legacy_full_qualification_required(current, expected, policy)
            ):
                return "ineligible:source-or-contract-changed"
        if _execution_host(home, policy, card_id, source_sha) == policy[
            "authority_host"
        ] and not _remote_source_reconstruction_failed(
            home, policy["authority_host"], card_id, source_sha
        ):
            return "deferred:no-ready-remote-host"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return "ineligible:" + str(exc)[:120]

    import subprocess

    # One claimant per card. skcoord keeps a single current task per agent and
    # DEMOTES the previous one to ready on every new claim, so claiming up to
    # four cards per cycle as plain "niobe" left only the last one in doing;
    # the builders then refused the rest as "remote qualification claim
    # changed" (2026-10-09: d0a4c912 demoted 1s after niobe claimed d621aeec).
    claimant = f"{actor}-requal-{card_id}"
    claim = subprocess.run(
        [skc, "coord", "claim", card_id, "--agent", claimant],
        capture_output=True,
        text=True,
        timeout=15,
    )
    card = CardStore(home).fold(card_id)
    claim_revision = (card.meta or {}).get("_claim_revision") if card else None
    if claim.returncode or card is None or card.owner != claimant or not claim_revision:
        return "deferred:claim-refused"
    state = requalify_or_advance(
        home,
        policy,
        skc,
        actor,
        card_id=card_id,
        core=dict(core, id=card_id),
        workspace=str(workspace),
        owner=claimant,
        claim_revision=claim_revision,
    )
    if state == "pending":
        target = next(
            (
                job.get("execution_host")
                for _, job in _records(root)
                if job.get("card") == card_id
                and job.get("owner") == claimant
                and job.get("claim_revision") == claim_revision
            ),
            "unknown-host",
        )
        return "pending:" + str(target)
    if state not in {"pending", "qualified"}:
        if (
            _release_exact_claim(
                home, skc, actor, card_id, claimant, claim_revision, column=origin_column
            )
            is False
        ):
            return "blocked:qualification-failed-claim-release-refused"
    return state


def _release_exact_claim(
    home, skc, actor, card_id, owner, claim_revision, column="ready", store=None
):
    """Release a qualification claim only while it is still the exact holder.

    Returns None when the claim is no longer held (nothing to do), True when it
    was released, and False when the release was refused. ``store`` serves only
    the read before any release; the read after a release is always a fresh
    CardStore, because the release itself changed the board.
    """
    latest = (store if store is not None else CardStore(home)).fold(card_id)
    if (
        latest is None
        or latest.owner != owner
        or latest.meta.get("_claim_revision") != claim_revision
    ):
        return None
    released = subprocess.run(
        [
            skc,
            "coord",
            "release-claim",
            card_id,
            "--owner",
            owner,
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
        or after.owner == owner
        and after.meta.get("_claim_revision") == claim_revision
    ):
        return False
    # release-claim lands the card in backlog, which parked READY cards for
    # good after one failed qualification (8 SKLegal cards on 2026-10-09).
    # Restore the column the card was offered from (ready unless known).
    if after.status.value != column and after.owner is None:
        subprocess.run(
            [skc, "coord", "move", card_id, column, "--agent", actor],
            capture_output=True,
            text=True,
            timeout=15,
        )
    return True


def _settled_marker(path: Path) -> Path:
    return path.with_name(path.stem + ".claim-settled")


def _failed_records(root: Path):
    for path in sorted(root.glob("*.job.json")):
        if _settled_marker(path).exists():
            continue
        if path.with_name(path.stem + ".failed.json").exists():
            try:
                yield path, json.loads(plan.read_private(path))
            except (OSError, ValueError):
                continue


def _remote_predecessor_retry_needed(root: Path, expected_job: dict):
    """Allow one new job after the known local-only remote-plan refusal."""
    if expected_job.get("remote_predecessor_recovery") is True:
        return False
    for path, prior in _failed_records(root):
        if prior != expected_job:
            continue
        try:
            failure = json.loads(plan.read_private(path.with_name(path.stem + ".failed.json")))
        except (OSError, ValueError):
            continue
        if failure.get("reason") == "operator test plan is invalid or stale":
            retry_job = dict(expected_job, remote_predecessor_recovery=True)
            retry_id = plan.sha(
                json.dumps(retry_job, sort_keys=True, separators=(",", ":")).encode()
            )
            return not root.joinpath(retry_id + ".job.json").exists()
    return False


def _release_failed_claims(home: Path, skc: str, actor: str, root: Path, limit: int = 4):
    """Return cards whose qualification job failed while still holding their claim.

    A failed job leaves _records(), so neither the reaper (which preserves only
    unfinished jobs) nor the advance loop ever looked at it again. When the
    failure was written by the remote executor (consume_remote) or by the
    batch advance, nothing released the exact claim: the card sat in DOING
    owned by <actor>-requal-<card> indefinitely and was absent from every
    pool (a69ec3a8, 90abb007, 2587020f on 2026-10-09).
    """
    states = []
    # Every failed record is re-read each cycle, and nearly all of them no
    # longer hold their claim. A fresh CardStore per record replays the whole
    # shared legacy overlay on its first fold: measured on chiap08 2026-10-09,
    # 64 such folds cost ~33s of the lane selection phase. One store serves the
    # reads until a release runs; any release attempt (result not None) may
    # have changed the board, so the next read starts a fresh store.
    store = None
    for _path, job in _failed_records(root):
        if len(states) >= limit:
            break
        card_id, owner, revision = job.get("card"), job.get("owner"), job.get("claim_revision")
        if not (
            isinstance(card_id, str)
            and isinstance(owner, str)
            and owner.endswith("-requal-" + card_id)
            and isinstance(revision, str)
            and revision
        ):
            continue
        try:
            if store is None:
                store = CardStore(home)
            result = _release_exact_claim(home, skc, actor, card_id, owner, revision, store=store)
        except (OSError, ValueError, subprocess.SubprocessError):
            result = False
        if result is None:
            # No longer the exact holder: claim revisions are unique, so this job can
            # never hold its claim again. Remember that instead of re-folding the card
            # every cycle (620 settled records cost ~7 s per cycle on chiap08).
            try:
                _settled_marker(_path).touch(mode=0o600)
            except OSError:
                pass
        if result is not None:
            store = None
            states.append(card_id + "=" + ("released" if result else "release-refused"))
    return states


def _begin(home, policy, skc, actor, card_id, core, workspace, owner, claim_revision):
    expected = profile.contract(dict(core, id=card_id))
    try:
        value, predecessor = profile.read_profile(home, card_id)
        initial_profile = None
    except FileNotFoundError:
        value, predecessor = None, None
        initial_profile = True
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
    source_sha256 = plan.workspace_source_fingerprint(expected["repository"], workspace)
    execution_host = _execution_host(home, policy, card_id, source_sha256)
    if execution_host == policy["authority_host"] and not _remote_source_reconstruction_failed(
        home, policy["authority_host"], card_id, source_sha256
    ):
        return "deferred:no-ready-remote-host"
    if initial_profile:
        recipe, node_environment = profile.initial_recipe(core, workspace)
        recipe = _all_vitest_targets(recipe, workspace)
        recipe_sha256 = plan.sha(
            json.dumps(recipe, sort_keys=True, separators=(",", ":")).encode()
        )
        initial_profile = profile.profile_value(
            dict(core, id=card_id),
            policy,
            recipe,
            actor,
            recipe_sha256,
            source_sha256=source_sha256,
            node_environment=node_environment,
        )
    else:
        legacy = profile.legacy_full_qualification_required(value, expected, policy)
        if any(value.get(key) != v for key, v in expected.items()) or (
            not legacy
            and not profile.fingerprint_only_stale(
                value, expected, policy, source_sha256=source_sha256, home=home
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
        "execution_host": execution_host,
        "qualification_implementation_sha256": _qualification_implementation_sha256(),
    }
    if initial_profile:
        try:
            predecessor_plan, _, plan_predecessor = plan.load_plan(
                home, binding, allow_completed=True, allow_remote_host=True
            )
        except FileNotFoundError:
            predecessor_plan = None
            plan_predecessor = None
        job.update(
            initial_profile=True,
            initial_profile_value=initial_profile,
            plan_predecessor_sha256=plan_predecessor,
        )
    root = _private_root(home)
    if (
        initial_profile
        and predecessor_plan is not None
        and predecessor_plan.get("remote_initial_profile_qualification") is True
        and plan_predecessor is not None
        and _remote_predecessor_retry_needed(root, job)
    ):
        job["remote_predecessor_recovery"] = True
    job_id = plan.sha(json.dumps(job, sort_keys=True, separators=(",", ":")).encode())
    path = root / (job_id + ".job.json")
    try:
        plan.write_once(path, job)
    except FileExistsError:
        if json.loads(plan.read_private(path)) != job:
            raise plan.TestEvidenceError("profile qualification job identity conflicts")
        failed = path.with_name(path.stem + ".failed.json")
        if failed.exists():
            failure = json.loads(plan.read_private(failed))
            return "failed:" + str(failure.get("reason", "prior qualification failure"))[:160]
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
    initial_profile = job.get("initial_profile") is True
    profile_value = job.get("initial_profile_value") if initial_profile else None
    plan.source_state(workspace, job["binding"])
    try:
        current_plan, _, plan_sha = plan.load_plan(
            home, job["binding"], allow_completed=True, allow_remote_host=True
        )
        if initial_profile:
            if current_plan.get("initial_profile_qualification") is not True:
                raise plan.TestEvidenceError("another test plan owns this source revision")
            previous_profile = current_plan.get("profile")
            same_profile = previous_profile == profile_value
            predecessor = job.get("plan_predecessor_sha256")
            if not same_profile and (
                plan_sha != predecessor
                or not _compatible_initial_profile(previous_profile, profile_value)
            ):
                raise plan.TestEvidenceError("another test plan owns this source revision")
            if (
                not same_profile
                or (
                    isinstance(job.get("execution_host"), str)
                    and current_plan.get("host") != job["execution_host"]
                )
                or (
                    initial_profile
                    and _remote_initial_preflight_failure(home, job["binding"], plan_sha)
                )
            ):
                launched = (plan.run_directory(home, plan_sha) / "launch.json").exists()
                recover_remote_preflight = False
                if launched and (same_profile or not plan._completed_test_attempt(home, plan_sha)):
                    recover_remote_preflight = (
                        same_profile
                        and initial_profile
                        and _remote_initial_preflight_failure(home, job["binding"], plan_sha)
                    )
                    if not recover_remote_preflight:
                        reason = (
                            "qualification plan host changed after launch"
                            if same_profile
                            else "qualification plan changed after launch"
                        )
                        raise plan.TestEvidenceError(reason)
                kwargs = {"acceptance_requalification": True} if launched else {}
                if recover_remote_preflight:
                    kwargs["acceptance_requalification"] = True
                context = (
                    _remote_predecessor_for_seal(home, job["binding"])
                    if initial_profile
                    else nullcontext()
                )
                with context:
                    recovery_context = (
                        _permit_remote_initial_retry(home, job["binding"], plan_sha)
                        if recover_remote_preflight
                        else nullcontext()
                    )
                    with recovery_context:
                        plan.seal_plan(
                            home,
                            job["binding"],
                            workspace,
                            policy,
                            profile_value["qualified_by"],
                            profile_value["qualification_sha256"],
                            profile=profile_value,
                            predecessor_sha256=plan_sha,
                            execution_host=job["execution_host"],
                            initial_profile_qualification=True,
                            **kwargs,
                        )
        elif (
            current_plan.get("profile_requalification") is not True
            or current_plan.get("profile_predecessor_sha256") != job["profile_sha256"]
        ):
            raise plan.TestEvidenceError("another test plan owns this source revision")
    except FileNotFoundError:
        if initial_profile:
            value = profile_value
            plan.seal_plan(
                home,
                job["binding"],
                workspace,
                policy,
                value["qualified_by"],
                value["qualification_sha256"],
                profile=value,
                execution_host=job.get("execution_host"),
                initial_profile_qualification=True,
            )
        else:
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
        if initial_profile:
            raise
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
    with _remote_initial_worker(home, job["binding"]):
        receipt = tests.run_or_read_tests(home, job["binding"], workspace, policy)
    if receipt is None:
        return "pending"
    if initial_profile:
        with _remote_initial_worker(home, job["binding"]):
            tests.validate_test_receipt(home, job["binding"], workspace)
        _qualify_profile_or_replay(
            home,
            card.model_dump(mode="json"),
            policy,
            profile_value["recipe"],
            profile_value["qualified_by"],
            receipt["receipt_sha256"],
            source_sha256=job["source_sha256"],
            node_environment=profile_value.get("node_environment"),
        )
    else:
        value, predecessor = profile.read_profile(home, job["card"])
    if not initial_profile and predecessor == job["profile_sha256"]:
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
            node_environment=_successor_environment(value),
        )
    elif not initial_profile and value.get("qualification_sha256") != receipt["receipt_sha256"]:
        raise plan.TestEvidenceError("profile predecessor changed")
    import subprocess

    latest = CardStore(home).fold(job["card"])
    if latest is None:
        raise plan.TestEvidenceError("profile qualified but source card disappeared")
    finished_verdict = _retained_finished_claim(home, job, latest)
    if finished_verdict is not None:
        plan.write_once(
            path.with_name(path.stem + ".done.json"),
            {
                "schema": "skfleet.profile-requalification-receipt/v1",
                "card": job["card"],
                "profile_sha256": profile.read_profile(home, job["card"])[1],
                "test_receipt_sha256": receipt["receipt_sha256"],
                "claim_retained_verdict": finished_verdict,
            },
        )
        return "qualified-held-finished-work"
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

    if not store.actuation_allowed(_fleet_paths(home)):
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
                # A completed run is validated against its sealed environment,
                # just as on the authority. Incomplete runs still require the
                # current runtime; run_or_read_tests verifies all receipt proofs.
                sealed, _, _ = plan.load_plan(home, job["binding"], allow_completed=True)
                initial = job.get("initial_profile") is True
                if initial:
                    if (
                        sealed.get("remote_initial_profile_qualification") is not True
                        or sealed.get("initial_profile_qualification") is not True
                        or sealed.get("authority_host") != policy.get("authority_host")
                        or sealed.get("host") != host
                        or sealed.get("profile") != job.get("initial_profile_value")
                    ):
                        raise plan.TestEvidenceError("remote initial qualification plan changed")
                elif (
                    sealed.get("remote_requalification") is not True
                    or sealed.get("authority_host") != policy.get("authority_host")
                    or sealed.get("host") != host
                ):
                    raise plan.TestEvidenceError("remote qualification plan binding changed")
                if initial:
                    value = job["initial_profile_value"]
                    if value.get("card") != job["card"]:
                        raise plan.TestEvidenceError("remote initial profile card changed")
                else:
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
                if initial:
                    recipe, node_environment = profile.initial_recipe(
                        card.model_dump(mode="json"), workspace
                    )
                    if (
                        recipe != value["recipe"]
                        or node_environment != value.get("node_environment")
                        or value.get("source_sha256") != job.get("source_sha256")
                    ):
                        raise plan.TestEvidenceError(
                            "remote initial qualification source recipe changed"
                        )
                receipt = tests.run_or_read_tests(home, job["binding"], workspace, policy)
                states.append(
                    "running:" + job["card"] if receipt is None else "receipt:" + job["card"]
                )
            except FileNotFoundError as exc:
                # The authority writes the job, plan and claim; Syncthing can
                # deliver the job before the plan or the card events. A missing
                # file inside the sync window is not a verdict: retry next tick
                # (2026-10-09: jobs failed on missing test-plans/... files).
                if time.time() - path.stat().st_mtime < SYNC_GRACE_SECONDS:
                    states.append("waiting-sync:" + str(job.get("card")))
                    continue
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
