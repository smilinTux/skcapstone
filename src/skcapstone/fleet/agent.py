"""Agent kind: spec normalization and drift conditions (Phase 5, step 1).

Pure Agent-object model, mirroring the node spec/conditions split in
store.py/conditions.py. No I/O: observed state is a plain dict passed in
by the caller (the controller, Phase 5 step 2).
"""

from __future__ import annotations

from .conditions import _cond


class AgentSpecError(ValueError):
    """An agent spec dict failed validation."""


def normalize_agent_spec(spec: dict) -> dict:
    """Validate and fill defaults for an agent spec.

    Args:
        spec: Raw agent spec dict.

    Returns:
        Normalized dict with name, soul, model, daemon, deleted.

    Raises:
        AgentSpecError: name missing/non-str, soul/model non-str when
            present, or daemon non-dict.
    """
    name = spec.get("name")
    if not isinstance(name, str) or not name:
        raise AgentSpecError(f"agent spec requires a non-empty str 'name', got {name!r}")
    soul = spec.get("soul")
    if soul is not None and not isinstance(soul, str):
        raise AgentSpecError(f"agent 'soul' must be a str when present, got {soul!r}")
    model = spec.get("model")
    if model is not None and not isinstance(model, str):
        raise AgentSpecError(f"agent 'model' must be a str when present, got {model!r}")
    daemon = spec.get("daemon", {})
    if not isinstance(daemon, dict):
        raise AgentSpecError(f"agent 'daemon' must be a dict, got {daemon!r}")
    return {
        "name": name,
        "soul": soul,
        "model": model,
        "daemon": daemon,
        "deleted": bool(spec.get("deleted", False)),
    }


def agent_conditions(spec: dict, observed: dict, now_iso: str) -> list[dict]:
    """Derive an agent's drift conditions from its spec and observed state."""
    soul = spec.get("soul")
    model = spec.get("model")
    soul_loaded = soul is None or observed.get("active_soul") == soul
    model_routable = model is None or observed.get("model") == model
    daemon_ready = bool(observed.get("daemon_ready"))
    return [
        _cond(
            "SoulLoaded",
            soul_loaded,
            "SoulMatch" if soul_loaded else "SoulMismatch",
            f"active soul is {observed.get('active_soul')!r}",
            now_iso,
        ),
        _cond(
            "ModelRoutable",
            model_routable,
            "ModelMatch" if model_routable else "ModelMismatch",
            f"routed model is {observed.get('model')!r}",
            now_iso,
        ),
        _cond(
            "DaemonReady",
            daemon_ready,
            "DaemonSelfReport",
            "daemon ready" if daemon_ready else "daemon not ready",
            now_iso,
        ),
    ]
