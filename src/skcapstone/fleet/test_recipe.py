"""Declared test recipe contract: card binding and the fixed test recipe a card runs."""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import production_test_composite as composite
from . import production_test_plan as plan
from .production_builder import digest
from .production_pytest_recipe import recipe_checks


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
    # Board labels are scope too: SKLegal cards carry frontend/backend labels
    # whose text names neither a file suffix nor a test runner.
    labels = {str(label).lower() for label in core.get("labels") or ()}
    node_scope = node_scope or "frontend" in labels
    python_scope = python_scope or bool(labels & {"backend", "python"})
    repository = (core.get("links") or {}).get("repository") or (core.get("meta") or {}).get(
        "repository", ""
    )
    python_project = (workspace / "pyproject.toml").is_file() and (workspace / "tests").is_dir()
    node_project = (workspace / "apps/web/package.json").is_file()

    def node_recipe():
        from . import production_test_node as node

        web = workspace / "apps/web"
        # Only source test modules. Vitest writes snapshot artifacts such as
        # src/__snapshots__/visual-regression.test.tsx.snap, which contain
        # ".test." but are not tests; counting one failed every SKLegal Node
        # recipe with "invalid Node per-file coverage" (2026-10-09).
        test_files = [
            path
            for path in web.rglob("*")
            if path.is_file()
            and (".test." in path.name or ".spec." in path.name)
            and path.suffix in {".ts", ".tsx", ".js", ".jsx"}
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
        # A baseline recipe for a large suite lists clean files individually
        # where a directory holds a module-level skip, plus its deselected
        # tests (~31 KB for skcapstone, over 64 KiB once extended).
        if not isinstance(configured, str) or len(configured) > 262144:
            raise plan.TestEvidenceError("operator test recipe is invalid")
        try:
            recipe = json.loads(configured, object_pairs_hook=plan._unique_object)
            recipe_checks(recipe)
            environment = None
            if "node_test" in recipe:
                from . import production_node_test_recipe as native
                from . import production_test_node as node

                native.validate_source(recipe, workspace)
                environment = node.qualified_environment(workspace)
            elif "vitest" in recipe:
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

    if (workspace / "package.json").is_file() and not (workspace / "apps/web").exists():
        from . import production_node_test_recipe as native
        from . import production_test_node as node

        tests = sorted(
            path.relative_to(workspace).as_posix()
            for path in (workspace / "tests").glob("*.test.mjs")
        )
        if tests:
            recipe = {"node_test": dict.fromkeys(tests, 1)}
            native.validate_source(recipe, workspace)
            return recipe, node.qualified_environment(workspace)

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


def declared_recipe(core: dict, workspace: Path) -> dict:
    """The recipe record a card's declared tests run from, derived at its exact source.

    Nothing is qualified, stored or compared against an earlier run: the record
    binds the card contract to the recipe chosen for ``workspace`` and is sealed
    into the plan of the one candidate commit that will run it.
    """
    from . import production_test_node as node

    recipe, environment = initial_recipe(core, workspace)
    value = {"schema": plan.PYTHON_RECIPE_SCHEMA, **contract(core), "recipe": recipe}
    if environment is not None:
        value["schema"] = (
            composite.SCHEMA if set(recipe) - {"vitest", "node_test"} else node.SCHEMA
        )
        value["node_environment"] = environment
    return value
