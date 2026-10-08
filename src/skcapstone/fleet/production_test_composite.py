"""One sealed candidate, fixed language phases and independently verified reports."""

from . import production_test_node as node
from . import production_test_plan as plan

SCHEMA_V1 = "skfleet.qualified-composite-test-profile/v1"
SCHEMA_V2 = "skfleet.qualified-composite-test-profile/v2"
SCHEMA = "skfleet.qualified-composite-test-profile/v3"


def is_composite(profile: dict | None) -> bool:
    """Recognize only the explicit combined profile variant."""
    return isinstance(profile, dict) and profile.get("schema") in {SCHEMA_V1, SCHEMA_V2, SCHEMA}


def phase(profile: dict | None, language: str) -> dict | None:
    """Project fixed phase inputs; original source/claim binding stays on the plan."""
    if not is_composite(profile):
        return profile
    recipe = profile["recipe"]
    if language == "node":
        return {**profile, "schema": node.SCHEMA, "recipe": {"vitest": recipe["vitest"]}}
    if language != "python":
        raise plan.TestEvidenceError("invalid test phase")
    return {
        **{key: value for key, value in profile.items() if key != "node_environment"},
        "schema": "skfleet.qualified-test-profile/v3",
        "recipe": {key: value for key, value in recipe.items() if key != "vitest"},
    }


def command_profile(profile: dict | None, argv: list[str]) -> dict | None:
    """Select a sandbox only for an exact parser-generated command."""
    if not is_composite(profile):
        return profile
    from .production_pytest_recipe import recipe_checks

    for language in ("node", "python"):
        value = phase(profile, language)
        if any(check["argv"] == argv for check in recipe_checks(value["recipe"])):
            return value
    raise plan.TestEvidenceError("command outside qualified composite phases")


def validate_source(profile: dict, workspace) -> None:
    """Recheck every required language against the same sealed source."""
    from .production_pytest_recipe import validate_source as python_source

    if node.is_node(profile) or is_composite(profile):
        node.validate_environment(profile["node_environment"], workspace)
    if not node.is_node(profile) or is_composite(profile):
        python_source(phase(profile, "python")["recipe"], workspace)


def report_names(profile: dict | None) -> tuple[str, ...]:
    """Keep both independent native reports, leaving single-language names intact."""
    if is_composite(profile):
        return ("vitest.xml", "pytest.xml")
    return ("vitest.xml",) if node.is_node(profile) else ("pytest.xml",)


def python_selection(profile: dict | None) -> bool:
    """Require exact collection evidence for a combined Python phase too."""
    from .production_pytest_recipe import requires_selection

    value = phase(profile, "python")
    return bool(value and not node.is_node(value) and requires_selection(value["recipe"]))


def evidence(raw: dict[str, bytes], profile: dict | None, selection=None) -> tuple[str, dict]:
    """Rehash both raw files and recompute strict counts, never reported aggregates."""
    if set(raw) != set(report_names(profile)):
        raise plan.TestEvidenceError("required phase reports are missing or unexpected")
    if not is_composite(profile):
        value = next(iter(raw.values()))
        return plan.sha(value), plan.junit_counts(value, profile, selection=selection)
    counts = {
        "node": plan.junit_counts(raw["vitest.xml"], phase(profile, "node")),
        "python": plan.junit_counts(
            raw["pytest.xml"], phase(profile, "python"), selection=selection
        ),
    }
    digest = plan.production_builder.digest({name: plan.sha(value) for name, value in raw.items()})
    return digest, {"total": sum(row["total"] for row in counts.values()), "phases": counts}
