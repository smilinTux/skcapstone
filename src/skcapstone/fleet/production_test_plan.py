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

from . import production_builder  # noqa: F401
from .production_policy import _unique_object
from .qualified_runtime import STATE_NAME, TOOL_PACKAGES, qualify_prefix

#: Sealed test execution runs only from the clean pinned qualification prefix
#: built by ``qualify_env``; production ~/.skenv is never the test runtime.
PREFIX = qualify_prefix(Path.home())
#: The pre-qualify-env runtime. Read only to recognize profiles whose
#: toolchain was qualified there, so they can be requalified once natively.
LEGACY_PREFIX = Path.home() / ".skenv"
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
_TOOLCHAIN_CACHE = {}


# The sealed sandbox runs the harness from the qualified prefix, and both the
# executing worker (qualify-env interpreter) and its validator (sknoded under
# ~/.skenv) must name the same files. Deriving this from __file__ gave each its
# own site-packages path, so every pytest-recipe receipt failed validation as
# "native command or raw output mismatch" although all checks passed
# (a0834032/ebe42d9e on chiap04, f3484694 on chiap01/02, 2026-10-09).
def harness_root(prefix: Path, fallback: Path) -> Path:
    """The qualified prefix's harness directory when installed, else ``fallback``."""
    candidate = (
        prefix
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
        / "skcapstone"
        / "fleet"
    )
    return candidate if (candidate / "production_pytest_selection.py").is_file() else fallback


HARNESS_ROOT = harness_root(PREFIX, Path(__file__).resolve().parent)
HARNESS_MODULES = (
    "qualified_runtime.py",
    "production_pytest_recipe.py",
    "production_pytest_selection.py",
    "production_test_plan.py",
    "production_test_profile.py",
    "production_test_worker.py",
    "production_test_node.py",
    "production_test_composite.py",
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
    state = PREFIX / STATE_NAME
    if not state.is_file():
        raise TestEvidenceError("qualified runtime manifest is missing")
    files = {PREFIX / "bin/ruff", state}
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


def toolchain_fingerprint() -> str:
    """Pin test tools and Python startup files separately from executor code.

    The qualification prefix must carry its build manifest, which binds the
    exact locked distribution set.
    """
    return _toolchain_fingerprint(PREFIX, legacy=False)


def _toolchain_fingerprint(prefix: Path, *, legacy: bool) -> str:
    """Hash one prefix's toolchain; ``legacy`` keeps the historical row set."""
    site = (
        prefix
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    files = {prefix / "bin/ruff", prefix / "bin/python"}
    if not legacy:
        if not (prefix / STATE_NAME).is_file():
            raise TestEvidenceError("qualified runtime manifest is missing")
        files.add(prefix / STATE_NAME)
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
    if len(files) + len(startup) > 10000:
        raise TestEvidenceError("qualified toolchain fingerprint exceeds file bound")
    rows, total = [], 0
    for path in sorted(files) + startup:
        info = path.lstat()
        content_path = path
        resolved = path.resolve()
        if path == prefix / "bin/python" and stat.S_ISLNK(info.st_mode):
            info = resolved.stat()
            if not stat.S_ISREG(info.st_mode) or not resolved.is_relative_to(
                Path(sys.base_prefix).resolve()
            ):
                raise TestEvidenceError("qualified interpreter escapes its base prefix")
            content_path = resolved
        elif not stat.S_ISREG(info.st_mode) or not resolved.is_relative_to(prefix.resolve()):
            raise TestEvidenceError("qualified runtime contains redirected modules")
        total += info.st_size
        if total > 64 * 1024 * 1024:
            raise TestEvidenceError("qualified toolchain fingerprint exceeds byte bound")
        key = (str(path), info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        digest = _TOOLCHAIN_CACHE.get(key)
        if digest is None:
            digest = sha(content_path.read_bytes())
            _TOOLCHAIN_CACHE[key] = digest
        label = str(path.relative_to(prefix))
        if path.suffix == ".pth":
            name = path.name
            if name.startswith("__editable__."):
                package, separator, version = name[:-4].rpartition("-")
                if separator and version[:1].isdigit():
                    name = package + ".pth"
            label = "startup/" + name
        rows.append((label, digest))
    return sha(json.dumps(rows, separators=(",", ":")).encode())


def legacy_toolchain_fingerprint() -> str | None:
    """Fingerprint the pre-qualify-env ~/.skenv toolchain, or None when absent."""
    try:
        return _toolchain_fingerprint(LEGACY_PREFIX, legacy=True)
    except (OSError, ValueError):
        return None


def source_fingerprint(repository: str, head: str, tree: str) -> str:
    """Bind a profile to the exact repository tree whose recipe was qualified."""
    if (
        not isinstance(repository, str)
        or not repository.startswith("https://")
        or "@" in repository
    ):
        raise TestEvidenceError("test repository binding is invalid")
    if not re.fullmatch(r"[0-9a-f]{40,64}", str(head)) or not re.fullmatch(
        r"[0-9a-f]{40,64}", str(tree)
    ):
        raise TestEvidenceError("qualified source state is invalid")
    return sha(
        json.dumps(
            {"repository": repository, "head": head, "tree": tree},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    )


def workspace_source_fingerprint(repository: str, workspace: Path) -> str:
    """Hash a clean checkout without trusting a caller-supplied revision."""
    try:
        head = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--verify", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--verify", "HEAD^{tree}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(workspace), "status", "--porcelain", "--untracked-files=all"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise TestEvidenceError("qualified source state is unavailable") from exc
    if dirty:
        raise TestEvidenceError("qualified source is dirty")
    return source_fingerprint(repository, head, tree)


class TestEvidenceError(ValueError):
    """Missing, ambiguous, stale or untrusted native test evidence."""

    __test__ = False


def sha(raw: bytes) -> str:
    """Hash exact persisted bytes."""
    return hashlib.sha256(raw).hexdigest()


def execution_policy_fingerprint(policy: dict, host: str | None = None) -> str:
    """Bind test evidence only to the host and limits that execute its tests."""
    host = host or policy.get("authority_host")
    quotas = policy.get("node_quotas")
    resources = quotas.get(host) if isinstance(quotas, dict) else None
    authority = policy.get("authority_host")
    if (
        not isinstance(host, str)
        or not host
        or not isinstance(authority, str)
        or not isinstance(resources, dict)
    ):
        raise TestEvidenceError("test execution policy is incomplete")
    value = {"authority_host": authority, "resources": resources}
    if host != authority:
        value["execution_host"] = host
    return sha(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


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
    requalification: bool = False,
    profile_predecessor_sha256: str | None = None,
    execution_host: str | None = None,
    initial_profile_qualification: bool = False,
    acceptance_requalification: bool = False,
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
                requalification,
                profile_predecessor_sha256,
                execution_host,
                initial_profile_qualification,
                acceptance_requalification,
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
        requalification,
        profile_predecessor_sha256,
        execution_host,
        initial_profile_qualification,
        acceptance_requalification,
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


def _plan_base(home: Path, binding: dict) -> Path:
    """Keep each exact claim revision's native test-plan chain separate."""
    revision = sha(binding["source_revision"].encode())[:16]
    return (
        home
        / "fleet/test-plans"
        / (binding["source_card"] + "-" + binding["source_head"] + "-" + revision + ".json")
    )


def _initial_plan_base(home: Path, binding: dict) -> Path:
    """Keep initial profile qualification separate from older unstarted plans."""
    base = _plan_base(home, binding)
    return base.with_name(base.stem + ".initial.json")


def _legacy_plan_base(home: Path, binding: dict) -> Path:
    return (
        home
        / "fleet/test-plans"
        / (binding["source_card"] + "-" + binding["source_head"] + ".json")
    )


def _seal_plan(
    home: Path,
    binding: dict,
    workspace: Path,
    policy: dict,
    qualified_by: str,
    qualification_sha256: str,
    profile: dict | None,
    predecessor_sha256: str | None,
    requalification: bool,
    profile_predecessor_sha256: str | None,
    execution_host: str | None,
    initial_profile_qualification: bool,
    acceptance_requalification: bool = False,
) -> Path:
    check_binding(binding)
    execution_host = execution_host or policy["authority_host"]
    if execution_host not in policy.get("node_quotas", {}):
        raise TestEvidenceError("test execution host is not in production policy")
    source_state(workspace, binding)
    directory = home / "fleet/test-plans"
    private_dir(directory, create=True)
    base = (
        _initial_plan_base(home, binding)
        if initial_profile_qualification
        else _plan_base(home, binding)
    )
    path = base
    if predecessor_sha256 is not None:
        previous, previous_path, current = load_plan(home, binding, require_current=False)
        if current != predecessor_sha256:
            raise TestEvidenceError("test plan predecessor changed")
        if (run_directory(home, current) / "launch.json").exists():
            raise TestEvidenceError("prior test plan already launched")
        path = previous_path.with_name(previous_path.stem + "." + current + ".json")
    if initial_profile_qualification and (profile is None or requalification):
        raise TestEvidenceError("initial profile qualification needs one unpublished profile")
    if initial_profile_qualification:
        from .production_test_profile import recipe_checks

    remote_requalification = requalification and execution_host != policy["authority_host"]
    remote_initial = initial_profile_qualification and execution_host != policy["authority_host"]
    value = {
        "schema": (
            "skfleet.native-test-plan/v6"
            if remote_initial
            else (
                "skfleet.native-test-plan/v5"
                if remote_requalification
                else (
                    "skfleet.native-test-plan/v4"
                    if predecessor_sha256 is not None and requalification
                    else (
                        "skfleet.native-test-plan/v3"
                        if requalification
                        else (
                            "skfleet.native-test-plan/v2"
                            if predecessor_sha256 is not None
                            else "skfleet.native-test-plan/v1"
                        )
                    )
                )
            )
        ),
        "binding": binding,
        "checks": (
            recipe_checks(profile["recipe"])
            if initial_profile_qualification and profile is not None
            else approved_checks()
        ),
        "qualified_by": qualified_by,
        "qualification_sha256": qualification_sha256,
        "python_sha256": sha((PREFIX / "bin/python").read_bytes()),
        "runtime_sha256": runtime_fingerprint(),
        "host": socket.gethostname().split(".")[0].lower(),
        "policy_sha256": execution_policy_fingerprint(policy, execution_host),
    }
    if remote_requalification:
        value["authority_host"] = policy["authority_host"]
        value["remote_requalification"] = True
    if initial_profile_qualification:
        value["initial_profile_qualification"] = True
    if remote_initial:
        value["authority_host"] = policy["authority_host"]
        value["remote_initial_profile_qualification"] = True
    value["host"] = execution_host
    if predecessor_sha256 is not None:
        value["predecessor_sha256"] = predecessor_sha256
    if profile is not None:
        from .production_test_profile import (
            fingerprint_only_stale,
            legacy_full_qualification_required,
            read_profile,
            recipe_checks,
            validate_profile,
        )

        expected_profile = {
            "card": binding["source_card"],
            "criteria_sha256": binding["criteria_sha256"],
        }
        if requalification:
            current, current_sha = read_profile(home, binding["source_card"], pinned=profile)
            stale = fingerprint_only_stale(
                current,
                {**expected_profile, "repository": current["repository"]},
                policy,
                source_sha256=(
                    source_fingerprint(
                        current["repository"], binding["source_head"], binding["source_tree"]
                    )
                    if "source_sha256" in current
                    else None
                ),
            ) or legacy_full_qualification_required(
                current, {**expected_profile, "repository": current["repository"]}, policy
            )
            if (
                profile_predecessor_sha256 != current_sha
                or qualified_by != current["qualified_by"]
                or qualification_sha256 != current["qualification_sha256"]
                or not stale
            ):
                raise TestEvidenceError("profile is not eligible for native requalification")
            value["profile_requalification"] = True
            value["profile_predecessor_sha256"] = current_sha
            # fingerprint_only_stale grants a profile still bound to the
            # pre-qualify-env ~/.skenv toolchain one migration requal. Seal the
            # legacy fingerprint it accepted so the executor, whose own legacy
            # ~/.skenv differs, can admit exactly that value and no other.
            if current.get("toolchain_sha256") not in (None, toolchain_fingerprint()):
                value["legacy_toolchain_sha256"] = current["toolchain_sha256"]
        else:
            expected = dict(expected_profile)
            if "source_sha256" in profile:
                expected["source_sha256"] = source_fingerprint(
                    profile["repository"],
                    binding["source_head"],
                    binding["source_tree"],
                )
            try:
                validate_profile(profile, expected, policy)
            except TestEvidenceError:
                # Only acceptance asks for this, after acceptance_requalifiable():
                # its trusted run at this candidate is the requalification.
                from .production_test_profile import acceptance_requalifiable

                if not (
                    acceptance_requalification
                    and acceptance_requalifiable(
                        profile,
                        {**expected_profile, "repository": profile.get("repository")},
                        policy,
                    )
                ):
                    raise
                value["acceptance_requalification"] = True
        value["profile"] = profile
        value["checks"] = recipe_checks(profile["recipe"])

        from .production_test_composite import validate_source

        validate_source(profile, workspace)
    if (
        not qualified_by
        or not re.fullmatch(r"[0-9a-f]{64}", qualification_sha256)
        or value["host"] != execution_host
        or (
            execution_host != policy["authority_host"]
            and not (remote_requalification or remote_initial)
        )
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
    home: Path,
    binding: dict,
    *,
    require_current: bool = True,
    allow_completed: bool = False,
    allow_remote_host: bool = False,
) -> tuple[dict, Path, str]:
    """Read an append-only plan chain and bind its latest approved environment."""
    check_binding(binding)
    base = _plan_base(home, binding)
    initial = _initial_plan_base(home, binding)
    if initial.exists():
        base = initial
    elif not base.exists():
        legacy = _legacy_plan_base(home, binding)
        if legacy.exists():
            try:
                prior = read_json(legacy)
            except (OSError, ValueError):
                prior = None
            if isinstance(prior, dict) and prior.get("binding") == binding:
                base = legacy
    path = base
    raw = read_private(path)
    seen = set()
    for generation in range(128):
        fingerprint = sha(raw)
        if fingerprint in seen:
            raise TestEvidenceError("test plan chain repeats")
        seen.add(fingerprint)
        plan = json.loads(raw, object_pairs_hook=_unique_object)
        _validate_plan(
            home, binding, plan, successor=generation > 0, allow_remote_host=allow_remote_host
        )
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


def _validate_plan(
    home: Path, binding: dict, plan: dict, *, successor: bool, allow_remote_host: bool = False
) -> None:
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
    requalification = plan.get("profile_requalification") is True
    remote_requalification = plan.get("remote_requalification") is True
    initial_qualification = plan.get("initial_profile_qualification") is True
    remote_initial = plan.get("remote_initial_profile_qualification") is True
    if remote_requalification:
        authority = plan.get("authority_host")
        if (
            not requalification
            or not isinstance(authority, str)
            or not authority
            or authority == plan.get("host")
            or plan.get("schema") != "skfleet.native-test-plan/v5"
        ):
            raise TestEvidenceError("remote test plan authority is invalid")
    elif not remote_initial and ("authority_host" in plan or "remote_requalification" in plan):
        raise TestEvidenceError("operator test plan is invalid or stale")
    if initial_qualification:
        required.add("initial_profile_qualification")
    if remote_initial:
        authority = plan.get("authority_host")
        if (
            not initial_qualification
            or not isinstance(authority, str)
            or not authority
            or authority == plan.get("host")
            or plan.get("schema") != "skfleet.native-test-plan/v6"
        ):
            raise TestEvidenceError("remote initial qualification authority is invalid")
        required.update({"authority_host", "remote_initial_profile_qualification"})
    if requalification:
        required.update({"profile_requalification", "profile_predecessor_sha256"})
        if "legacy_toolchain_sha256" in plan:
            if not re.fullmatch(r"[0-9a-f]{64}", str(plan["legacy_toolchain_sha256"])):
                raise TestEvidenceError("operator test plan is invalid or stale")
            required.add("legacy_toolchain_sha256")
    if remote_requalification:
        required.update({"authority_host", "remote_requalification"})
    elif not requalification and "profile_predecessor_sha256" in plan:
        raise TestEvidenceError("operator test plan is invalid or stale")
    expected_checks = approved_checks()
    if "profile" in plan:
        from .production_test_profile import read_profile, recipe_checks

        required.add("profile")
        if plan.get("acceptance_requalification") is True:
            if requalification or initial_qualification:
                raise TestEvidenceError("operator test plan is invalid or stale")
            required.add("acceptance_requalification")
        profile = plan["profile"]
        if initial_qualification:
            from .production_test_profile import _validate_shape

            expected_profile = {
                "card": binding["source_card"],
                "criteria_sha256": binding["criteria_sha256"],
            }
            _validate_shape(profile)
            if (
                any(profile.get(key) != value for key, value in expected_profile.items())
                or profile.get("host") != plan.get("authority_host", plan.get("host"))
                or profile.get("python_sha256") != plan.get("python_sha256")
                or profile.get("runtime_sha256") != plan.get("runtime_sha256")
                or profile.get("source_sha256")
                != source_fingerprint(
                    profile.get("repository", ""),
                    binding["source_head"],
                    binding["source_tree"],
                )
            ):
                raise TestEvidenceError("initial candidate test profile source changed")
            if (
                profile.get("qualified_by") != plan.get("qualified_by")
                or profile.get("qualification_sha256") != plan.get("qualification_sha256")
                or profile.get("python_sha256") != plan.get("python_sha256")
                or profile.get("runtime_sha256") != plan.get("runtime_sha256")
            ):
                raise TestEvidenceError("initial candidate test profile authority changed")
        else:
            profile, profile_sha = read_profile(
                home, binding["source_card"], pinned=plan["profile"]
            )
        if requalification:
            if (
                profile != plan["profile"]
                or profile_sha != plan["profile_predecessor_sha256"]
                or profile.get("card") != binding["source_card"]
                or profile.get("criteria_sha256") != binding["criteria_sha256"]
                or profile.get("host") != plan.get("authority_host", plan.get("host"))
                or profile.get("python_sha256") != plan.get("python_sha256")
                or (
                    "toolchain_sha256" in profile
                    and profile.get("toolchain_sha256")
                    not in {toolchain_fingerprint(), plan.get("legacy_toolchain_sha256")}
                )
                or (
                    "source_sha256" in profile
                    and profile["source_sha256"]
                    != source_fingerprint(
                        profile.get("repository", ""),
                        binding["source_head"],
                        binding["source_tree"],
                    )
                )
                or (
                    profile.get("runtime_sha256") == plan.get("runtime_sha256")
                    and profile.get("policy_sha256") == plan.get("policy_sha256")
                    and not str(profile.get("schema", "")).endswith("/v1")
                    and "legacy_toolchain_sha256" not in plan
                )
            ):
                raise TestEvidenceError("candidate test profile changed")
        elif not initial_qualification and (
            profile != plan["profile"]
            or profile.get("card") != binding["source_card"]
            or profile.get("criteria_sha256") != binding["criteria_sha256"]
            or any(
                profile.get(k) != plan.get(k)
                for k in ("qualified_by", "qualification_sha256", "host")
            )
            # An acceptance requalification seals a profile whose fingerprints
            # differ by design; its trusted run at this candidate refreshes them.
            or (
                plan.get("acceptance_requalification") is not True
                and (
                    any(
                        profile.get(k) != plan.get(k)
                        for k in ("python_sha256", "runtime_sha256", "policy_sha256")
                    )
                    or (
                        "source_sha256" in profile
                        and profile["source_sha256"]
                        != source_fingerprint(
                            profile.get("repository", ""),
                            binding["source_head"],
                            binding["source_tree"],
                        )
                    )
                )
            )
        ):
            raise TestEvidenceError("candidate test profile changed")
        expected_checks = recipe_checks(profile["recipe"])
        from . import production_test_node as node
        from .production_test_composite import is_composite

        if node.is_node(profile) or is_composite(profile):
            node.validate_environment(profile["node_environment"])
    if (
        set(plan) != required
        or plan["schema"]
        != (
            "skfleet.native-test-plan/v6"
            if remote_initial
            else (
                "skfleet.native-test-plan/v5"
                if remote_requalification
                else (
                    "skfleet.native-test-plan/v4"
                    if successor and requalification
                    else (
                        "skfleet.native-test-plan/v3"
                        if requalification
                        else (
                            "skfleet.native-test-plan/v2"
                            if successor
                            else "skfleet.native-test-plan/v1"
                        )
                    )
                )
            )
        )
        or plan["binding"] != binding
        or plan["checks"] != expected_checks
        or not isinstance(plan["qualified_by"], str)
        or not plan["qualified_by"]
        or any(
            not re.fullmatch(r"[0-9a-f]{64}", str(plan[k]))
            for k in ("qualification_sha256", "python_sha256", "runtime_sha256", "policy_sha256")
        )
        or not isinstance(plan.get("host"), str)
        or (plan["host"] != socket.gethostname().split(".")[0].lower() and not allow_remote_host)
        or (
            allow_remote_host
            and plan["host"] != socket.gethostname().split(".")[0].lower()
            and not (remote_requalification or remote_initial)
        )
        or ((remote_requalification or remote_initial) and plan["host"] == plan["authority_host"])
    ):
        raise TestEvidenceError("operator test plan is invalid or stale")


def _validate_current_plan(plan: dict) -> None:
    if (
        plan["python_sha256"] != sha((PREFIX / "bin/python").read_bytes())
        or plan["runtime_sha256"] != runtime_fingerprint()
    ):
        raise TestEvidenceError("operator test plan is invalid or stale")


def _safe_generated_cache(workspace: Path, relative: str) -> bool:
    """Allow only owned, nonsymlink files under inert test-tool cache paths."""
    parts = Path(relative).parts
    if (
        not parts
        or Path(relative).is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
        or not (parts[0] in {".pytest_cache", ".ruff_cache"} or "__pycache__" in parts)
    ):
        return False
    current = Path(workspace)
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = current.lstat()
        except OSError:
            return False
        if info.st_uid != os.getuid():
            return False
        if index < len(parts) - 1:
            if not stat.S_ISDIR(info.st_mode):
                return False
        elif not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            return False
    return True


def _source_status_is_clean(workspace: Path, output: str) -> bool:
    """Reject source edits and unknown files while tolerating inert test caches."""
    records = [record for record in output.split("\0") if record]
    for record in records:
        if len(record) < 4:
            return False
        status, relative = record[:2], record[3:]
        if status not in {"??", "!!"} or not _safe_generated_cache(workspace, relative):
            return False
    return True


def source_state(workspace: Path, binding: dict) -> dict:
    """Check Git source, permitting only inert generated test caches."""

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
        return result.stdout

    state = {
        "head": git("rev-parse", "HEAD").strip(),
        "tree": git("rev-parse", "HEAD^{tree}").strip(),
    }
    if (
        state != {"head": binding["source_head"], "tree": binding["source_tree"]}
        or not _source_status_is_clean(
            workspace,
            git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored"),
        )
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
    if profile and profile.get("recipe") == {"pytest_all": True}:
        if len(raw) > MAX_OUTPUT or b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
            raise TestEvidenceError("invalid full-suite JUnit size or entities")
        root = ElementTree.fromstring(raw)
        cases = list(root.iter("testcase"))
        identities = set()
        for case in cases:
            identity = (case.get("classname"), case.get("name"))
            if (
                any(case.find(tag) is not None for tag in ("failure", "error", "skipped"))
                or not all(isinstance(value, str) and value for value in identity)
                or identity in identities
            ):
                raise TestEvidenceError("full pytest suite failed or returned invalid identities")
            identities.add(identity)
        suites = list(root.iter("testsuite"))
        if (
            not cases
            or not suites
            or any(
                int(s.get(k, "-1")) != 0 for s in suites for k in ("failures", "errors", "skipped")
            )
            or sum(int(s.get("tests", "-1")) for s in suites) != len(cases)
        ):
            raise TestEvidenceError("full pytest suite totals are invalid")
        return {"total": len(cases), "failures": 0, "errors": 0, "skipped": 0}
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
