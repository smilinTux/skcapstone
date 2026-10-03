"""Required timer enablement audit and bounded convergence."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from skcapstone.estate import sovereign_home

Runner = Callable[..., object]
GOVERNED_SEAT_SERVICES = (
    "skfleet-atlas.service",
    "skfleet-seraph.service",
    "skfleet-niobe.service",
    "skfleet-niobe-live.service",
)


def _acquire_scheduler_migration_lock(config_home: Path) -> int | None:
    """Acquire the shared forward/rollback scheduler transaction lock."""

    path = config_home / "skcapstone" / "scheduler-migration.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _release_scheduler_migration_lock(fd: int) -> None:
    """Release a scheduler migration lock descriptor."""

    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def policy_revision(profile: dict) -> str:
    """Stable revision for the exact profile policy being applied."""
    encoded = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def required_timers(profile: dict) -> list[str]:
    """Timers mandated by the profile, excluding allowed-only units."""
    required = (profile.get("units") or {}).get("required") or []
    return sorted({unit for unit in required if unit.endswith(".timer")})


def forbidden_timers(profile: dict) -> list[str]:
    """Timers that must be disabled and inactive for the profile."""

    forbidden = (profile.get("units") or {}).get("mustNot") or []
    return sorted({unit for unit in forbidden if unit.endswith(".timer")})


def approved_legacy_timers(home: Path) -> frozenset[str]:
    """Return Atlas, Seraph, and exactly one activation-selected Niobe timer."""

    from .seat_cycle_orchestrator import select_niobe_timer

    return frozenset({"skfleet-atlas.timer", "skfleet-seraph.timer", select_niobe_timer(home)})


def legacy_timer_rollback_profile(legacy_timers: list[str], *, home: Path) -> dict:
    """Build the reverse-migration fence for restoring legacy seat timers."""

    requested = set(legacy_timers)
    approved = approved_legacy_timers(home)
    if requested != approved or len(legacy_timers) != len(requested):
        raise ValueError("legacy rollback requires exactly: " + ", ".join(sorted(approved)))
    return {
        "units": {
            "required": sorted(requested),
            "mustNot": ["skfleet-seat-cycle.timer"],
        }
    }


def _state(unit: str, runner: Runner) -> dict[str, str]:
    try:
        result = runner(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=LoadState,UnitFileState,ActiveState,SubState,FragmentPath,Job",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return {
            "_known": "false",
            "LoadState": "unknown",
            "UnitFileState": "unknown",
            "ActiveState": "unknown",
            "Job": "unknown",
        }
    state = dict(
        line.split("=", 1)
        for line in str(getattr(result, "stdout", "")).splitlines()
        if "=" in line
    )
    authoritative_absence = (
        state.get("LoadState") == "not-found" and state.get("ActiveState") == "inactive"
    )
    if getattr(result, "returncode", 1) != 0 and not authoritative_absence:
        return {
            "_known": "false",
            "LoadState": "unknown",
            "UnitFileState": "unknown",
            "ActiveState": "unknown",
            "Job": "unknown",
        }
    state["_known"] = "true"
    return state


def audit_timer(unit: str, *, runner: Runner, config_home: Path) -> dict:
    """Read one timer's runtime and exact wants-link state without mutation."""
    state = _state(unit, runner)
    link = config_home / "systemd" / "user" / "timers.target.wants" / unit
    fragment_text = state.get("FragmentPath") or ""
    fragment = Path(fragment_text) if fragment_text else None
    link_ok = link.is_symlink() and fragment is not None and link.resolve() == fragment.resolve()
    enabled = state.get("UnitFileState") == "enabled" and link_ok
    active = (
        state.get("_known") == "true"
        and state.get("LoadState") == "loaded"
        and state.get("ActiveState") == "active"
        and state.get("SubState") in {"waiting", "running"}
        and state.get("Job") in {"", "0", "n/a"}
    )
    return {
        "unit": unit,
        "loaded": state.get("LoadState") == "loaded",
        "enabled": enabled,
        "active_waiting": active,
        "unit_file_state": state.get("UnitFileState", "unknown"),
        "wants_link": str(link),
        "wants_link_ok": link_ok,
        "fragment_path": fragment_text,
        "drift": not (state.get("LoadState") == "loaded" and enabled and active),
    }


def audit_forbidden_timer(unit: str, *, runner: Runner) -> dict:
    """Prove a forbidden timer and its paired service cannot execute."""

    timer = _state(unit, runner)
    service_name = unit.removesuffix(".timer") + ".service"
    service = _state(service_name, runner)
    timer_safe = (
        timer.get("_known") == "true"
        and timer.get("Job") in {"", "0", "n/a"}
        and (
            (
                timer.get("LoadState") == "loaded"
                and timer.get("UnitFileState") == "disabled"
                and timer.get("ActiveState") == "inactive"
            )
            or (timer.get("LoadState") == "not-found" and timer.get("ActiveState") == "inactive")
        )
    )
    service_safe = (
        service.get("_known") == "true"
        and service.get("Job") in {"", "0", "n/a"}
        and (
            (service.get("LoadState") == "loaded" and service.get("ActiveState") == "inactive")
            or (
                service.get("LoadState") == "not-found"
                and service.get("ActiveState") == "inactive"
            )
        )
    )
    return {
        "unit": unit,
        "loaded": timer.get("LoadState") == "loaded",
        "enabled": timer.get("LoadState") == "loaded" and timer.get("UnitFileState") != "disabled",
        "active": timer.get("ActiveState") != "inactive",
        "service": service_name,
        "service_inactive": service_safe,
        "safe": timer_safe and service_safe,
    }


def _append(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, (json.dumps(payload, sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)


def mutate(
    unit: str,
    *,
    enabled: bool,
    runner: Runner,
    evidence_path: Path,
    actor: str,
    source_revision: str,
    prior_state: str,
    now: Callable[[], datetime] | None = None,
) -> bool:
    """Change one unit's enablement and append evidence for the attempt."""
    requested = "enabled" if enabled else "disabled"
    try:
        result = runner(
            ["systemctl", "--user", "enable" if enabled else "disable", unit],
            capture_output=True,
            text=True,
            timeout=10,
        )
        ok = getattr(result, "returncode", 1) == 0
        detail = str(getattr(result, "stderr", "") or "")[-500:]
    except Exception as exc:
        ok = False
        detail = str(exc)[-500:]
    clock = now or (lambda: datetime.now(timezone.utc))
    _append(
        evidence_path,
        {
            "actor": actor,
            "prior_state": prior_state,
            "requested_state": requested,
            "result": "ok" if ok else "failed",
            "result_detail": detail,
            "source_revision": source_revision,
            "timestamp": clock().astimezone(timezone.utc).isoformat(),
            "unit": unit,
        },
    )
    return ok


def converge_required_timers(
    profile: dict,
    *,
    runner: Runner,
    config_home: Path,
    evidence_path: Path,
    actor: str,
    source_revision: str | None = None,
    enable: bool = True,
    start: bool = True,
) -> list[dict]:
    """Enable and/or start only profile-required timers, idempotently."""
    revision = source_revision or policy_revision(profile)
    results = []
    for unit in required_timers(profile):
        before = audit_timer(unit, runner=runner, config_home=config_home)
        if not before["loaded"]:
            results.append(before)
            continue
        if enable and not before["enabled"]:
            if not mutate(
                unit,
                enabled=True,
                runner=runner,
                evidence_path=evidence_path,
                actor=actor,
                source_revision=revision,
                prior_state=before["unit_file_state"],
            ):
                results.append(audit_timer(unit, runner=runner, config_home=config_home))
                continue
        current = audit_timer(unit, runner=runner, config_home=config_home)
        if start and not current["active_waiting"]:
            try:
                runner(
                    ["systemctl", "--user", "start", unit],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except Exception:
                pass
        after = audit_timer(unit, runner=runner, config_home=config_home)
        after["converged"] = (not enable or after["enabled"]) and (
            not start or after["active_waiting"]
        )
        results.append(after)
    return results


def converge_forbidden_timers(
    profile: dict,
    *,
    runner: Runner,
    config_home: Path,
    evidence_path: Path,
    actor: str,
    source_revision: str | None = None,
) -> list[dict]:
    """Disable and stop forbidden timers before required timers are enabled."""

    del config_home
    revision = source_revision or policy_revision(profile)
    results = []
    for unit in forbidden_timers(profile):
        service = unit.removesuffix(".timer") + ".service"
        before = _state(unit, runner)
        try:
            completed = runner(
                ["systemctl", "--user", "disable", "--now", unit],
                capture_output=True,
                text=True,
                timeout=10,
            )
            disable_ok = getattr(completed, "returncode", 1) == 0
            detail = str(getattr(completed, "stderr", "") or "")[-500:]
        except Exception as exc:
            disable_ok = False
            detail = str(exc)[-500:]
        try:
            stopped = runner(
                ["systemctl", "--user", "stop", service],
                capture_output=True,
                text=True,
                timeout=310,
            )
            stop_ok = getattr(stopped, "returncode", 1) == 0
            if not stop_ok:
                detail = (detail + "; " + str(getattr(stopped, "stderr", "") or ""))[-500:]
        except Exception as exc:
            stop_ok = False
            detail = (detail + "; " + str(exc))[-500:]
        row = audit_forbidden_timer(unit, runner=runner)
        safe = row["safe"]
        _append(
            evidence_path,
            {
                "actor": actor,
                "prior_state": f"{before.get('UnitFileState', 'unknown')}/"
                f"{before.get('ActiveState', 'unknown')}",
                "requested_state": "disabled_inactive",
                "result": "ok" if safe else "failed",
                "result_detail": detail,
                "disable_result": "ok" if disable_ok else "failed",
                "stop_result": "ok" if stop_ok else "failed",
                "source_revision": revision,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "unit": unit,
            },
        )
        results.append(row)
    return results


def converge_governed_services_inactive(
    *,
    runner: Runner,
    evidence_path: Path,
    actor: str,
    source_revision: str,
) -> list[dict]:
    """Stop and prove every governed seat service inactive before rollback."""

    results = []
    for unit in GOVERNED_SEAT_SERVICES:
        try:
            stopped = runner(
                ["systemctl", "--user", "stop", unit],
                capture_output=True,
                text=True,
                timeout=310,
            )
            stop_ok = getattr(stopped, "returncode", 1) == 0
            detail = str(getattr(stopped, "stderr", "") or "")[-500:]
        except Exception as exc:
            stop_ok = False
            detail = str(exc)[-500:]
        state = _state(unit, runner)
        safe = (
            state.get("_known") == "true"
            and state.get("ActiveState") == "inactive"
            and state.get("LoadState") in {"loaded", "not-found"}
            and state.get("Job") in {"", "0", "n/a"}
        )
        _append(
            evidence_path,
            {
                "actor": actor,
                "requested_state": "inactive",
                "result": "ok" if safe else "failed",
                "result_detail": detail,
                "source_revision": source_revision,
                "stop_result": "ok" if stop_ok else "failed",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "unit": unit,
            },
        )
        results.append({"unit": unit, "safe": safe, "stop_ok": stop_ok})
    return results


def converge_scheduler_transition(
    profile: dict,
    *,
    runner: Runner,
    config_home: Path,
    evidence_path: Path,
    actor: str,
    source_revision: str,
    enable: bool,
    start: bool,
) -> dict:
    """Atomically cut over from forbidden timers to the required scheduler."""

    lock_fd = _acquire_scheduler_migration_lock(config_home)
    if lock_fd is None:
        return {"acquired": False, "forbidden": [], "required": []}
    try:
        forbidden = converge_forbidden_timers(
            profile,
            runner=runner,
            config_home=config_home,
            evidence_path=evidence_path,
            actor=actor,
            source_revision=source_revision,
        )
        if not all(row["safe"] for row in forbidden):
            return {"acquired": True, "forbidden": forbidden, "required": []}
        required = converge_required_timers(
            profile,
            runner=runner,
            config_home=config_home,
            evidence_path=evidence_path,
            actor=actor,
            source_revision=source_revision,
            enable=enable,
            start=start,
        )
        return {"acquired": True, "forbidden": forbidden, "required": required}
    finally:
        _release_scheduler_migration_lock(lock_fd)


def rollback_to_legacy_timers(
    legacy_timers: list[str],
    *,
    home: Path,
    runner: Runner,
    config_home: Path,
    evidence_path: Path,
    actor: str,
) -> dict:
    """Stop the orchestrator fence before restoring legacy seat timers."""

    profile = legacy_timer_rollback_profile(legacy_timers, home=home)
    lock_fd = _acquire_scheduler_migration_lock(config_home)
    if lock_fd is None:
        return {
            "ok": False,
            "lock_acquired": False,
            "forbidden": [],
            "services": [],
            "required": [],
        }
    try:
        forbidden = converge_forbidden_timers(
            profile,
            runner=runner,
            config_home=config_home,
            evidence_path=evidence_path,
            actor=actor,
        )
        if not all(row["safe"] for row in forbidden):
            return {
                "ok": False,
                "lock_acquired": True,
                "forbidden": forbidden,
                "services": [],
                "required": [],
            }
        services = converge_governed_services_inactive(
            runner=runner,
            evidence_path=evidence_path,
            actor=actor,
            source_revision=policy_revision(profile),
        )
        if not all(row["safe"] for row in services):
            return {
                "ok": False,
                "lock_acquired": True,
                "forbidden": forbidden,
                "services": services,
                "required": [],
            }
        required = converge_required_timers(
            profile,
            runner=runner,
            config_home=config_home,
            evidence_path=evidence_path,
            actor=actor,
        )
        return {
            "ok": len(required) == len(required_timers(profile))
            and all(row["converged"] for row in required),
            "lock_acquired": True,
            "forbidden": forbidden,
            "services": services,
            "required": required,
        }
    finally:
        _release_scheduler_migration_lock(lock_fd)


def main() -> int:
    """Run the guarded operational rollback to legacy timers."""

    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("rollback-legacy",))
    parser.add_argument("--legacy-timer", action="append", required=True)
    parser.add_argument("--home", type=Path, default=sovereign_home())
    parser.add_argument("--config-home", type=Path, default=Path("~/.config").expanduser())
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--actor", required=True)
    args = parser.parse_args()
    try:
        result = rollback_to_legacy_timers(
            args.legacy_timer,
            home=args.home,
            runner=subprocess.run,
            config_home=args.config_home,
            evidence_path=args.evidence,
            actor=args.actor,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
