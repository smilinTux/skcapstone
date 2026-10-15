"""sknoded actuation: the 30s converge pass (spec section 6, steps 2-4).

Gate order is the whole point: tree readable, then freeze, then whether the
freeze store is even human-provisioned yet (AUTONOMY_ARCHITECTURE.md section
3.6: a node with no provisioned freeze store does not heal), then per-node
opt-in, then per-service spec validity and pause, and only then verbs
under bounded backoff. Anything unreadable degrades to "touch nothing".
"""

from __future__ import annotations

import socket
import time
from typing import Callable

from . import actuation, alerts, backoff, events, profiles, signing, store
from .paths import FleetPaths
from .services import ServiceSpecError, normalize_service_spec

ACTUATION_INTERVAL_S = 30


def tcp_probe(check: dict) -> bool:
    """v1 health check: TCP connect to localhost:port."""
    try:
        with socket.create_connection(("127.0.0.1", int(check["port"])), timeout=1.0):
            return True
    except Exception:
        return False


def actuation_enabled(paths: FleetPaths, node: str) -> bool:
    """True only when the operator opted this node in (spec R4, section 6).

    Missing node object, unreadable spec, or absent flag all mean
    report-only. Every node is born report-only.
    """
    spec = store.read_spec(paths, "node", node)
    if spec is None:
        return False
    return bool(spec.get("spec", {}).get("actuate"))


def local_services(paths: FleetPaths, node: str) -> list[dict]:
    """Service placements addressed to this node, joined with their specs.

    spec_payload is None when the spec file is missing or unreadable; the
    caller must treat that as "do not touch" (degrade-safe).
    """
    out: list[dict] = []
    for placement in store.list_placements(paths, "service"):
        if placement.get("node") != node:
            continue
        name = placement["name"]
        out.append(
            {
                "name": name,
                "placement": placement,
                "spec_payload": store.read_spec(paths, "service", name),
            }
        )
    return out


def verify_desired(
    spec_payload: dict,
    placement: dict | None,
    verifier,
) -> tuple[bool, str]:
    """Classify the pair of files actuation consumes (Card 3.5).

    Both the service spec and its placement must verify. A missing
    verifier (no roster, capauth absent) is unverified by definition:
    under enforce that refuses NEW actuation and never touches running
    services (fail safe at the trust boundary).
    """
    if verifier is None:
        return (False, "no verifier available (empty roster or capauth missing)")
    failures: list[str] = []
    for label, payload in (("spec", spec_payload), ("placement", placement)):
        if payload is None:
            continue
        status, detail = signing.verify_payload(payload, verifier)
        if status != "verified":
            failures.append(f"{label} {status}: {detail}")
    if failures:
        return (False, "; ".join(failures))
    return (True, "spec and placement verified")


def _cond(type: str, active: bool, reason: str, message: str, now_iso: str) -> dict:
    return {
        "type": type,
        "status": "True" if active else "False",
        "reason": reason,
        "message": message,
        "lastTransition": now_iso,
    }


def _now_iso(now: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_service_status(
    paths: FleetPaths,
    writer: store.Writer,
    name: str,
    state: actuation.UnitState,
    spec: dict,
    generation: int,
    conds: list[dict],
    track: dict,
) -> None:
    from .conditions import merge_transitions

    previous = store.read_status(paths, "service", name, writer.node) or {}
    conds = merge_transitions(conds, previous.get("conditions", []))
    store.write_status(
        paths,
        "service",
        name,
        node=writer.node,
        status={
            "state": state.state,
            "pid": state.pid,
            "since": state.since,
            "restarts": int(track["attempts"]),
            "runtime": spec["runtime"],
        },
        conditions=conds,
        observed_generation=generation,
        writer=writer,
    )


def _heal(
    paths: FleetPaths,
    writer: store.Writer,
    name: str,
    spec: dict,
    state: actuation.UnitState,
    track: dict,
    runner: actuation.Runner,
    now: float,
) -> None:
    """One bounded heal attempt (start or restart) with logs-on-failure.

    Refuses regardless of caller when `store.check_actuation_gate` is not
    allowed (belt-and-suspenders: `converge_once`'s `mode` computation
    already keeps `may_heal` false in that case, but this is one of the
    two surfaces AUTONOMY_ARCHITECTURE.md section 3.6 names explicitly, so
    it does not rely solely on an upstream caller getting the gate right).
    """
    if not store.check_actuation_gate(paths).allowed:
        return
    if state.state == "failed":
        logs = actuation.failure_logs(spec, runner=runner)
        events.emit(
            paths,
            writer,
            kind="service",
            name=name,
            type="Actuation",
            reason="FailureLogs",
            message=logs[-800:],
            now=now,
        )
        ok = actuation.restart(spec, runner=runner)
        reason = "Restarted" if ok else "RestartFailed"
    else:
        ok = actuation.start(spec, runner=runner)
        reason = "Started" if ok else "StartFailed"
    backoff.record_attempt(track, now)
    events.emit(
        paths,
        writer,
        kind="service",
        name=name,
        type="Actuation",
        reason=reason,
        message=f"unit={spec['unit']} attempt={track['attempts']}",
        now=now,
    )


def converge_service(
    paths: FleetPaths,
    node: str,
    name: str,
    spec_payload: dict | None,
    *,
    writer: store.Writer,
    runner: actuation.Runner,
    prober: Callable[[dict], bool],
    mode: str,
    now: float,
    sig_mode: str = "off",
    verification: tuple[bool, str] = (True, ""),
    role: str = "",
    profile_gate: str = "off",
) -> dict:
    """Converge one locally placed service. Returns a summary dict.

    Args:
        role: The node's install profile name, "" when unbound.
        profile_gate: off (default) | shadow | enforce, the same rollout
            shape as sig_mode. shadow reports only; enforce additionally
            refuses to heal, and never stops anything.
    """
    now_iso = _now_iso(now)
    if spec_payload is None:
        events.emit(
            paths,
            writer,
            kind="service",
            name=name,
            type="Degrade",
            reason="SpecUnreadable",
            message="spec missing or unreadable; unit left untouched",
            now=now,
        )
        return {"skipped": "spec unreadable"}
    try:
        spec = normalize_service_spec(spec_payload.get("spec", {}))
    except ServiceSpecError as exc:
        events.emit(
            paths,
            writer,
            kind="service",
            name=name,
            type="Degrade",
            reason="SpecInvalid",
            message=str(exc),
            now=now,
        )
        return {"skipped": f"spec invalid: {exc}"}
    if spec["deleted"]:
        return {"skipped": "tombstoned (deleted: true); unit left untouched"}

    state = actuation.state_of(spec, runner=runner)
    track = backoff.tracker(node, name)
    verified_ok, verify_detail = verification
    if sig_mode != "off" and not verified_ok:
        if events.emit(
            paths,
            writer,
            kind="service",
            name=name,
            type="Trust",
            reason="SpecUnverified",
            message=verify_detail,
            now=now,
        ):
            if sig_mode == "enforce":
                alerts.send_alert(
                    f"fleet: service {name} on {node} REFUSED actuation: {verify_detail}",
                    level="error",
                )

    outside_profile = False
    if profile_gate != "off":
        outside_profile = not profiles.unit_allowed(
            role, spec["unit"], manifests=profiles.manifest_dir(paths)
        )
        if outside_profile:
            events.emit(
                paths,
                writer,
                kind="service",
                name=name,
                type="Degrade",
                reason="OutsideProfile",
                message=(
                    f"unit={spec['unit']} is forbidden for role {role!r}; "
                    + ("healing suppressed" if profile_gate == "enforce" else "report only")
                ),
                now=now,
            )

    probe_ok = True
    if spec["healthCheck"] is not None and state.state == "active":
        probe_ok = prober(spec["healthCheck"])
    healthy = state.state == "active" and probe_ok
    acted = "none"

    if healthy:
        backoff.record_healthy(track, now)
    unhealthy_unit = state.state in {"failed", "inactive", "missing"}
    # Enforcement is the REFUSAL TO HEAL and nothing more. There is
    # deliberately no stop verb anywhere on this path: stopping a running
    # service because a manifest disagrees with it is precisely the outage
    # this epic exists to prevent, and a stale manifest is the likeliest
    # cause of the disagreement.
    blocked_by_profile = profile_gate == "enforce" and outside_profile
    may_heal = (
        mode == "actuate"
        and not (sig_mode == "enforce" and not verified_ok)
        and not blocked_by_profile
        and not spec["paused"]
        and spec["restartPolicy"] == "on-failure"
        and unhealthy_unit
    )
    if may_heal:
        if backoff.is_crash_looping(track):
            if events.emit(
                paths,
                writer,
                kind="service",
                name=name,
                type="Actuation",
                reason="CrashLooping",
                message=f"unit={spec['unit']} attempts={track['attempts']}; " "healing stopped",
                now=now,
            ):
                alerts.send_alert(
                    f"fleet: service {name} CrashLooping on {node} "
                    f"({track['attempts']} attempts); healing stopped",
                    level="error",
                )
            acted = "crash-looping"
        elif backoff.allowed(track, now):
            _heal(paths, writer, name, spec, state, track, runner, now)
            acted = "healed"
        else:
            acted = "backoff-wait"
    if mode == "actuate" and sig_mode == "enforce" and not verified_ok:
        acted = "unverified"
    elif mode == "actuate" and blocked_by_profile:
        acted = "outside-profile"

    if healthy:
        ready = _cond("Ready", True, "UnitActive", f"unit {spec['unit']} active", now_iso)
    elif state.state == "active" and not probe_ok:
        ready = _cond(
            "Ready", False, "ProbeFailed", f"port {spec['healthCheck']['port']} closed", now_iso
        )
    elif state.state == "unknown":
        ready = {
            **_cond("Ready", False, "StateUnknown", "unit state unknown", now_iso),
            "status": "Unknown",
        }
    else:
        ready = _cond("Ready", False, "UnitDown", f"unit state {state.state}", now_iso)
    if mode != "actuate":
        _prog_reason = {
            "frozen": "Frozen",
            "unprovisioned": "Unprovisioned",
        }.get(mode, "ReportOnly")
        _prog_message = {
            "frozen": "actuation halted: frozen",
            "unprovisioned": "actuation halted: freeze store not provisioned",
        }.get(mode, "node not opted in")
        prog = _cond("Progressing", False, _prog_reason, _prog_message, now_iso)
    elif spec["paused"]:
        prog = _cond("Progressing", False, "Paused", "spec.paused is true", now_iso)
    else:
        prog = _cond(
            "Progressing",
            acted in {"healed", "backoff-wait"},
            "Healing" if acted in {"healed", "backoff-wait"} else "Converged",
            f"last action: {acted}",
            now_iso,
        )
    conds = [
        ready,
        prog,
        _cond(
            "CrashLooping",
            backoff.is_crash_looping(track),
            "BackoffExhausted" if backoff.is_crash_looping(track) else "WithinBudget",
            f"attempts={track['attempts']}",
            now_iso,
        ),
        *(
            [
                _cond(
                    "SpecUnverified",
                    not verified_ok,
                    "SignatureInvalid" if not verified_ok else "SignatureOk",
                    verify_detail,
                    now_iso,
                )
            ]
            if sig_mode != "off"
            else []
        ),
        *(
            [
                _cond(
                    "OutsideProfile",
                    outside_profile,
                    "UnitForbiddenForRole" if outside_profile else "UnitPermitted",
                    f"role={role or 'unbound'} unit={spec['unit']} gate={profile_gate}",
                    now_iso,
                )
            ]
            if profile_gate != "off"
            else []
        ),
    ]
    generation = int(spec_payload.get("generation", 0))
    _write_service_status(paths, writer, name, state, spec, generation, conds, track)
    return {"state": state.state, "acted": acted}


def converge_once(
    paths: FleetPaths,
    node: str,
    *,
    runner: actuation.Runner | None = None,
    prober: Callable[[dict], bool] | None = None,
    now: float | None = None,
    verifier=None,
) -> dict:
    """One actuation pass for this node (spec section 6, steps 2-4)."""
    runner = actuation.default_runner if runner is None else runner
    prober = tcp_probe if prober is None else prober
    now = time.time() if now is None else now
    sig_mode = signing.signing_mode()
    profile_gate = profiles.gate_mode()
    if sig_mode != "off" and verifier is None:
        verifier = signing.capauth_verifier()
    role = ""
    try:
        gate = store.check_actuation_gate(paths)
        if gate.reason == store.REASON_FROZEN:
            mode = "frozen"
        elif gate.reason == store.REASON_UNPROVISIONED:
            mode = "unprovisioned"
        elif actuation_enabled(paths, node):
            mode = "actuate"
        else:
            mode = "report-only"
        if profile_gate != "off":
            role = profiles.profile_of(store.read_spec(paths, "node", node)) or ""
        entries = local_services(paths, node)
    except OSError:
        return {"mode": "degraded", "services": {}}
    writer = store.Writer(role="sknoded", node=node, identity=store.writer_identity())
    results: dict[str, dict] = {}
    for entry in entries:
        verification = (True, "")
        if sig_mode != "off" and entry["spec_payload"] is not None:
            verification = verify_desired(entry["spec_payload"], entry["placement"], verifier)
        results[entry["name"]] = converge_service(
            paths,
            node,
            entry["name"],
            entry["spec_payload"],
            writer=writer,
            runner=runner,
            prober=prober,
            mode=mode,
            now=now,
            sig_mode=sig_mode,
            verification=verification,
            role=role,
            profile_gate=profile_gate,
        )
    return {"mode": mode, "services": results}
