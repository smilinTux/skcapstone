"""Private operator plans, exact source state and fixed trial coverage contracts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
from pathlib import Path
from xml.etree import ElementTree

from . import production_builder  # noqa: F401
from .production_policy import _unique_object
from .qualified_runtime import STATE_NAME, TOOL_PACKAGES, qualify_prefix

#: Sealed test execution runs only from the clean pinned qualification prefix
#: built by ``qualify_env``; production ~/.skenv is never the test runtime.
PREFIX = qualify_prefix(Path.home())
# A full skcapstone suite (12.7k cases, ~475 bytes of JUnit attributes each)
# writes a ~6 MB report. At 4 MiB the worker's RLIMIT_FSIZE killed pytest while
# it wrote the report, so no full-suite qualification could ever finish.
MAX_OUTPUT = 32 * 1024 * 1024
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
#: One schema, one record per candidate. Earlier predecessor-chain, requalification
#: and remote-qualification generations are not read or written any more.
PLAN_SCHEMA = "skfleet.native-test-plan/v7"
#: Schema of a declared Python recipe record (Node variants carry their own).
PYTHON_RECIPE_SCHEMA = "skfleet.qualified-test-profile/v3"
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


def runtime_fingerprint() -> str:
    """Fingerprint the qualified test runtime, recorded as provenance in each run record.

    No dispatch or acceptance gate compares this value: a plan records it so a result can be
    traced to the runtime that produced it. Cache only exact inode/size/mtime/ctime
    observations, never a timed success.
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
    if len(files) + len(startup) > 10000:
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
    return sha(json.dumps(rows, separators=(",", ":")).encode())


def toolchain_fingerprint() -> str:
    """Fingerprint test tools and Python startup files, recorded as provenance.

    The qualification prefix must carry its build manifest, which binds the
    exact locked distribution set.
    """
    prefix = PREFIX
    site = (
        prefix
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    files = {prefix / "bin/ruff", prefix / "bin/python"}
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


def _plan_base(home: Path, binding: dict) -> Path:
    """Name the single run record of one exact candidate commit and claim revision."""
    revision = sha(binding["source_revision"].encode())[:16]
    return (
        home
        / "fleet/test-plans"
        / (binding["source_card"] + "-" + binding["source_head"] + "-" + revision + ".json")
    )


def seal_plan(
    home: Path,
    binding: dict,
    workspace: Path,
    policy: dict,
    qualified_by: str,
    qualification_sha256: str,
    *,
    profile: dict | None = None,
    execution_host: str | None = None,
) -> Path:
    """Seal the declared tests of one exact candidate as one immutable run record.

    ``profile`` is the card's declared recipe record (``test_recipe.declared_recipe``),
    not a stored or qualified profile: nothing is read from or published to a profile
    store, and the runtime fingerprints are recorded as provenance only. Without one,
    the fixed trial checks run. An existing record is never replaced.
    """
    check_binding(binding)
    execution_host = execution_host or policy["authority_host"]
    if execution_host not in policy.get("node_quotas", {}):
        raise TestEvidenceError("test execution host is not in production policy")
    if not qualified_by or not re.fullmatch(r"[0-9a-f]{64}", qualification_sha256):
        raise TestEvidenceError("operator qualification is incomplete")
    source_state(workspace, binding)
    directory = home / "fleet/test-plans"
    private_dir(directory, create=True)
    value = {
        "schema": PLAN_SCHEMA,
        "binding": binding,
        "checks": approved_checks(),
        "qualified_by": qualified_by,
        "qualification_sha256": qualification_sha256,
        "python_sha256": sha((PREFIX / "bin/python").read_bytes()),
        "toolchain_sha256": toolchain_fingerprint(),
        "runtime_sha256": runtime_fingerprint(),
        "policy_sha256": execution_policy_fingerprint(policy, execution_host),
        "host": execution_host,
    }
    if profile is not None:
        from .production_pytest_recipe import recipe_checks
        from .production_test_composite import validate_source

        _validate_declared(profile, binding)
        value["profile"] = profile
        value["checks"] = recipe_checks(profile["recipe"])
        validate_source(profile, workspace)
    path = _plan_base(home, binding)
    write_once(path, value)
    return path


def load_plan(
    home: Path, binding: dict, *, allow_remote_host: bool = False
) -> tuple[dict, Path, str]:
    """Read the one run record of this exact candidate and validate it against the binding."""
    check_binding(binding)
    path = _plan_base(home, binding)
    raw = read_private(path)
    plan = json.loads(raw, object_pairs_hook=_unique_object)
    _validate_plan(plan, binding, allow_remote_host=allow_remote_host)
    return plan, path, sha(raw)


def _validate_declared(profile: dict, binding: dict) -> None:
    """Check a declared recipe record is exactly this card's contract and a known variant."""
    from . import production_test_composite as composite
    from . import production_test_node as node
    from .production_pytest_recipe import recipe_checks

    variant = isinstance(profile, dict) and (
        node.is_node(profile) or composite.is_composite(profile)
    )
    if (
        not isinstance(profile, dict)
        or set(profile)
        != {"schema", "card", "repository", "criteria_sha256", "recipe"}
        | ({"node_environment"} if variant else set())
        or profile["schema"] not in {PYTHON_RECIPE_SCHEMA, node.SCHEMA, composite.SCHEMA}
        or profile["card"] != binding["source_card"]
        or profile["criteria_sha256"] != binding["criteria_sha256"]
        or not isinstance(profile["repository"], str)
        or not isinstance(profile["recipe"], dict)
    ):
        raise TestEvidenceError("declared test recipe does not match the card")
    recipe_checks(profile["recipe"])
    if bool({"vitest", "node_test"} & profile["recipe"].keys()) != variant:
        raise TestEvidenceError("test recipe and recipe variant disagree")
    if ("vitest" in profile["recipe"] and len(profile["recipe"]) > 1) != composite.is_composite(
        profile
    ):
        raise TestEvidenceError("test phases and recipe variant disagree")


def _validate_plan(plan: dict, binding: dict, *, allow_remote_host: bool = False) -> None:
    """Fail closed unless the record is exactly the declared checks of this binding."""
    required = {
        "schema",
        "binding",
        "checks",
        "qualified_by",
        "qualification_sha256",
        "python_sha256",
        "toolchain_sha256",
        "runtime_sha256",
        "policy_sha256",
        "host",
    }
    if not isinstance(plan, dict):
        raise TestEvidenceError("operator test plan is invalid or stale")
    expected_checks = approved_checks()
    if "profile" in plan:
        from . import production_test_composite as composite
        from . import production_test_node as node
        from .production_pytest_recipe import recipe_checks

        required.add("profile")
        _validate_declared(plan["profile"], binding)
        expected_checks = recipe_checks(plan["profile"]["recipe"])
        if node.is_node(plan["profile"]) or composite.is_composite(plan["profile"]):
            node.validate_environment(plan["profile"]["node_environment"])
    if (
        set(plan) != required
        or plan["schema"] != PLAN_SCHEMA
        or plan["binding"] != binding
        or plan["checks"] != expected_checks
        or not isinstance(plan["qualified_by"], str)
        or not plan["qualified_by"]
        or any(
            not re.fullmatch(r"[0-9a-f]{64}", str(plan[k]))
            for k in (
                "qualification_sha256",
                "python_sha256",
                "toolchain_sha256",
                "runtime_sha256",
                "policy_sha256",
            )
        )
        or not isinstance(plan["host"], str)
        or (plan["host"] != socket.gethostname().split(".")[0].lower() and not allow_remote_host)
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
