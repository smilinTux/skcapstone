"""Effective lanes: the operator's allow list intersected with route health.

``production.json`` lanes stay the ALLOW list and are never mutated here. Each
dispatch cycle the rotator keeps a ledger of exact-route preflight outcomes
(the probe it already performs before every launch). A lane whose selected route
fails preflight is withdrawn for the rest of the cycle and the card is offered to
the next compatible lane, so a dead provider (DeepSeek out of credit, Z.ai quota
429) no longer pins cards that another allowed family could run. Failures expire
through ``preflight_failure_expired``, so a recovered provider is probed again
and becomes effective as soon as that probe passes.

Nothing here talks to a provider. ``preflight`` callables are supplied by the
caller and are expected to be the ledger-backed ``preflight_through_ledger``.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, MutableMapping

from ..fleet_route_preflight import preflight_failure_expired

UNHEALTHY_PREFIX = "route-preflight-failed:"


def preflight_through_ledger(
    ledger: MutableMapping[Any, dict],
    key: Any,
    probe: Callable[[], Any],
    *,
    now: float,
) -> tuple[Any, str, bool]:
    """Return ``(result, reason, cached)``; ``result`` is None when blocked.

    ``probe`` runs only on a ledger miss or after a failure has expired, so the
    cycle pays at most one provider request per exact route.
    """
    entry = ledger.get(key)
    if (
        entry is not None
        and entry["result"] is None
        and preflight_failure_expired(entry["failed_at"], now)
    ):
        ledger.pop(key, None)
        entry = None
    if entry is not None:
        if entry["result"] is None:
            return None, "cached-failure", True
        return entry["result"], "", True
    try:
        result = probe()
    except ValueError as exc:
        reason = str(exc)[:140]
        ledger[key] = {"result": None, "failed_at": now, "reason": reason}
        return None, reason, False
    ledger[key] = {"result": result, "failed_at": now, "reason": ""}
    return result, "", False


def select_lane_with_fallthrough(
    select: Callable[[Mapping[str, tuple]], tuple[str | None, str]],
    model_for_lane: Callable[[str], str | None],
    preflight: Callable[[str], tuple[bool, str]],
    health: MutableMapping[str, tuple],
) -> tuple[str | None, str, list[dict]]:
    """Pick a lane whose exact route passes preflight, withdrawing dead ones.

    ``select`` owns every policy rule (explicit pins, disabled families, size
    floors): it only ever returns an allowed, compatible lane. A failing lane is
    marked unhealthy in ``health`` so ``select`` cannot return it again; a pin to
    that lane therefore ends in ``None`` and the card waits. Each iteration
    withdraws one lane, so the loop is bounded by the lane count.
    """
    hops: list[dict] = []
    while True:
        name, reason = select(health)
        if name is None:
            break
        model = model_for_lane(name)
        if model is None:
            break
        ok, why = preflight(model)
        if ok:
            break
        hops.append({"from": name, "model": model, "reason": why})
        health[name] = (False, UNHEALTHY_PREFIX + why)
    for index, hop in enumerate(hops):
        hop["to"] = hops[index + 1]["from"] if index + 1 < len(hops) else (name or "none")
    return name, reason, hops


def fallthrough_log_lines(host: str, card: str, hops: list[dict]) -> list[str]:
    return [
        "ROUTE_FALLTHROUGH|%s|%s|from=%s|to=%s|reason=%s"
        % (host, card, hop["from"], hop["to"], hop["reason"] or "preflight-failed")
        for hop in hops
    ]


def effective_lanes_line(host: str, lanes, unhealthy: Mapping[str, str]) -> str:
    """EFFECTIVE_LANES for the cycle: allowed, observed-healthy, unhealthy."""
    allowed = [lane["name"] for lane in lanes if lane.get("target", 0) > 0]
    bad = {name: unhealthy[name] for name in allowed if name in unhealthy}
    healthy = [name for name in allowed if name not in bad]
    return "EFFECTIVE_LANES|%s|allowed=%s|healthy=%s|unhealthy=%s" % (
        host,
        ",".join(allowed) or "none",
        ",".join(healthy) or "none",
        ",".join("%s:%s" % (n, r.replace(",", ";")[:80]) for n, r in bad.items()) or "none",
    )
