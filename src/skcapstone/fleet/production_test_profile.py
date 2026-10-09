"""Operator-qualified Python test contracts, never commands supplied by a model."""

from __future__ import annotations

import json
import re
import socket
from itertools import islice
from pathlib import Path

from skcoord.card_store import CardStore, card_mutation_lock

from . import production_test_composite as composite
from . import production_test_node as node
from . import production_test_plan as plan
from .production_builder import digest
from .production_pytest_recipe import recipe_checks

SCHEMA_V1 = "skfleet.qualified-test-profile/v1"
SCHEMA_V2 = "skfleet.qualified-test-profile/v2"
SCHEMA = "skfleet.qualified-test-profile/v3"
FIELDS = {
    "schema",
    "card",
    "repository",
    "criteria_sha256",
    "recipe",
    "qualified_by",
    "qualification_sha256",
    "python_sha256",
    "toolchain_sha256",
    "source_sha256",
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


def initial_recipe(core: dict, workspace: Path) -> tuple[dict, dict | None]:
    """Choose a fixed test recipe from the card scope or operator selection."""
    text = "\n".join(
        [
            str(core.get("title") or ""),
            str(core.get("description") or ""),
            *(core.get("acceptance_criteria") or []),
        ]
    ).lower()
    node_scope = (
        "apps/web/" in text
        or "vitest" in text
        or ".tsx" in text
        or ".ts" in text
        or "frontend" in text
    )
    python_scope = ".py" in text or "pytest" in text or "python" in text
    repository = (core.get("links") or {}).get("repository") or (core.get("meta") or {}).get(
        "repository", ""
    )
    python_project = (workspace / "pyproject.toml").is_file() and (workspace / "tests").is_dir()
    node_project = (workspace / "apps/web/package.json").is_file()

    def node_recipe():
        from . import production_test_node as node

        web = workspace / "apps/web"
        test_files = [
            path
            for path in web.rglob("*")
            if path.is_file() and (".test." in path.name or ".spec." in path.name)
        ]
        tests = sorted(path.relative_to(web).as_posix() for path in test_files)
        if (
            not tests
            or len(tests) > 256
            or any(
                not path.startswith("src/")
                or ".test.ts" not in path
                or (web / path).is_symlink()
                or not (web / path).resolve().is_relative_to(web.resolve())
                for path in tests
            )
        ):
            raise plan.TestEvidenceError("Node source has no bounded Vitest suite")
        recipe = {"vitest": {path: 1 for path in tests}}
        return recipe, node.qualified_environment(workspace)

    configured = (core.get("links") or {}).get("test_profile_recipe")
    if configured is not None:
        if not isinstance(configured, str) or len(configured) > 16384:
            raise plan.TestEvidenceError("operator test recipe is invalid")
        try:
            recipe = json.loads(configured, object_pairs_hook=plan._unique_object)
            recipe_checks(recipe)
            environment = None
            if "vitest" in recipe:
                from . import production_test_node as node

                environment = node.qualified_environment(workspace)
                for path in recipe["vitest"]:
                    target = workspace / "apps/web" / path
                    if target.is_symlink() or not target.is_file():
                        raise plan.TestEvidenceError("Node test target is missing or redirected")
                if len(recipe) > 1:
                    composite.validate_source(
                        {
                            "schema": composite.SCHEMA,
                            "recipe": recipe,
                            "node_environment": environment,
                        },
                        workspace,
                    )
            else:
                from .production_pytest_recipe import validate_source

                validate_source(recipe, workspace)
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise plan.TestEvidenceError(
                "operator test recipe is invalid: " + str(exc)[:120]
            ) from exc
        return recipe, environment

    if node_scope and python_scope:
        if not (python_project and node_project):
            raise plan.TestEvidenceError("mixed test scope needs Python and frontend projects")
        recipe, environment = node_recipe()
        return {"pytest_all": True, **recipe}, environment
    if node_scope and node_project:
        return node_recipe()
    if python_project and (
        python_scope
        or not node_project
        or repository.rstrip("/").removesuffix(".git").endswith("/skcapstone")
    ):
        return {"pytest_all": True}, None
    raise plan.TestEvidenceError("card scope has no supported initial test recipe")


def profile_value(
    core: dict,
    policy: dict,
    recipe: dict,
    qualified_by: str,
    qualification_sha256: str,
    *,
    source_sha256: str | None = None,
    node_environment: dict | None = None,
) -> dict:
    """Build and validate a profile value without publishing it."""
    value = {
        "schema": SCHEMA if source_sha256 is not None else SCHEMA_V2,
        **contract(core),
        "recipe": recipe,
        "qualified_by": qualified_by,
        "qualification_sha256": qualification_sha256,
        "python_sha256": plan.sha((plan.PREFIX / "bin/python").read_bytes()),
        "toolchain_sha256": plan.toolchain_fingerprint(),
        "runtime_sha256": plan.runtime_fingerprint(),
        "policy_sha256": plan.execution_policy_fingerprint(policy),
        "host": socket.gethostname().split(".")[0].lower(),
    }
    if source_sha256 is not None:
        value["source_sha256"] = source_sha256
    if node_environment is not None:
        value["schema"] = (
            (composite.SCHEMA if source_sha256 is not None else composite.SCHEMA_V2)
            if set(recipe) - {"vitest"}
            else (node.SCHEMA if source_sha256 is not None else node.SCHEMA_V2)
        )
        value.update(
            node_environment=node_environment,
        )
    validate_profile(value, contract(core), policy)
    return value


def _validate_shape(value: dict) -> None:
    """Check each historical generation without requalifying its old environment."""
    node_profile = node.is_node(value) or composite.is_composite(value)
    fields = FIELDS | {"node_environment"} if node_profile else FIELDS
    schema = value.get("schema") if isinstance(value, dict) else None
    legacy_v1 = schema in {SCHEMA_V1, node.SCHEMA_V1, composite.SCHEMA_V1}
    legacy_v2 = schema in {SCHEMA_V2, node.SCHEMA_V2, composite.SCHEMA_V2}
    if legacy_v1:
        fields = fields - {"toolchain_sha256"}
    if legacy_v1 or legacy_v2:
        fields = fields - {"source_sha256"}
    expected_schemas = {SCHEMA, node.SCHEMA, composite.SCHEMA}
    if legacy_v1:
        expected_schemas |= {SCHEMA_V1, node.SCHEMA_V1, composite.SCHEMA_V1}
    if legacy_v2:
        expected_schemas |= {SCHEMA_V2, node.SCHEMA_V2, composite.SCHEMA_V2}
    hash_fields = (
        (
            "qualification_sha256",
            "python_sha256",
            "runtime_sha256",
            "criteria_sha256",
            "policy_sha256",
        )
        + (() if legacy_v1 else ("toolchain_sha256",))
        + (() if legacy_v1 or legacy_v2 else ("source_sha256",))
    )
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value["schema"] not in expected_schemas
        or not isinstance(value["qualified_by"], str)
        or not value["qualified_by"]
        or any(
            not isinstance(value[k], str) or not re.fullmatch(r"[0-9a-f]{64}", value[k])
            for k in hash_fields
        )
    ):
        raise plan.TestEvidenceError("qualified test profile is missing, stale or conflicting")
    recipe_checks(value["recipe"])
    if ("vitest" in value["recipe"]) != node_profile:
        raise plan.TestEvidenceError("test recipe and profile variant disagree")
    if ("vitest" in value["recipe"] and len(value["recipe"]) > 1) != composite.is_composite(value):
        raise plan.TestEvidenceError("test phases and profile variant disagree")


def validate_profile(value: dict, expected: dict, policy: dict, *, environment=True) -> dict:
    """Validate authority qualification or its existing trusted dispatch copy."""
    _validate_shape(value)
    if (
        any(value.get(k) != v for k, v in expected.items())
        or value["policy_sha256"] != plan.execution_policy_fingerprint(policy)
        or value["host"] != policy["authority_host"]
        or value["schema"] in {SCHEMA_V1, node.SCHEMA_V1, composite.SCHEMA_V1}
    ):
        raise plan.TestEvidenceError("qualified test profile is missing, stale or conflicting")
    node_profile = node.is_node(value) or composite.is_composite(value)
    if node_profile and environment:
        node.validate_environment(value["node_environment"])
    if environment and (
        value["host"] != socket.gethostname().split(".")[0].lower()
        or value["runtime_sha256"] != plan.runtime_fingerprint()
        or value["toolchain_sha256"] != plan.toolchain_fingerprint()
        or value["python_sha256"] != plan.sha((plan.PREFIX / "bin/python").read_bytes())
    ):
        raise plan.TestEvidenceError("qualified test execution environment changed")
    return value


def fingerprint_only_stale(
    value: dict, expected: dict, policy: dict, *, source_sha256: str | None = None
) -> bool:
    """Allow automatic requalification only when recipe and test tools still match.

    One migration exception: a current-schema profile whose toolchain is the
    exact pre-qualify-env ~/.skenv toolchain on this authority predates the
    clean qualification prefix. Its recipe is unchanged, so it is offered one
    full native requalification bound to the new prefix instead of being
    blocked forever. The legacy fingerprint is recomputed from live bytes, so
    a ~/.skenv that has since drifted grants nothing.
    """
    _validate_shape(value)
    if (
        value["schema"]
        in {
            SCHEMA_V1,
            SCHEMA_V2,
            node.SCHEMA_V1,
            node.SCHEMA_V2,
            composite.SCHEMA_V1,
            composite.SCHEMA_V2,
        }
        or any(
            value.get(key) != expected.get(key)
            for key in ("card", "repository", "criteria_sha256")
        )
        or value["host"] != policy.get("authority_host")
        or value["host"] != socket.gethostname().split(".")[0].lower()
        or value["python_sha256"] != plan.sha((plan.PREFIX / "bin/python").read_bytes())
        or (source_sha256 is not None and value["source_sha256"] != source_sha256)
    ):
        return False
    toolchain = plan.toolchain_fingerprint()
    if value["toolchain_sha256"] != toolchain:
        legacy = plan.legacy_toolchain_fingerprint()
        if legacy is None or legacy == toolchain or value["toolchain_sha256"] != legacy:
            return False
        if node.is_node(value) or composite.is_composite(value):
            node.validate_environment(value["node_environment"])
        return True
    if node.is_node(value) or composite.is_composite(value):
        node.validate_environment(value["node_environment"])
    return value["runtime_sha256"] != plan.runtime_fingerprint() or value[
        "policy_sha256"
    ] != plan.execution_policy_fingerprint(policy)


def legacy_full_qualification_required(value: dict, expected: dict, policy: dict) -> bool:
    """Require a one-time full run when a legacy profile lacks safe bindings."""
    _validate_shape(value)
    legacy_v1 = value["schema"] in {SCHEMA_V1, node.SCHEMA_V1, composite.SCHEMA_V1}
    legacy_v2 = value["schema"] in {SCHEMA_V2, node.SCHEMA_V2, composite.SCHEMA_V2}
    if (
        not (legacy_v1 or legacy_v2)
        or any(value.get(key) != expected.get(key) for key in expected)
        or value["host"] != policy.get("authority_host")
        or value["host"] != socket.gethostname().split(".")[0].lower()
        or value["python_sha256"] != plan.sha((plan.PREFIX / "bin/python").read_bytes())
    ):
        return False
    if node.is_node(value) or composite.is_composite(value):
        node.validate_environment(value["node_environment"])
    return (
        legacy_v1
        or value["runtime_sha256"] != plan.runtime_fingerprint()
        or value["policy_sha256"] != plan.execution_policy_fingerprint(policy)
        or value.get("toolchain_sha256") != plan.toolchain_fingerprint()
    )


class ProfileRequalificationRequired(plan.TestEvidenceError):
    """A stored recipe needs fresh native qualification before dispatch."""

    __test__ = False


def qualify_profile(
    home: Path,
    core: dict,
    policy: dict,
    recipe: dict,
    qualified_by: str,
    qualification_sha256: str,
    *,
    source_sha256: str | None = None,
    node_environment: dict | None = None,
) -> Path:
    """Operator API after actual recipe qualification; no worker calls this API."""
    value = profile_value(
        core,
        policy,
        recipe,
        qualified_by,
        qualification_sha256,
        source_sha256=source_sha256,
        node_environment=node_environment,
    )
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
    source_sha256: str | None = None,
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
        if source_sha256 is None:
            source_sha256 = predecessor.get("source_sha256")
        if current != predecessor_sha256:
            raise plan.TestEvidenceError("profile predecessor changed")
        if qualification_sha256 == predecessor.get("qualification_sha256"):
            raise plan.TestEvidenceError("profile successor requires fresh qualification evidence")
        value = {
            "schema": SCHEMA if source_sha256 is not None else SCHEMA_V2,
            **expected,
            "recipe": recipe,
            "qualified_by": qualified_by,
            "qualification_sha256": qualification_sha256,
            "python_sha256": plan.sha((plan.PREFIX / "bin/python").read_bytes()),
            "toolchain_sha256": plan.toolchain_fingerprint(),
            "runtime_sha256": runtime_sha256,
            "policy_sha256": plan.execution_policy_fingerprint(policy),
            "host": socket.gethostname().split(".")[0].lower(),
        }
        if source_sha256 is not None:
            value["source_sha256"] = source_sha256
            expected["source_sha256"] = source_sha256
        if node_environment is not None:
            value.update(
                schema=(
                    (composite.SCHEMA if "pytest" in recipe else node.SCHEMA)
                    if source_sha256 is not None
                    else (composite.SCHEMA_V2 if "pytest" in recipe else node.SCHEMA_V2)
                ),
                node_environment=node_environment,
            )
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


def preflight(
    home: Path,
    core: dict,
    labels,
    policy: dict,
    *,
    source_sha256: str | None = None,
) -> dict | None:
    """Withhold only new source-only producers without a supported trusted contract."""
    labels = {str(label).lower() for label in labels}
    if "source-only" not in labels or "review" in labels or "seat-seraph" in labels:
        return None
    expected = contract(core)
    try:
        value, _ = read_profile(home, expected["card"])
    except FileNotFoundError:
        raise plan.TestEvidenceError("required-test-profile-unqualified") from None
    if (
        source_sha256 is not None
        and "source_sha256" in value
        and value["source_sha256"] != source_sha256
    ):
        raise plan.TestEvidenceError("qualified test source changed")
    if fingerprint_only_stale(value, expected, policy, source_sha256=source_sha256):
        raise ProfileRequalificationRequired(
            "profile fingerprints changed; native requalification required"
        )
    if legacy_full_qualification_required(value, expected, policy):
        raise ProfileRequalificationRequired("legacy profile requires a full native qualification")
    return validate_profile(value, expected, policy)


def seal_candidate(
    home: Path, binding: dict, workspace: Path, policy: dict, repository: str
) -> None:
    """Seal once only after acceptance has independently validated current custody."""
    predecessor_sha256 = None
    try:
        existing, _, predecessor_sha256 = plan.load_plan(home, binding, allow_completed=True)
        if existing["policy_sha256"] == plan.execution_policy_fingerprint(policy):
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
    if "source_sha256" in value:
        expected["source_sha256"] = plan.source_fingerprint(
            repository, binding["source_head"], binding["source_tree"]
        )
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
