#!/usr/bin/env python3
"""Fleet readiness gate.

Before a rollout goes to more than one host, this checks that a node has
everything the dispatcher will need at runtime: every mandatory environment
variable the dispatcher requires, and every module every systemd unit's
ExecStart names, actually importable under the target Python.

Two things went wrong in production that this exists to catch:
  1. A node came up FAILING because main required SKFLEET_GATEWAY_URL and no
     systemd unit supplied it.
  2. skfleet-niobe-shadow.service names a module, skcapstone.seat_shadow_entrypoint,
     that does not exist on main. Nothing caught a unit pointing at a missing
     module.

Python 3.12, standard library only.
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]

# The gateway URL must be a bare origin: scheme plus netloc only. A URL that
# carries a path, query, or fragment (e.g. http://host:18790/v1) passes a
# presence-only check and then silently 404s every health probe at runtime.
# Asserting the shape here turns the 2026-09-18 three-day silent outage into
# a failed readiness gate at startup.
GATEWAY_URL_VAR = "SKFLEET_GATEWAY_URL"
GATEWAY_URL_UNITS = ("skfleet-rotate.service",)


def _parse_environment(raw):
    """Parse a systemd Environment= payload into a name->value dict."""
    env = {}
    for tok in (raw or "").split():
        if "=" not in tok:
            continue
        k, v = tok.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def _check_gateway_url_shape(name, value, lines):
    """Assert `value` is a bare origin. Returns True when OK.

    Appends a FAIL line naming the variable when the value carries a path,
    query, or fragment.
    """
    if value is None or value == "":
        return True
    parts = urlsplit(value)
    problems = []
    if parts.scheme.lower() not in ("http", "https"):
        problems.append("scheme %r" % parts.scheme)
    if not parts.netloc:
        problems.append("missing netloc")
    if parts.path and parts.path != "/":
        problems.append("path %r" % parts.path)
    if parts.query:
        problems.append("query %r" % parts.query)
    if parts.fragment:
        problems.append("fragment %r" % parts.fragment)
    if problems:
        lines.append(
            "FAIL required env: %s value %r is not a bare origin (%s)"
            % (name, value, "; ".join(problems))
        )
        return False
    return True


def _gateway_env_snapshot_for_unit(unit):
    """Get a live unit's effective environment from systemctl (unit file + all
    drop-in fragments), so the shape check runs against what the dispatcher
    actually sees.

    Returns (env_dict, error_string_or_None).
    """
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "show", "-p", "Environment", "--value", unit],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return {}, (proc.stderr.strip() or "systemctl show failed")
        return _parse_environment(proc.stdout), None
    except subprocess.SubprocessError as exc:
        return {}, str(exc)


def _unit_in_scope(unit):
    """Decide whether a role unit (e.g. skfleet-rotate.service) is in scope on
    this host, so a host that simply does not run that role does not get
    falsely flagged for missing env vars the role would have required.

    Returns (in_scope, error, checked_unit):
      in_scope True/False when determined, error None;
      in_scope None when the state could not be determined (treated by the
      caller as a FAIL, never a SKIP), error set to a reason string.

    The state checked is *active* state (systemctl --user is-active), not
    *enabled* state. Measured against the real fleet, enabled-state does not
    distinguish a rotate host from a seat/dispatcher host the way active
    state does: a rotate dispatcher can be installed-but-not-started
    (enabled but not active) on a seat host and would incorrectly pass an
    enabled-state check, while a running rotate host is active whether or
    not it is enabled. Active-state is the correct discriminator.
    """
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", unit],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.SubprocessError as exc:
        return None, str(exc), unit
    if proc.returncode == 0:
        return True, None, unit
    state = (proc.stdout or proc.stderr).strip()
    if state in ("inactive", "activating", "deactivating", "active"):
        # "active" here means is-active exited nonzero, so treat only
        # inactive-family states as "does not apply to this host".
        return False, None, unit
    if state in ("unknown", "failed", ""):
        # unknown typically means the unit is not installed at all on this
        # host; that is a legitimate "not this host's role" signal, not an
        # error.
        return False, None, unit
    # Anything else ("failed", a missing systemctl, etc.) is undetermined.
    return None, f"systemctl --user is-active {unit} reported {state!r}", unit


def main() -> int:
    ap = argparse.ArgumentParser(description="Assert the fleet's systemd units can actually run their ExecStart and that the dispatcher's required env vars are set.")
    ap.add_argument("--python", default=sys.executable, help="Python interpreter to import the ExecStart modules under (default: current one)")
    ap.add_argument("--units-dir", default=str(REPO_ROOT / "units"), help="directory of .service/.timer files (default: <repo>/units)")
    ap.add_argument("--rotate-script", default=str(REPO_ROOT / "scripts" / "fleet" / "skfleet_rotate.py"), help="path to skfleet_rotate.py")
    ap.add_argument("--gateway-units", nargs="*", default=list(GATEWAY_URL_UNITS), help="unit names whose SKFLEET_GATEWAY_URL value must be a bare origin")
    ap.add_argument("--require-healthy-gateway", action="store_true", help="also fail if the gateway does not answer GET /health")
    ap.add_argument("--verdict-out", default=None, help="optionally write a small JSON verdict file (ready? + per-var status list)")
    args = ap.parse_args()

    ok = True
    lines = []

    # --- Part 1: every unit's ExecStart module is importable.
    try:
        import skcapstone.fleet.lane_health  # noqa: F401
        import skcapstone.fleet.skfleet_rotate  # noqa: F401
        import skcapstone.seat_entrypoint  # noqa: F401
    except Exception as exc:
        ok = False
        lines.append(f"FAIL modules: {exc}")

    if ok:
        # --- Part 2: dispatcher required_env, via in-process import.
        rotate_path = Path(args.rotate_script)
        if not rotate_path.is_file():
            ok = False
            lines.append(f"FAIL dispatcher source: {rotate_path} not found")
        else:
            spec = importlib.util.spec_from_file_location("_skfleet_rotate_check", str(rotate_path))
            if spec is None or spec.loader is None:
                ok = False
                lines.append(f"FAIL dispatcher source: could not build import spec for {rotate_path}")
            else:
                mod = importlib.util.module_from_spec(spec)
                try:
                    spec.loader.exec_module(mod)
                    mandatory = set(mod.required_env())
                    for name in sorted(mandatory):
                        val = os.environ.get(name, "")
                        if val != "":
                            if _check_gateway_url_shape(name, val, lines):
                                lines.append(f"OK required env: {name}")
                            else:
                                ok = False
                        elif name == GATEWAY_URL_VAR and args.gateway_units:
                            # For the gateway URL specifically, the value may
                            # live only in a drop-in, so also check the live
                            # effective environment of each unit in scope.
                            for unit in args.gateway_units:
                                in_scope, scope_error, checked_unit = _unit_in_scope(unit)
                                if scope_error is not None:
                                    ok = False
                                    lines.append(
                                        "FAIL required env: could not determine whether %s applies to this host "
                                        "(checked %s: %s); an undetermined scope is never treated as skippable"
                                        % (unit, checked_unit, scope_error)
                                    )
                                elif in_scope:
                                    env_snap, snap_error = _gateway_env_snapshot_for_unit(unit)
                                    if snap_error is not None:
                                        ok = False
                                        lines.append(
                                            "FAIL required env: %s value could not be determined for %s (%s); "
                                            "an undetermined environment is never treated as ready"
                                            % (GATEWAY_URL_VAR, unit, snap_error)
                                        )
                                    else:
                                        snap_val = env_snap.get(GATEWAY_URL_VAR, "")
                                        if snap_val != "" and not _check_gateway_url_shape(GATEWAY_URL_VAR, snap_val, lines):
                                            ok = False
                                        elif snap_val != "":
                                            lines.append(
                                                "OK required env: %s is a bare origin on %s (%s)"
                                                % (GATEWAY_URL_VAR, unit, snap_val)
                                            )
                        else:
                            ok = False
                            lines.append(f"FAIL required env: {name} is missing")

    if args.require_healthy_gateway:
        from urllib.request import urlopen, Request

        gateway_url = os.environ.get("SKFLEET_GATEWAY_URL", "").rstrip("/")
        if not gateway_url:
            lines.append("FAIL gateway health: SKFLEET_GATEWAY_URL not set; cannot probe /health")
            ok = False
        else:
            health_url = gateway_url + "/health"
            try:
                with urlopen(Request(health_url), timeout=5) as resp:
                    body = resp.read().decode("utf-8", "replace")
                    if resp.status == 200:
                        lines.append(f"OK gateway health: 200 {health_url}")
                    else:
                        lines.append(f"FAIL gateway health: {resp.status} {health_url}")
                        ok = False
            except Exception as exc:
                lines.append(f"FAIL gateway health: {exc} at {health_url}")
                ok = False

    if args.verdict_out:
        verdict = {
            "ready": ok,
            "lines": lines,
            "ts": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
        }
        Path(args.verdict_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.verdict_out).write_text(json.dumps(verdict, indent=2) + "\n", encoding="utf-8")

    print("\n".join(lines))
    print("READY" if ok else "NOT READY")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
