"""Required timer enablement audit and bounded convergence."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

Runner = Callable[..., object]


def policy_revision(profile: dict) -> str:
    """Stable revision for the exact profile policy being applied."""
    encoded = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def required_timers(profile: dict) -> list[str]:
    """Timers mandated by the profile, excluding allowed-only units."""
    required = (profile.get("units") or {}).get("required") or []
    return sorted({unit for unit in required if unit.endswith(".timer")})


def _state(unit: str, runner: Runner) -> dict[str, str]:
    try:
        result = runner(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=LoadState,UnitFileState,ActiveState,SubState,FragmentPath",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return {"LoadState": "unknown", "UnitFileState": "unknown", "ActiveState": "unknown"}
    if getattr(result, "returncode", 1) != 0:
        return {"LoadState": "unknown", "UnitFileState": "unknown", "ActiveState": "unknown"}
    return dict(
        line.split("=", 1)
        for line in str(getattr(result, "stdout", "")).splitlines()
        if "=" in line
    )


def audit_timer(unit: str, *, runner: Runner, config_home: Path) -> dict:
    """Read one timer's runtime and exact wants-link state without mutation."""
    state = _state(unit, runner)
    link = config_home / "systemd" / "user" / "timers.target.wants" / unit
    fragment_text = state.get("FragmentPath") or ""
    fragment = Path(fragment_text) if fragment_text else None
    link_ok = link.is_symlink() and fragment is not None and link.resolve() == fragment.resolve()
    enabled = state.get("UnitFileState") == "enabled" and link_ok
    active = state.get("ActiveState") == "active" and state.get("SubState") == "waiting"
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
) -> list[dict]:
    """Enable and start only profile-required timers, idempotently."""
    revision = source_revision or policy_revision(profile)
    results = []
    for unit in required_timers(profile):
        before = audit_timer(unit, runner=runner, config_home=config_home)
        if not before["loaded"]:
            results.append(before)
            continue
        if not before["enabled"]:
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
        if not current["active_waiting"]:
            try:
                runner(
                    ["systemctl", "--user", "start", unit],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except Exception:
                pass
        results.append(audit_timer(unit, runner=runner, config_home=config_home))
    return results
