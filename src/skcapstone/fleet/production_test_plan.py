"""Private operator plans, exact source state and fixed trial coverage contracts."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree

from . import production_builder
from .production_policy import _unique_object
from .qualified_runtime import TOOL_PACKAGES

PREFIX = Path.home() / ".skenv"
MAX_OUTPUT = 4 * 1024 * 1024
TEST_FILES = tuple(
    "tests/" + name + ".py"
    for name in (
        "test_scheduler_decision",
        "test_skfleet_pool_v2_authority",
        "test_skfleet_claimability",
        "test_skfleet_terminal_review_skip",
        "test_skfleet_review_metadata",
        "test_skfleet_review_claim_release",
        "test_skfleet_assigned_review_observation",
        "test_fleet_duplicate_admission",
        "test_skfleet_awaiting_gates",
        "test_skfleet_provisional_opener",
        "test_seraph_dispatcher_path",
        "test_provisional_verdict_producer",
    )
)
BINDING_KEYS = frozenset(
    {
        "source_card",
        "source_owner",
        "source_claim_revision",
        "source_head",
        "source_tree",
        "source_revision",
        "criteria_sha256",
    }
)
_RUNTIME_CACHE = {}
HARNESS_ROOT = Path(__file__).resolve().parent
HARNESS_MODULES = (
    "qualified_runtime.py",
    "production_pytest_recipe.py",
    "production_pytest_selection.py",
    "production_test_plan.py",
    "production_test_profile.py",
    "production_test_worker.py",
    "production_test_node.py",
    "production_tests.py",
    "production_admission.py",
    "production_builder.py",
    "production_policy.py",
)


def runtime_fingerprint() -> str:
    """Pin the test toolchain and trusted executor, not package install provenance.

    Cache only exact inode/size/mtime/ctime observations, never a timed success.
    Candidate source is bound separately and its tests run from /work/src. A
    new SKCapstone wheel RECORD or unrelated module cannot change this contract.
    """
    site = (
        PREFIX
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    files = {PREFIX / "bin/ruff"}
    for package in TOOL_PACKAGES:
        directory = site / package
        if not directory.is_dir():
            raise TestEvidenceError("qualified runtime dependency is missing: " + package)
        files.update(
            path for path in directory.rglob("*") if path.suffix in {".py", ".so", ".pyd"}
        )
    for tool in ("pytest", "pytest_asyncio", "ruff", "pluggy", "iniconfig", "packaging"):
        files.update(site.glob(tool + "-*.dist-info/METADATA"))
    files.update(
        path for path in (site / "sitecustomize.py", site / "usercustomize.py") if path.exists()
    )
    startup = sorted(site.glob("*.pth"))
    harness = [HARNESS_ROOT / name for name in HARNESS_MODULES]
    if len(files) + len(startup) + len(harness) > 10000:
        raise TestEvidenceError("qualified runtime fingerprint exceeds file bound")
    rows, total = [], 0

    def add(path: Path, label: str, root: Path) -> None:
        nonlocal total
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or not path.resolve().is_relative_to(root.resolve()):
            raise TestEvidenceError("qualified runtime contains redirected modules")
        total += info.st_size
        if total > 64 * 1024 * 1024:
            raise TestEvidenceError("qualified runtime fingerprint exceeds byte bound")
        key = (str(path), info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        digest = _RUNTIME_CACHE.get(key)
        if digest is None:
            digest = sha(path.read_bytes())
            _RUNTIME_CACHE[key] = digest
        rows.append((label, digest))

    for path in sorted(files):
        add(path, str(path.relative_to(PREFIX)), PREFIX)
    startup_names = set()
    for path in startup:
        name = path.name
        if name.startswith("__editable__."):
            package, separator, version = name[:-4].rpartition("-")
            if separator and version[:1].isdigit():
                name = package + ".pth"
        if name in startup_names:
            raise TestEvidenceError("qualified runtime startup files are ambiguous")
        startup_names.add(name)
        # Editable .pth filenames contain a wheel version that does not alter
        # startup behavior. Keep the package name and ordering, and hash bytes.
        add(path, "startup/" + name, PREFIX)
    for path in harness:
        add(path, "harness/" + path.name, HARNESS_ROOT)
    return sha(json.dumps(rows, separators=(",", ":")).encode())


class TestEvidenceError(ValueError):
    """Missing, ambiguous, stale or untrusted native test evidence."""

    __test__ = False


def sha(raw: bytes) -> str:
    """Hash exact persisted bytes."""
    return hashlib.sha256(raw).hexdigest()


def private_dir(path: Path, *, create=False) -> None:
    """Require an owned private real directory, including existing directories."""
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise TestEvidenceError("test evidence directory is not private")


def read_private(path: Path, maximum=MAX_OUTPUT) -> bytes:
    """Bound reads of owned single-link regular files without following symlinks."""
    private_dir(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise TestEvidenceError("test evidence file is not private")
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise TestEvidenceError("test evidence exceeds read bound")
    return raw


def write_once(path: Path, value: dict) -> None:
    """Publish an immutable native record, refusing any existing destination."""
    private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_json(path: Path) -> dict:
    """Parse bounded native evidence without ambiguous duplicate JSON keys."""
    value = json.loads(read_private(path), object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise TestEvidenceError("test evidence is not an object")
    return value


def check_binding(binding: dict) -> None:
    """Validate exact keys before constructing any evidence path."""
    if (
        set(binding) != BINDING_KEYS
        or any(not isinstance(v, str) or not v for v in binding.values())
        or not re.fullmatch(r"[0-9a-f]{8}", binding["source_card"])
        or any(
            not re.fullmatch(r"[0-9a-f]{40}", binding[k]) for k in ("source_head", "source_tree")
        )
        or not re.fullmatch(r"[0-9a-f]{64}", binding["criteria_sha256"])
    ):
        raise TestEvidenceError("invalid exact source binding")


def approved_checks() -> list[dict]:
    """The initial qualified trial profile has no model-controlled command text."""
    python = str(PREFIX / "bin/python")
    return [
        {
            "id": "pytest",
            "argv": [
                python,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--junitxml=/output/pytest.xml",
                *TEST_FILES,
            ],
        },
        {"id": "compile", "argv": [python, "-m", "py_compile", "scripts/fleet/skfleet-rotate.py"]},
        {
            "id": "lint",
            "argv": [python, "-m", "ruff", "check", "tests/test_skfleet_terminal_review_skip.py"],
        },
        {"id": "changelog", "argv": [python, "scripts/changelog_fragments.py", "--check"]},
    ]


def seal_plan(
    home: Path,
    binding: dict,
    workspace: Path,
    policy: dict,
    qualified_by: str,
    qualification_sha256: str,
    *,
    profile: dict | None = None,
    predecessor_sha256: str | None = None,
) -> Path:
    """Seal an explicitly qualified legacy or reusable profile for one candidate."""
    if predecessor_sha256 is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", predecessor_sha256):
            raise TestEvidenceError("test plan predecessor invalid")
        with _plan_run_lock(home, predecessor_sha256):
            return _seal_plan(
                home,
                binding,
                workspace,
                policy,
                qualified_by,
                qualification_sha256,
                profile,
                predecessor_sha256,
            )
    return _seal_plan(
        home,
        binding,
        workspace,
        policy,
        qualified_by,
        qualification_sha256,
        profile,
        predecessor_sha256,
    )


@contextmanager
def _plan_run_lock(home: Path, fingerprint: str):
    """Fence successor publication against an old generation's test launch."""
    root = home / "fleet/test-runs"
    private_dir(root, create=True)
    directory = run_directory(home, fingerprint)
    private_dir(directory, create=True)
    fd = os.open(directory / ".run.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def _seal_plan(
    home: Path,
    binding: dict,
    workspace: Path,
    policy: dict,
    qualified_by: str,
    qualification_sha256: str,
    profile: dict | None,
    predecessor_sha256: str | None,
) -> Path:
    check_binding(binding)
    source_state(workspace, binding)
    directory = home / "fleet/test-plans"
    private_dir(directory, create=True)
    base = directory / (binding["source_card"] + "-" + binding["source_head"] + ".json")
    path = base
    if predecessor_sha256 is not None:
        previous, _, current = load_plan(home, binding, require_current=False)
        if current != predecessor_sha256:
            raise TestEvidenceError("test plan predecessor changed")
        if (run_directory(home, current) / "launch.json").exists():
            raise TestEvidenceError("prior test plan already launched")
        path = base.with_name(base.stem + "." + current + ".json")
    value = {
        "schema": (
            "skfleet.native-test-plan/v2"
            if predecessor_sha256 is not None
            else "skfleet.native-test-plan/v1"
        ),
        "binding": binding,
        "checks": approved_checks(),
        "qualified_by": qualified_by,
        "qualification_sha256": qualification_sha256,
        "python_sha256": sha((PREFIX / "bin/python").read_bytes()),
        "runtime_sha256": runtime_fingerprint(),
        "host": socket.gethostname().split(".")[0].lower(),
        "policy_sha256": production_builder.digest(policy),
    }
    if predecessor_sha256 is not None:
        value["predecessor_sha256"] = predecessor_sha256
    if profile is not None:
        from .production_test_profile import recipe_checks, validate_profile

        validate_profile(
            profile,
            {"card": binding["source_card"], "criteria_sha256": binding["criteria_sha256"]},
            policy,
        )
        value["profile"] = profile
        value["checks"] = recipe_checks(profile["recipe"])
        from . import production_test_node as node

        if node.is_node(profile):
            node.validate_environment(profile["node_environment"], workspace)
        else:
            from .production_pytest_recipe import validate_source

            validate_source(profile["recipe"], workspace)
    if (
        not qualified_by
        or not re.fullmatch(r"[0-9a-f]{64}", qualification_sha256)
        or value["host"] != policy["authority_host"]
    ):
        raise TestEvidenceError("operator qualification is incomplete")
    if predecessor_sha256 is not None and all(
        previous[key] == value[key]
        for key in ("qualification_sha256", "python_sha256", "runtime_sha256", "policy_sha256")
    ):
        raise TestEvidenceError("test plan successor lacks fresh qualification")
    write_once(path, value)
    return path


def load_plan(
    home: Path, binding: dict, *, require_current: bool = True, allow_completed: bool = False
) -> tuple[dict, Path, str]:
    """Read an append-only plan chain and bind its latest approved environment."""
    check_binding(binding)
    base = (
        home
        / "fleet/test-plans"
        / (binding["source_card"] + "-" + binding["source_head"] + ".json")
    )
    path = base
    raw = read_private(path)
    seen = set()
    for generation in range(128):
        fingerprint = sha(raw)
        if fingerprint in seen:
            raise TestEvidenceError("test plan chain repeats")
        seen.add(fingerprint)
        plan = json.loads(raw, object_pairs_hook=_unique_object)
        _validate_plan(home, binding, plan, successor=generation > 0)
        next_path = base.with_name(base.stem + "." + fingerprint + ".json")
        try:
            next_raw = read_private(next_path)
        except FileNotFoundError:
            extras = list(base.parent.glob(base.stem + ".*.json"))
            if len(extras) != generation:
                raise TestEvidenceError("test plan chain is disconnected") from None
            if require_current:
                try:
                    _validate_current_plan(plan)
                except TestEvidenceError:
                    run = run_directory(home, fingerprint)
                    if not allow_completed or not all(
                        (run / name).exists()
                        for name in ("launch.json", "receipt.json", "terminal.json")
                    ):
                        raise
            return plan, path, fingerprint
        if (run_directory(home, fingerprint) / "launch.json").exists():
            raise TestEvidenceError("prior test plan already launched")
        next_plan = json.loads(next_raw, object_pairs_hook=_unique_object)
        if not isinstance(next_plan, dict) or next_plan.get("predecessor_sha256") != fingerprint:
            raise TestEvidenceError("test plan predecessor changed")
        path, raw = next_path, next_raw
    raise TestEvidenceError("test plan chain exceeds generation bound")


def _validate_plan(home: Path, binding: dict, plan: dict, *, successor: bool) -> None:
    if not isinstance(plan, dict):
        raise TestEvidenceError("operator test plan is invalid or stale")
    required = {
        "schema",
        "binding",
        "checks",
        "qualified_by",
        "qualification_sha256",
        "python_sha256",
        "runtime_sha256",
        "host",
        "policy_sha256",
    }
    if successor:
        required.add("predecessor_sha256")
    expected_checks = approved_checks()
    if "profile" in plan:
        from .production_test_profile import read_profile, recipe_checks

        required.add("profile")
        profile, _ = read_profile(home, binding["source_card"], pinned=plan["profile"])
        if (
            profile != plan["profile"]
            or profile.get("card") != binding["source_card"]
            or profile.get("criteria_sha256") != binding["criteria_sha256"]
            or any(
                profile.get(k) != plan.get(k)
                for k in (
                    "qualified_by",
                    "qualification_sha256",
                    "python_sha256",
                    "runtime_sha256",
                    "policy_sha256",
                    "host",
                )
            )
        ):
            raise TestEvidenceError("candidate test profile changed")
        expected_checks = recipe_checks(profile["recipe"])
        from . import production_test_node as node

        if node.is_node(profile):
            node.validate_environment(profile["node_environment"])
    if (
        set(plan) != required
        or plan["schema"]
        != ("skfleet.native-test-plan/v2" if successor else "skfleet.native-test-plan/v1")
        or plan["binding"] != binding
        or plan["checks"] != expected_checks
        or not isinstance(plan["qualified_by"], str)
        or not plan["qualified_by"]
        or any(
            not re.fullmatch(r"[0-9a-f]{64}", str(plan[k]))
            for k in ("qualification_sha256", "python_sha256", "runtime_sha256", "policy_sha256")
        )
        or plan["host"] != socket.gethostname().split(".")[0].lower()
    ):
        raise TestEvidenceError("operator test plan is invalid or stale")


def _validate_current_plan(plan: dict) -> None:
    if (
        plan["python_sha256"] != sha((PREFIX / "bin/python").read_bytes())
        or plan["runtime_sha256"] != runtime_fingerprint()
    ):
        raise TestEvidenceError("operator test plan is invalid or stale")


def source_state(workspace: Path, binding: dict) -> dict:
    """Check actual Git source, including ignored/untracked files and linked worktrees."""

    def git(*args):
        result = subprocess.run(
            ["/usr/bin/git", "-C", str(workspace), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
            env={
                "PATH": "/usr/bin",
                "HOME": "/nonexistent",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.fsmonitor",
                "GIT_CONFIG_VALUE_0": "false",
            },
        )
        return result.stdout.strip()

    state = {"head": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}")}
    if (
        state != {"head": binding["source_head"], "tree": binding["source_tree"]}
        or git("status", "--porcelain", "--untracked-files=all", "--ignored")
        or git("ls-files", "--stage").find("160000 ") >= 0
    ):
        raise TestEvidenceError("test source is changed, dirty, or contains submodules")
    return state


def run_directory(home: Path, plan_sha: str) -> Path:
    """Return the single immutable attempt directory for the exact plan bytes."""
    return home / "fleet/test-runs" / plan_sha


def junit_counts(
    raw: bytes, profile: dict | None = None, *, selection: dict | None = None
) -> dict:
    """Recompute strict per-file coverage from raw JUnit, never reported totals alone."""
    from . import production_test_node as node

    if node.is_node(profile):
        return node.junit_counts(raw, profile)
    from .production_pytest_recipe import requires_selection, selected_junit_counts

    if profile and requires_selection(profile["recipe"]):
        return selected_junit_counts(raw, profile, selection)
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise TestEvidenceError("JUnit entities are forbidden")
    root = ElementTree.fromstring(raw)
    cases = list(root.iter("testcase"))
    minima = (
        profile["recipe"]["pytest"]
        if profile
        else {
            name: 11 if name.endswith("test_skfleet_terminal_review_skip.py") else 1
            for name in TEST_FILES
        }
    )
    counts = {name: 0 for name in minima}
    identities = set()
    for case in cases:
        if any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
            raise TestEvidenceError("required tests failed, errored or skipped")
        classname = case.get("classname", "")
        matching = [
            name
            for name in minima
            if any(
                classname == prefix or classname.startswith(prefix + ".")
                for prefix in (name[:-3].replace("/", "."), Path(name).stem)
            )
        ]
        if len(matching) != 1:
            raise TestEvidenceError("JUnit includes an unexpected test file")
        name = matching[0]
        identity = (classname, case.get("name"))
        if not identity[1] or identity in identities:
            raise TestEvidenceError("JUnit testcase identity is missing or duplicated")
        identities.add(identity)
        counts[name] += 1
    suites = list(root.iter("testsuite"))
    if (
        not suites
        or any(int(s.get(k, "-1")) != 0 for s in suites for k in ("failures", "errors", "skipped"))
        or sum(int(s.get("tests", "-1")) for s in suites) != len(cases)
        or any(counts[name] < minimum for name, minimum in minima.items())
        or (profile is None and len(cases) < 226)
    ):
        raise TestEvidenceError("JUnit coverage does not meet the qualified plan")
    return {"total": len(cases), "per_file": counts, "failures": 0, "errors": 0, "skipped": 0}
