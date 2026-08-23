"""ModelServer kind: spec normalization and Serving conditions (Phase 6, step 1).

Pure ModelServer-object model, mirroring the agent spec/conditions split in
agent.py. No I/O: observed state is a plain dict passed in by the caller
(the ModelController, a later card). No gateway wiring here (that is 6.2).
"""

from __future__ import annotations

from .conditions import _cond


class ModelServerSpecError(ValueError):
    """A ModelServer spec dict failed validation."""


def normalize_modelserver_spec(spec: dict) -> dict:
    """Validate and fill defaults for a ModelServer spec.

    Args:
        spec: Raw ModelServer spec dict.

    Returns:
        Normalized dict with name, ports, models, node, vramBudgetGb, deleted.

    Raises:
        ModelServerSpecError: name missing/non-str, ports non-list or
            containing a value outside 1..65535, models non-list, or node
            non-str when present.
    """
    name = spec.get("name")
    if not isinstance(name, str) or not name:
        raise ModelServerSpecError(
            f"modelserver spec requires a non-empty str 'name', got {name!r}"
        )
    ports = spec.get("ports", [])
    if not isinstance(ports, list):
        raise ModelServerSpecError(f"modelserver 'ports' must be a list, got {ports!r}")
    for port in ports:
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ModelServerSpecError(
                f"modelserver 'ports' entries must be ints in 1..65535, got {port!r}"
            )
    models = spec.get("models", [])
    if not isinstance(models, list):
        raise ModelServerSpecError(f"modelserver 'models' must be a list, got {models!r}")
    node = spec.get("node")
    if node is not None and not isinstance(node, str):
        raise ModelServerSpecError(f"modelserver 'node' must be a str when present, got {node!r}")
    return {
        "name": name,
        "ports": ports,
        "models": models,
        "node": node,
        "vramBudgetGb": spec.get("vramBudgetGb"),
        "deleted": bool(spec.get("deleted", False)),
    }


def modelserver_conditions(spec: dict, observed: dict, now_iso: str) -> list[dict]:
    """Derive a ModelServer's Serving condition from its spec and observed state."""
    open_ports = observed.get("open_ports", [])
    loaded_models = observed.get("loaded_models", [])
    ports_open = all(port in open_ports for port in spec.get("ports", []))
    models_loaded = all(model in loaded_models for model in spec.get("models", []))
    serving = ports_open and models_loaded
    return [
        _cond(
            "Serving",
            serving,
            "PortsAndModelsReady" if serving else "NotServing",
            f"vram_gb is {observed.get('vram_gb')!r}",
            now_iso,
        ),
    ]
