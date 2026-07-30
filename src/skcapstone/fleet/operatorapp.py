"""Operatorapp kind: an app's operator-facet registration (R1.4).

A first-class subapp exposes an operator facet (explain / observe / act) via its
signed skworld.module.json. The manifest DECLARES which reversible standard
actions it proposes (``proposedStandardActions``); the fleet Operatorapp object
is where a HUMAN ratifies which of those the operator seat may run auto-standard
(``ratifiedStandardActions``). The AI operator seat may register and refresh an
Operatorapp (repos, cli, proposals, declared conditions) but can never write the
ratified list: that lever stays human-only, the same principle as freeze and
plane files (enforced in store.write_spec).

Pure model, no I/O: observed state is a plain dict passed in by the caller.
"""

from __future__ import annotations

from .conditions import _cond


class OperatorappSpecError(ValueError):
    """An Operatorapp spec dict failed validation."""


def _str_list(value, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise OperatorappSpecError(
            f"operatorapp {field!r} must be a list of non-empty str, got {value!r}"
        )
    return list(value)


def normalize_operatorapp_spec(spec: dict) -> dict:
    """Validate and fill defaults for an Operatorapp spec.

    Args:
        spec: Raw Operatorapp spec dict.

    Returns:
        Normalized dict with name, cli, repos, contractVersion,
        proposedStandardActions, ratifiedStandardActions, conditions, deleted.

    Raises:
        OperatorappSpecError: name missing/non-str, cli non-str when present,
            any action/condition/repo list malformed, or contractVersion
            non-int when present.
    """
    name = spec.get("name")
    if not isinstance(name, str) or not name:
        raise OperatorappSpecError(
            f"operatorapp spec requires a non-empty str 'name', got {name!r}"
        )
    cli = spec.get("cli")
    if cli is not None and (not isinstance(cli, str) or not cli):
        raise OperatorappSpecError(f"operatorapp 'cli' must be a non-empty str, got {cli!r}")
    contract_version = spec.get("contractVersion", 1)
    if not isinstance(contract_version, int):
        raise OperatorappSpecError(
            f"operatorapp 'contractVersion' must be an int, got {contract_version!r}"
        )
    return {
        "name": name,
        "cli": cli,
        "repos": _str_list(spec.get("repos", []), "repos"),
        "contractVersion": contract_version,
        "proposedStandardActions": _str_list(
            spec.get("proposedStandardActions", []), "proposedStandardActions"
        ),
        "ratifiedStandardActions": _str_list(
            spec.get("ratifiedStandardActions", []), "ratifiedStandardActions"
        ),
        "conditions": _str_list(spec.get("conditions", []), "conditions"),
        "deleted": bool(spec.get("deleted", False)),
    }


def operatorapp_conditions(spec: dict, observed: dict, now_iso: str) -> list[dict]:
    """Derive an Operatorapp's ProposalsRatified condition.

    ProposalsRatified is healthy (True) only when every proposed standard action
    has been ratified by a human. A pending proposal flips it False, surfacing to
    the operator that a human ratification is outstanding before those actions can
    run auto-standard.
    """
    proposed = spec.get("proposedStandardActions", [])
    ratified = set(spec.get("ratifiedStandardActions", []))
    all_ratified = all(action in ratified for action in proposed)
    return [
        _cond(
            "ProposalsRatified",
            all_ratified,
            "AllProposalsRatified" if all_ratified else "PendingHumanRatification",
            f"proposed={proposed!r} ratified={sorted(ratified)!r}",
            now_iso,
        ),
    ]
