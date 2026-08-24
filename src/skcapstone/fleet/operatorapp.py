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


#: contractVersion 2 (docs/OPERATOR_PLANE_MIGRATION.md Phase 2) transport values.
#: ``http`` names a caller-reachable ``skoperator.remote/v1`` endpoint (self-served
#: or fronted by the home node's sknoded); ``cli-local`` is the same node-local
#: exec contractVersion 1 already means, spelled explicitly for a v2 spec that
#: wants the fallback without an endpoint.
TRANSPORTS = frozenset({"http", "cli-local"})

#: The fields contractVersion 2 adds. Rejected on a contractVersion 1 spec (see
#: normalize_operatorapp_spec): a v1 spec means cli-local, full stop, and must
#: never carry a field that implies otherwise.
_V2_FIELDS = ("endpoint", "node", "transport")


def _str_list(value, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise OperatorappSpecError(
            f"operatorapp {field!r} must be a list of non-empty str, got {value!r}"
        )
    return list(value)


def _opt_str(value, field: str) -> str | None:
    if value is not None and (not isinstance(value, str) or not value):
        raise OperatorappSpecError(f"operatorapp {field!r} must be a non-empty str, got {value!r}")
    return value


def normalize_operatorapp_spec(spec: dict) -> dict:
    """Validate and fill defaults for an Operatorapp spec.

    contractVersion 2 (docs/OPERATOR_PLANE_MIGRATION.md Phase 2,
    docs/OPERATOR_PLANE_REMOTE_STANDARD.md section 9) adds three optional
    fields on top of contractVersion 1's ``cli``:

    * ``endpoint``: the app's ``skoperator.remote/v1`` URL. Authoritative when
      present (precedence order #1) -- see ``operator_seat.eyes``.
    * ``node``: the app's home node name (``fleet.paths.self_node_name()``
      shape). Only THIS node may exec ``cli`` locally for the app -- see
      :func:`cli_exec_eligible`.
    * ``transport``: ``"http"`` or ``"cli-local"``, echoing which of the two
      the app actually serves.

    This is store-side validation only: it does not register anything, call
    anything, or resolve a node name. A contractVersion 1 spec (the default,
    and every app registered in production today) MUST NOT carry any of the
    three -- that is what "v1 specs stay valid and mean cli-local" means: the
    absence of these fields IS the v1 contract, not an omission to backfill.

    Args:
        spec: Raw Operatorapp spec dict.

    Returns:
        Normalized dict with name, cli, repos, contractVersion, endpoint,
        node, transport, proposedStandardActions, ratifiedStandardActions,
        conditions, deleted.

    Raises:
        OperatorappSpecError: name missing/non-str, cli non-str when present,
            any action/condition/repo list malformed, contractVersion
            non-int, a v2 field set on a contractVersion 1 spec, endpoint/node
            non-str when present, or transport not one of :data:`TRANSPORTS`.
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
    if not isinstance(contract_version, int) or isinstance(contract_version, bool):
        raise OperatorappSpecError(
            f"operatorapp 'contractVersion' must be an int, got {contract_version!r}"
        )

    endpoint = spec.get("endpoint")
    node = spec.get("node")
    transport = spec.get("transport")
    if contract_version < 2:
        present = [field for field in _V2_FIELDS if spec.get(field) is not None]
        if present:
            raise OperatorappSpecError(
                f"operatorapp {present!r} require contractVersion >= 2 "
                f"(got contractVersion={contract_version!r}); a contractVersion 1 "
                "spec means cli-local and must not declare them"
            )
    else:
        endpoint = _opt_str(endpoint, "endpoint")
        node = _opt_str(node, "node")
        if transport is not None and transport not in TRANSPORTS:
            raise OperatorappSpecError(
                f"operatorapp 'transport' must be one of {sorted(TRANSPORTS)}, "
                f"got {transport!r}"
            )

    return {
        "name": name,
        "cli": cli,
        "repos": _str_list(spec.get("repos", []), "repos"),
        "contractVersion": contract_version,
        "endpoint": endpoint,
        "node": node,
        "transport": transport,
        "proposedStandardActions": _str_list(
            spec.get("proposedStandardActions", []), "proposedStandardActions"
        ),
        "ratifiedStandardActions": _str_list(
            spec.get("ratifiedStandardActions", []), "ratifiedStandardActions"
        ),
        "conditions": _str_list(spec.get("conditions", []), "conditions"),
        "deleted": bool(spec.get("deleted", False)),
    }


def cli_exec_eligible(spec: dict, local_node: str) -> tuple[bool, str]:
    """Whether THIS process may exec ``spec['cli']`` locally, per the Phase 2
    home-node rule (precedence order #2: "spec.cli executed by the app's
    HOME-NODE agent only (local fallback)").

    contractVersion 1 (the default, and every app registered in production
    today): always eligible. That is the entire meaning of "v1 specs stay
    valid and mean cli-local" -- this function changes nothing for them.

    contractVersion 2: eligible ONLY when ``spec['node']`` names THIS node.
    A v2 spec that declares no home node, or names a different one, is never
    exec'd here -- "a remote seat never execs spec.cli itself again"
    (docs/OPERATOR_PLANE_MIGRATION.md Phase 2). Pure: no I/O, no subprocess,
    no node-name resolution (the caller supplies ``local_node``, typically
    ``fleet.paths.self_node_name()``).

    Args:
        spec: A normalized (or raw, ``.get``-shaped) Operatorapp spec dict.
        local_node: This process's own node name.

    Returns:
        ``(eligible, reason)``. ``reason`` is empty when eligible, else a
        human sentence explaining why exec was refused.
    """
    contract_version = spec.get("contractVersion", 1)
    if not isinstance(contract_version, int) or contract_version < 2:
        return True, ""
    node = spec.get("node")
    if node is not None and node == local_node:
        return True, ""
    if node:
        reason = (
            f"contractVersion=2 spec is homed on node {node!r}, not this node "
            f"({local_node!r}); a remote seat never execs spec.cli itself"
        )
    else:
        reason = (
            "contractVersion=2 spec declares no home node ('node' is unset); "
            "a remote seat never execs spec.cli itself without one"
        )
    return False, reason


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
