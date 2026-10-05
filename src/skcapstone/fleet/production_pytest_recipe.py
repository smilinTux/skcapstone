"""Bounded Python recipes, sealed-source paths and exact collection evidence."""

import re
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

from . import production_test_node as node
from . import production_test_plan as plan

BASE_FIELDS = {"pytest", "compile", "lint", "changelog"}
MAX_TARGETS = 4096
_PATH = re.compile(
    r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*(?:/[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)*\Z"
)


def safe_target(path: str, *, pytest: bool = False) -> str:
    """Admit literal relative components, never flags, globs or traversal."""
    if (
        not isinstance(path, str)
        or len(path) > 240
        or path.startswith("-")
        or not _PATH.fullmatch(path)
    ):
        raise plan.TestEvidenceError("unsafe test target")
    parts = PurePosixPath(path).parts
    if pytest:
        if "tests" not in parts:
            raise plan.TestEvidenceError("pytest requires explicit test files or test directories")
    elif parts[0] not in ("tests", "src", "scripts") or not path.endswith(".py"):
        raise plan.TestEvidenceError("unsafe test target")
    return path


def covered(path: str, targets: dict[str, int]) -> str:
    """Return the one explicit file/directory covering a collected source file."""
    matches = [
        t for t in targets if path == t or (not t.endswith(".py") and path.startswith(t + "/"))
    ]
    if len(matches) != 1:
        raise plan.TestEvidenceError("test file outside or overlapping approved targets")
    return matches[0]


def safe_node(node_id: str, targets: dict[str, int]) -> str:
    """Validate an exact pytest ID bound to a selected test source file."""
    if (
        not isinstance(node_id, str)
        or len(node_id) > 16384
        or any(c in node_id for c in "\x00\r\n")
    ):
        raise plan.TestEvidenceError("invalid baseline node ID")
    path, sep, suffix = node_id.partition("::")
    if not sep or not suffix or not path.endswith(".py"):
        raise plan.TestEvidenceError("invalid baseline node ID")
    safe_target(path, pytest=True)
    covered(path, targets)
    return path


def requires_selection(recipe: dict) -> bool:
    """New directory or baseline contracts require trusted exact collection evidence."""
    return bool(recipe.get("deselect")) or any(
        not p.endswith(".py") for p in recipe.get("pytest", {})
    )


def recipe_checks(recipe: dict) -> list[dict]:
    """Compile operator-approved bounded inputs to fixed command argv."""
    if isinstance(recipe, dict) and "vitest" in recipe:
        return node.checks(recipe)
    if (
        not isinstance(recipe, dict)
        or not BASE_FIELDS <= recipe.keys()
        or recipe.keys() - BASE_FIELDS - {"file_cap", "deselect"}
    ):
        raise plan.TestEvidenceError("unsupported test recipe")
    cap = recipe.get("file_cap", 64)
    if type(cap) is not int or not 1 <= cap <= MAX_TARGETS:
        raise plan.TestEvidenceError("invalid test file cap")
    tests = recipe["pytest"]
    if (
        not isinstance(tests, dict)
        or not 1 <= len(tests) <= cap
        or any(type(n) is not int or not 1 <= n <= 100000 for n in tests.values())
    ):
        raise plan.TestEvidenceError("required per-file test coverage is missing")
    for path in tests:
        safe_target(path, pytest=True)
        covered(path, tests)
    for category in ("compile", "lint"):
        paths = recipe[category]
        if (
            not isinstance(paths, list)
            or len(paths) > 64
            or any(not isinstance(p, str) for p in paths)
            or len(paths) != len(set(paths))
        ):
            raise plan.TestEvidenceError("invalid approved check targets")
        for path in paths:
            safe_target(path)
    if type(recipe["changelog"]) is not bool:
        raise plan.TestEvidenceError("invalid changelog selection")
    baseline = recipe.get("deselect", [])
    if (
        not isinstance(baseline, list)
        or len(baseline) > MAX_TARGETS
        or any(not isinstance(n, str) for n in baseline)
        or len(baseline) != len(set(baseline))
    ):
        raise plan.TestEvidenceError("invalid baseline node IDs")
    for node_id in baseline:
        safe_node(node_id, tests)
    python = str(plan.PREFIX / "bin/python")
    argv = [
        python,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-p",
        "production_pytest_selection",
        "-m",
        "not host_systemd",
        "--junitxml=/output/pytest.xml",
    ]
    if requires_selection(recipe):
        argv += ["--rootdir=/work", "--confcutdir=/work"]
        argv += ["--skfleet-baseline-node=" + n for n in baseline]
    checks = [{"id": "pytest", "argv": [*argv, *tests]}]
    if recipe["compile"]:
        checks.append({"id": "compile", "argv": [python, "-m", "py_compile", *recipe["compile"]]})
    if recipe["lint"]:
        checks.append({"id": "lint", "argv": [python, "-m", "ruff", "check", *recipe["lint"]]})
    if recipe["changelog"]:
        checks.append(
            {"id": "changelog", "argv": [python, "scripts/changelog_fragments.py", "--check"]}
        )
    return checks


def validate_source(recipe: dict, workspace: Path) -> None:
    """Require every target and baseline file to be real within sealed source."""
    recipe_checks(recipe)
    workspace = workspace.resolve(strict=True)
    paths = [*recipe["pytest"], *recipe["compile"], *recipe["lint"]]
    paths += [safe_node(n, recipe["pytest"]) for n in recipe.get("deselect", [])]
    if recipe["changelog"]:
        paths.append("scripts/changelog_fragments.py")
    for path in paths:
        target = workspace / path
        try:
            if target.resolve(strict=True) != target or not (
                target.is_file() if path.endswith(".py") else target.is_dir()
            ):
                raise plan.TestEvidenceError("test target is not real sealed source")
            if target.is_dir():
                for count, child in enumerate(target.rglob("*"), 1):
                    if count > 100000 or child.is_symlink():
                        raise plan.TestEvidenceError(
                            "test directory exceeds bound or redirects sealed source"
                        )
        except OSError as exc:
            raise plan.TestEvidenceError("test target missing from sealed source") from exc


def selected_junit_counts(raw: bytes, profile: dict, selection: dict | None) -> dict:
    """Bind successful JUnit identities to exact selected IDs and excluded baseline."""
    from _pytest.junitxml import mangle_test_address

    recipe = profile["recipe"]
    recipe_checks(recipe)
    baseline = sorted(recipe.get("deselect", []))
    if (
        not isinstance(selection, dict)
        or set(selection) != {"schema", "baseline", "deselected", "selected"}
        or selection["schema"] != "skfleet.pytest-selection/v1"
        or selection["baseline"] != baseline
        or selection["deselected"] != baseline
    ):
        raise plan.TestEvidenceError("exact baseline collection evidence is missing or changed")
    selected = selection["selected"]
    if (
        not isinstance(selected, list)
        or not 1 <= len(selected) <= 100000
        or any(not isinstance(n, str) for n in selected)
        or selected != sorted(set(selected))
        or set(selected).intersection(baseline)
    ):
        raise plan.TestEvidenceError("invalid selected test identities")
    identities, counts = set(), dict.fromkeys(recipe["pytest"], 0)
    for node_id in selected:
        path = safe_node(node_id, recipe["pytest"])
        counts[covered(path, recipe["pytest"])] += 1
        address = mangle_test_address(node_id)
        identity = (".".join(address[:-1]), address[-1])
        if identity in identities:
            raise plan.TestEvidenceError("duplicate selected test identity")
        identities.add(identity)
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise plan.TestEvidenceError("JUnit entities are forbidden")
    root = ElementTree.fromstring(raw)
    cases, actual = list(root.iter("testcase")), set()
    for case in cases:
        if any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
            raise plan.TestEvidenceError("required tests failed, errored or skipped")
        identity = (case.get("classname", ""), case.get("name"))
        if identity in actual or identity not in identities:
            raise plan.TestEvidenceError("unexpected or duplicate JUnit test identity")
        actual.add(identity)
    suites = list(root.iter("testsuite"))
    if (
        actual != identities
        or not suites
        or any(int(s.get(k, "-1")) != 0 for s in suites for k in ("failures", "errors", "skipped"))
        or sum(int(s.get("tests", "-1")) for s in suites) != len(cases)
        or any(counts[t] < n for t, n in recipe["pytest"].items())
    ):
        raise plan.TestEvidenceError("JUnit coverage does not meet the qualified plan")
    return {
        "total": len(cases),
        "per_file": counts,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "deselected": baseline,
    }
