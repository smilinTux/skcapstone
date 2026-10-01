"""Operator-qualified Python test contracts, never commands supplied by a model."""

from __future__ import annotations

import re
import socket
from pathlib import Path

from . import production_test_plan as plan
from .production_builder import digest

SCHEMA = "skfleet.qualified-test-profile/v1"
FIELDS = {
    "schema", "card", "repository", "criteria_sha256", "recipe", "qualified_by",
    "qualification_sha256", "python_sha256", "runtime_sha256", "policy_sha256", "host",
}


def contract(core: dict) -> dict:
    """Bind a specific nonempty native contract, not a repository-wide permission."""
    card = core.get("id")
    criteria = core.get("acceptance_criteria")
    sections = (core.get("links") or {}, core.get("meta") or {})
    if any(not isinstance(section, dict) for section in sections):
        raise plan.TestEvidenceError("test repository binding is invalid")
    values = [
        section.get("repository")
        for section in sections
        if section.get("repository")
    ]
    if any(not isinstance(value, str) for value in values):
        raise plan.TestEvidenceError("test repository binding is invalid")
    repositories = set(values)
    if (not isinstance(card, str) or not re.fullmatch(r"[0-9a-f]{8}", card)
            or not isinstance(criteria, list) or not criteria
            or any(not isinstance(c, str) or not c.strip() for c in criteria)
            or len(repositories) != 1):
        raise plan.TestEvidenceError("test contract lacks exact card/repository/criteria")
    repository = repositories.pop()
    if (not isinstance(repository, str) or not repository.startswith("https://")
            or "@" in repository):
        raise plan.TestEvidenceError("test repository must be credential-free HTTPS")
    return {"card": card, "repository": repository, "criteria_sha256": digest(criteria)}


def recipe_checks(recipe: dict) -> list[dict]:
    """Compile bounded file lists into fixed argv templates for the existing sandbox."""
    if not isinstance(recipe, dict) or set(recipe) != {"pytest", "compile", "lint", "changelog"}:
        raise plan.TestEvidenceError("unsupported test recipe")
    tests = recipe["pytest"]
    if (not isinstance(tests, dict) or not 1 <= len(tests) <= 64
            or any(type(n) is not int or not 1 <= n <= 100000 for n in tests.values())):
        raise plan.TestEvidenceError("required per-file test coverage is missing")
    for category in ("compile", "lint"):
        paths = recipe[category]
        if (not isinstance(paths, list) or len(paths) > 64
                or any(not isinstance(path, str) for path in paths)
                or len(paths) != len(set(paths))):
            raise plan.TestEvidenceError("invalid approved check targets")
    if type(recipe["changelog"]) is not bool:
        raise plan.TestEvidenceError("invalid changelog selection")
    for path in [*tests, *recipe["compile"], *recipe["lint"]]:
        if (not isinstance(path, str) or len(path) > 240
                or not re.fullmatch(r"(?:tests|src|scripts)/[A-Za-z0-9_/]+\.py", path)
                or "//" in path):
            raise plan.TestEvidenceError("unsafe test target")
    if any(not p.startswith("tests/") for p in tests):
        raise plan.TestEvidenceError("pytest requires explicit test files")
    python = str(plan.PREFIX / "bin/python")
    checks = [{"id": "pytest", "argv": [python, "-m", "pytest", "-q", "-p",
               "no:cacheprovider", "--junitxml=/output/pytest.xml", *tests]}]
    if recipe["compile"]:
        checks.append({"id": "compile", "argv": [python, "-m", "py_compile",
                                                  *recipe["compile"]]})
    if recipe["lint"]:
        checks.append({"id": "lint", "argv": [python, "-m", "ruff", "check", *recipe["lint"]]})
    if recipe["changelog"]:
        checks.append({"id": "changelog", "argv": [python, "scripts/changelog_fragments.py",
                                                     "--check"]})
    return checks


def validate_profile(value: dict, expected: dict, policy: dict, *, environment=True) -> dict:
    """Validate authority qualification or its existing trusted dispatch copy."""
    if (not isinstance(value, dict) or set(value) != FIELDS or value["schema"] != SCHEMA
            or any(value.get(k) != v for k, v in expected.items())
            or not isinstance(value["qualified_by"], str) or not value["qualified_by"]
            or any(not isinstance(value[k], str) or not re.fullmatch(r"[0-9a-f]{64}", value[k])
                   for k in ("qualification_sha256", "python_sha256", "runtime_sha256",
                             "criteria_sha256", "policy_sha256"))
            or value["policy_sha256"] != digest(policy)
            or value["host"] != policy["authority_host"]):
        raise plan.TestEvidenceError("qualified test profile is missing, stale or conflicting")
    recipe_checks(value["recipe"])
    if environment and (
        value["host"] != socket.gethostname().split(".")[0].lower()
        or value["runtime_sha256"] != plan.runtime_fingerprint()
        or value["python_sha256"] != plan.sha((plan.PREFIX / "bin/python").read_bytes())
    ):
        raise plan.TestEvidenceError("qualified test execution environment changed")
    return value


def qualify_profile(home: Path, core: dict, policy: dict, recipe: dict,
                    qualified_by: str, qualification_sha256: str) -> Path:
    """Operator API after actual recipe qualification; no worker calls this API."""
    value = {"schema": SCHEMA, **contract(core), "recipe": recipe,
             "qualified_by": qualified_by, "qualification_sha256": qualification_sha256,
             "python_sha256": plan.sha((plan.PREFIX / "bin/python").read_bytes()),
             "runtime_sha256": plan.runtime_fingerprint(), "policy_sha256": digest(policy),
             "host": socket.gethostname().split(".")[0].lower()}
    validate_profile(value, contract(core), policy)
    directory = home / "fleet/test-profiles"
    plan.private_dir(directory, create=True)
    path = directory / (value["card"] + ".json")
    plan.write_once(path, value)
    return path


def preflight(home: Path, core: dict, labels, policy: dict) -> dict | None:
    """Withhold only new source-only producers without a supported trusted contract."""
    labels = {str(label).lower() for label in labels}
    if "source-only" not in labels or "review" in labels or "seat-seraph" in labels:
        return None
    expected = contract(core)
    try:
        value = plan.read_json(home / "fleet/test-profiles" / (expected["card"] + ".json"))
    except FileNotFoundError:
        raise plan.TestEvidenceError("required-test-profile-unqualified") from None
    return validate_profile(value, expected, policy)


def seal_candidate(home: Path, binding: dict, workspace: Path, policy: dict,
                   repository: str) -> None:
    """Seal once only after acceptance has independently validated current custody."""
    try:
        plan.load_plan(home, binding)
        return
    except FileNotFoundError:
        pass
    expected = {"card": binding["source_card"], "repository": repository,
                "criteria_sha256": binding["criteria_sha256"]}
    value = plan.read_json(home / "fleet/test-profiles" / (expected["card"] + ".json"))
    validate_profile(value, expected, policy)
    plan.seal_plan(home, binding, workspace, policy, value["qualified_by"],
                   value["qualification_sha256"], profile=value)
