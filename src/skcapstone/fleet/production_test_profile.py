"""Operator-qualified Python test contracts, never commands supplied by a model."""

from __future__ import annotations

import json
import re
import socket
from itertools import islice
from pathlib import Path

from skcoord.card_store import CardStore, card_mutation_lock

from . import production_test_node as node
from . import production_test_plan as plan
from .production_builder import digest

SCHEMA = "skfleet.qualified-test-profile/v1"
FIELDS = {
    "schema",
    "card",
    "repository",
    "criteria_sha256",
    "recipe",
    "qualified_by",
    "qualification_sha256",
    "python_sha256",
    "runtime_sha256",
    "policy_sha256",
    "host",
}


def contract(core: dict) -> dict:
    """Bind a specific nonempty native contract, not a repository-wide permission."""
    card = core.get("id")
    criteria = core.get("acceptance_criteria")
    sections = (core.get("links") or {}, core.get("meta") or {})
    if any(not isinstance(section, dict) for section in sections):
        raise plan.TestEvidenceError("test repository binding is invalid")
    values = [section.get("repository") for section in sections if section.get("repository")]
    if any(not isinstance(value, str) for value in values):
        raise plan.TestEvidenceError("test repository binding is invalid")
    repositories = set(values)
    if (
        not isinstance(card, str)
        or not re.fullmatch(r"[0-9a-f]{8}", card)
        or not isinstance(criteria, list)
        or not criteria
        or any(not isinstance(c, str) or not c.strip() for c in criteria)
        or len(repositories) != 1
    ):
        raise plan.TestEvidenceError("test contract lacks exact card/repository/criteria")
    repository = repositories.pop()
    if (
        not isinstance(repository, str)
        or not repository.startswith("https://")
        or "@" in repository
    ):
        raise plan.TestEvidenceError("test repository must be credential-free HTTPS")
    return {"card": card, "repository": repository, "criteria_sha256": digest(criteria)}


def recipe_checks(recipe: dict) -> list[dict]:
    """Compile bounded file lists into fixed argv templates for the existing sandbox."""
    if isinstance(recipe, dict) and "vitest" in recipe:
        return node.checks(recipe)
    if not isinstance(recipe, dict) or set(recipe) != {"pytest", "compile", "lint", "changelog"}:
        raise plan.TestEvidenceError("unsupported test recipe")
    tests = recipe["pytest"]
    if (
        not isinstance(tests, dict)
        or not 1 <= len(tests) <= 64
        or any(type(n) is not int or not 1 <= n <= 100000 for n in tests.values())
    ):
        raise plan.TestEvidenceError("required per-file test coverage is missing")
    for category in ("compile", "lint"):
        paths = recipe[category]
        if (
            not isinstance(paths, list)
            or len(paths) > 64
            or any(not isinstance(path, str) for path in paths)
            or len(paths) != len(set(paths))
        ):
            raise plan.TestEvidenceError("invalid approved check targets")
    if type(recipe["changelog"]) is not bool:
        raise plan.TestEvidenceError("invalid changelog selection")
    for path in [*tests, *recipe["compile"], *recipe["lint"]]:
        if (
            not isinstance(path, str)
            or len(path) > 240
            or not re.fullmatch(r"(?:tests|src|scripts)/[A-Za-z0-9_/-]+\.py", path)
            or "//" in path
        ):
            raise plan.TestEvidenceError("unsafe test target")
    if any(not p.startswith("tests/") for p in tests):
        raise plan.TestEvidenceError("pytest requires explicit test files")
    python = str(plan.PREFIX / "bin/python")
    checks = [
        {
            "id": "pytest",
            "argv": [
                python,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "-m",
                "not host_systemd",
                "--junitxml=/output/pytest.xml",
                *tests,
            ],
        }
    ]
    if recipe["compile"]:
        checks.append({"id": "compile", "argv": [python, "-m", "py_compile", *recipe["compile"]]})
    if recipe["lint"]:
        checks.append({"id": "lint", "argv": [python, "-m", "ruff", "check", *recipe["lint"]]})
    if recipe["changelog"]:
        checks.append(
            {"id": "changelog", "argv": [python, "scripts/changelog_fragments.py", "--check"]}
        )
    return checks


def _validate_shape(value: dict) -> None:
    """Check each historical generation without requalifying its old environment."""
    node_profile = node.is_node(value)
    fields = FIELDS | {"node_environment"} if node_profile else FIELDS
    schema = node.SCHEMA if node_profile else SCHEMA
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value["schema"] != schema
        or not isinstance(value["qualified_by"], str)
        or not value["qualified_by"]
        or any(
            not isinstance(value[k], str) or not re.fullmatch(r"[0-9a-f]{64}", value[k])
            for k in (
                "qualification_sha256",
                "python_sha256",
                "runtime_sha256",
                "criteria_sha256",
                "policy_sha256",
            )
        )
    ):
        raise plan.TestEvidenceError("qualified test profile is missing, stale or conflicting")
    recipe_checks(value["recipe"])
    if ("vitest" in value["recipe"]) != node_profile:
        raise plan.TestEvidenceError("test recipe and profile variant disagree")


def validate_profile(value: dict, expected: dict, policy: dict, *, environment=True) -> dict:
    """Validate authority qualification or its existing trusted dispatch copy."""
    _validate_shape(value)
    if (
        any(value.get(k) != v for k, v in expected.items())
        or value["policy_sha256"] != digest(policy)
        or value["host"] != policy["authority_host"]
    ):
        raise plan.TestEvidenceError("qualified test profile is missing, stale or conflicting")
    node_profile = node.is_node(value)
    if node_profile and environment:
        node.validate_environment(value["node_environment"])
    if environment and (
        value["host"] != socket.gethostname().split(".")[0].lower()
        or value["runtime_sha256"] != plan.runtime_fingerprint()
        or value["python_sha256"] != plan.sha((plan.PREFIX / "bin/python").read_bytes())
    ):
        raise plan.TestEvidenceError("qualified test execution environment changed")
    return value


def qualify_profile(
    home: Path,
    core: dict,
    policy: dict,
    recipe: dict,
    qualified_by: str,
    qualification_sha256: str,
    *,
    node_environment: dict | None = None,
) -> Path:
    """Operator API after actual recipe qualification; no worker calls this API."""
    value = {
        "schema": SCHEMA,
        **contract(core),
        "recipe": recipe,
        "qualified_by": qualified_by,
        "qualification_sha256": qualification_sha256,
        "python_sha256": plan.sha((plan.PREFIX / "bin/python").read_bytes()),
        "runtime_sha256": plan.runtime_fingerprint(),
        "policy_sha256": digest(policy),
        "host": socket.gethostname().split(".")[0].lower(),
    }
    if node_environment is not None:
        value.update(schema=node.SCHEMA, node_environment=node_environment)
    validate_profile(value, contract(core), policy)
    directory = home / "fleet/test-profiles"
    plan.private_dir(directory, create=True)
    path = directory / (value["card"] + ".json")
    plan.write_once(path, value)
    return path


def read_profile(home: Path, card: str, *, pinned: dict | None = None) -> tuple[dict, str]:
    """Resolve an append-only chain, or an exact historical sealed-plan profile."""
    if not isinstance(card, str) or not re.fullmatch(r"[0-9a-f]{8}", card):
        raise plan.TestEvidenceError("invalid profile card")
    root = home / "fleet/test-profiles"
    raw = plan.read_private(root / (card + ".json"))
    value = json.loads(raw, object_pairs_hook=plan._unique_object)
    found = None
    seen = set()
    for _ in range(128):
        fingerprint = plan.sha(raw)
        if fingerprint in seen or not isinstance(value, dict) or value.get("card") != card:
            raise plan.TestEvidenceError("invalid test profile chain")
        _validate_shape(value)
        seen.add(fingerprint)
        if pinned is not None and value == pinned:
            found = (value, fingerprint)
        directory = root / card
        if directory.exists() or directory.is_symlink():
            plan.private_dir(directory)
        path = directory / (fingerprint + ".json")
        try:
            raw = plan.read_private(path)
        except FileNotFoundError:
            if directory.exists() and len(list(islice(directory.iterdir(), 129))) != len(seen) - 1:
                raise plan.TestEvidenceError("ambiguous or disconnected test profile chain")
            if pinned is None:
                return value, fingerprint
            if found is not None:
                return found
            raise plan.TestEvidenceError("candidate test profile changed") from None
        envelope = json.loads(raw, object_pairs_hook=plan._unique_object)
        if not isinstance(envelope, dict) or envelope.get("predecessor_sha256") != fingerprint:
            raise plan.TestEvidenceError("invalid test profile successor")
        claimed = (
            set(envelope) == {"schema", "predecessor_sha256", "source_claim", "profile"}
            and envelope["schema"] == "skfleet.test-profile-successor/v1"
            and isinstance(envelope["source_claim"], dict)
            and set(envelope["source_claim"]) == {"owner", "claim_revision"}
            and all(isinstance(v, str) and v for v in envelope["source_claim"].values())
        )
        unclaimed = (
            set(envelope) == {"schema", "predecessor_sha256", "source_card_sha256", "profile"}
            and envelope["schema"] == "skfleet.test-profile-successor/v2"
            and isinstance(envelope["source_card_sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", envelope["source_card_sha256"])
        )
        if not (claimed or unclaimed):
            raise plan.TestEvidenceError("invalid test profile successor")
        value = envelope["profile"]
    raise plan.TestEvidenceError("test profile chain exceeds generation bound")


def supersede_profile(
    home: Path,
    core: dict,
    policy: dict,
    recipe: dict,
    qualified_by: str,
    qualification_sha256: str,
    *,
    predecessor_sha256: str,
    runtime_sha256: str,
    source_claim: dict | None = None,
    unclaimed: bool = False,
    node_environment: dict | None = None,
) -> Path:
    """Append qualified evidence under exact claimed or unclaimed source custody."""
    expected = contract(core)
    if (
        unclaimed == (source_claim is not None)
        or not isinstance(predecessor_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", predecessor_sha256)
    ):
        raise plan.TestEvidenceError("invalid profile successor binding")
    if source_claim is not None and (
        not isinstance(source_claim, dict)
        or set(source_claim) != {"owner", "claim_revision"}
        or any(not isinstance(v, str) or not v for v in source_claim.values())
    ):
        raise plan.TestEvidenceError("invalid profile successor binding")
    with card_mutation_lock(home, expected["card"]):
        card = CardStore(home).fold(expected["card"])
        if (
            card is None
            or card.archived
            or card.meta.get("claim_conflicts")
            or contract(card.model_dump(mode="json")) != expected
        ):
            raise plan.TestEvidenceError("profile source claim changed")
        if unclaimed:
            if card.status.value not in {"backlog", "ready"} or card.owner is not None:
                raise plan.TestEvidenceError("profile source claim changed")
            source_card_sha256 = plan.sha(
                json.dumps(
                    card.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
                ).encode()
            )
        elif (
            card.status.value != "doing"
            or card.owner != source_claim["owner"]
            or card.meta.get("_claim_revision") != source_claim["claim_revision"]
        ):
            raise plan.TestEvidenceError("profile source claim changed")
        predecessor, current = read_profile(home, expected["card"])
        if current != predecessor_sha256:
            raise plan.TestEvidenceError("profile predecessor changed")
        if qualification_sha256 == predecessor.get("qualification_sha256"):
            raise plan.TestEvidenceError("profile successor requires fresh qualification evidence")
        value = {
            "schema": SCHEMA,
            **expected,
            "recipe": recipe,
            "qualified_by": qualified_by,
            "qualification_sha256": qualification_sha256,
            "python_sha256": plan.sha((plan.PREFIX / "bin/python").read_bytes()),
            "runtime_sha256": runtime_sha256,
            "policy_sha256": digest(policy),
            "host": socket.gethostname().split(".")[0].lower(),
        }
        if node_environment is not None:
            value.update(schema=node.SCHEMA, node_environment=node_environment)
        validate_profile(value, expected, policy)
        directory = home / "fleet/test-profiles" / expected["card"]
        plan.private_dir(directory, create=True)
        if len(list(islice(directory.iterdir(), 128))) >= 127:
            raise plan.TestEvidenceError("test profile chain exceeds generation bound")
        path = directory / (current + ".json")
        envelope = {"predecessor_sha256": current, "profile": value}
        if unclaimed:
            envelope.update(
                schema="skfleet.test-profile-successor/v2", source_card_sha256=source_card_sha256
            )
        else:
            envelope.update(schema="skfleet.test-profile-successor/v1", source_claim=source_claim)
        plan.write_once(path, envelope)
        return path


def preflight(home: Path, core: dict, labels, policy: dict) -> dict | None:
    """Withhold only new source-only producers without a supported trusted contract."""
    labels = {str(label).lower() for label in labels}
    if "source-only" not in labels or "review" in labels or "seat-seraph" in labels:
        return None
    expected = contract(core)
    try:
        value, _ = read_profile(home, expected["card"])
    except FileNotFoundError:
        raise plan.TestEvidenceError("required-test-profile-unqualified") from None
    return validate_profile(value, expected, policy)


def seal_candidate(
    home: Path, binding: dict, workspace: Path, policy: dict, repository: str
) -> None:
    """Seal once only after acceptance has independently validated current custody."""
    predecessor_sha256 = None
    try:
        existing, _, predecessor_sha256 = plan.load_plan(home, binding)
        if existing["policy_sha256"] == digest(policy):
            return
    except FileNotFoundError:
        pass
    except plan.TestEvidenceError as exc:
        if str(exc) != "operator test plan is invalid or stale":
            raise
        _, _, predecessor_sha256 = plan.load_plan(home, binding, require_current=False)
    expected = {
        "card": binding["source_card"],
        "repository": repository,
        "criteria_sha256": binding["criteria_sha256"],
    }
    value, _ = read_profile(home, expected["card"])
    validate_profile(value, expected, policy)
    plan.seal_plan(
        home,
        binding,
        workspace,
        policy,
        value["qualified_by"],
        value["qualification_sha256"],
        profile=value,
        predecessor_sha256=predecessor_sha256,
    )
