"""Dashboard Fleet Drift view: install-profile drift, per node, fleet wide.

Epic 3bbf39ea, card d1c6d605. This module is the dashboard's surface over
``skfleet node doctor --all``: it reads each node's published
``status.inventory`` from the files-as-API fleet tree and grades it against
that node's install profile. No ssh, no host access, and no actuation verb
anywhere in this file.

The grading itself is NOT implemented here. ``skcapstone.fleet.cli._doctor_one``
is the fleet's one grader and this panel calls it directly, private name and
all. That coupling is deliberate: a second copy of "who gets graded and how"
would eventually disagree with the CLI, and an operator comparing the panel
against ``skfleet node doctor`` would have no way to tell which one lied.

Severity grading is inherited from ``profile_doctor`` and must not be
flattened by the presentation layer:

    forbidden        error  the node is doing something it was told not to
    missing_required warn   the node has not finished becoming what it is
    unexpected       info   the manifest has not caught up with reality

Only the error grade alerts. Grading the noisy categories as errors is how a
signal turns into wallpaper everyone learns to ignore.

A node with no role, or one that has published no inventory, renders as an
explicit SKIPPED state carrying its reason. It is neither clean nor drifted:
an absent inventory is not an observation of an empty machine, and treating
it as one grades a healthy node as missing everything.

No em/en dashes anywhere (SKWorld hard rule).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

logger = logging.getLogger("skdashboard.fleet")

#: Grades in descending severity, the order the panel renders groups in.
GRADE_ORDER = ("error", "warn", "info")

#: Node severities in descending order, worst node first in the payload.
SEVERITY_ORDER = ("error", "warn", "info", "ok")

#: Minimum seconds between two sk-alert fires from this panel. Mirrors
#: skcapstone.fleet.events.DEDUPE_WINDOW_S, which is the rate cap every other
#: fleet alert already inherits by being gated on events.emit().
ALERT_MIN_INTERVAL_S = 300.0

#: How many findings the alert text names before it summarizes the rest. The
#: alert is a pointer to the panel, not a replacement for reading it.
ALERT_MAX_NAMED = 5


def default_state_path(home: Path) -> Path:
    """Where the edge-trigger state for this panel's alerts lives."""
    return Path(home).expanduser() / "dashboard" / "fleet-drift-alert.json"


def _skip_code(paths, view, inventory, profile_for) -> str:
    """Machine-readable reason a node could not be graded.

    Mirrors ``_doctor_one``'s check order exactly, because the human-readable
    note the panel displays comes from there: a code that disagreed with the
    text beside it would be worse than no code at all.
    """
    if not view.role:
        return "no_role"
    if profile_for(paths, view.role) is None:
        return "no_profile"
    if inventory is None:
        return "no_inventory"
    return "ungraded"


def _counts(findings: list[dict]) -> dict:
    """Findings per grade for one node, every grade always present."""
    out = {grade: 0 for grade in GRADE_ORDER}
    for finding in findings:
        if finding["grade"] in out:
            out[finding["grade"]] += 1
    return out


def collect_drift(paths=None) -> dict:
    """Grade every node in the fleet tree against its install profile.

    Args:
        paths: A ``FleetPaths``, or None for the live tree (SKFLEET_ROOT).

    Returns:
        dict with ``nodes`` (graded, worst first), ``skipped`` (ungradeable,
        each with a reason), ``summary`` counts, and ``errors``. On any
        failure the shape is still well formed and ``errors`` explains why,
        because this panel must never 500 the page.
    """
    payload: dict = {
        "nodes": [],
        "skipped": [],
        "summary": {"graded": 0, "skipped": 0, "error": 0, "warn": 0, "info": 0, "ok": 0},
        "errors": [],
    }
    try:
        from skcapstone.fleet import node_controller, store
        from skcapstone.fleet.cli import _doctor_one, _profile_for
        from skcapstone.fleet.paths import default_paths
    except ImportError as exc:
        payload["errors"].append(f"fleet profile doctor unavailable: {exc}")
        return payload

    if paths is None:
        paths = default_paths()

    try:
        views = node_controller.node_views(paths)
    except Exception as exc:  # noqa: BLE001 -- never 500 the panel
        payload["errors"].append(f"node views failed: {exc}")
        return payload

    for view in views:
        try:
            status = (store.read_node_file(paths, view.name, "node.json") or {}).get("status", {})
            # `.get("inventory")` would collapse absent into empty, which is
            # the one verdict this whole path exists to never produce.
            published = status["inventory"] if "inventory" in status else None
            report, note = _doctor_one(paths, view.name, published)
        except Exception as exc:  # noqa: BLE001 -- one bad node is not a dead panel
            payload["errors"].append(f"{view.name}: {exc}")
            continue

        if report is None:
            payload["skipped"].append(
                {
                    "node": view.name,
                    "role": view.role or "",
                    "reason_code": _skip_code(paths, view, published, _profile_for),
                    "reason": note,
                }
            )
            continue

        report["counts"] = _counts(report["findings"])
        payload["nodes"].append(report)

    payload["nodes"].sort(key=lambda n: (SEVERITY_ORDER.index(n["severity"]), n["node"]))
    payload["skipped"].sort(key=lambda n: n["node"])
    summary = payload["summary"]
    summary["graded"] = len(payload["nodes"])
    summary["skipped"] = len(payload["skipped"])
    for node in payload["nodes"]:
        summary[node["severity"]] += 1
    return payload


def error_fingerprint(payload: dict) -> list[str]:
    """Sorted ``node/category/name`` ids of every error-grade finding.

    Identity, not a count: two nodes swapping which forbidden unit they run
    is a new condition an operator has not seen, and must re-alert.
    """
    return sorted(
        f"{node['node']}/{finding['category']}/{finding['name']}"
        for node in payload.get("nodes", [])
        for finding in node.get("findings", [])
        if finding["grade"] == "error"
    )


def alert_message(payload: dict, fingerprint: list[str]) -> str:
    """One line of sk-alert text: how many, on how many nodes, and which."""
    nodes = sorted({item.split("/", 1)[0] for item in fingerprint})
    named = ", ".join(fingerprint[:ALERT_MAX_NAMED])
    rest = len(fingerprint) - ALERT_MAX_NAMED
    if rest > 0:
        named += f" (+{rest} more)"
    return (
        f"fleet drift: {len(fingerprint)} forbidden finding(s) on "
        f"{len(nodes)} node(s): {named}"
    )


def _read_state(state_path: Path) -> dict:
    try:
        return json.loads(Path(state_path).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 -- missing or corrupt state means "no edge seen yet"
        return {}


def _write_state(state_path: Path, state: dict) -> None:
    """Replace the state file atomically; a torn read must never re-alert."""
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".fleet-drift-alert.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, sort_keys=True)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 -- a failed state write must not break the panel
        Path(tmp).unlink(missing_ok=True)
        raise


def _default_send(message: str) -> bool:
    """Fire one sk-alert through the fleet's own alert primitive.

    Reuses ``skcapstone.fleet.alerts.send_alert`` rather than shelling out
    here: it already resolves the binary through ``shutil.which`` with an
    absolute fallback and passes the message as an argv element, which is the
    exact pair of mistakes that has left fleet alert paths silently dead.

    The level is ``warn`` because sk-alert only understands info/warn/crit.
    Passing the finding's own grade string ("error") would not raise, it would
    quietly render as the info bell, which reads as less urgent than a warn.
    """
    try:
        from skcapstone.fleet.alerts import send_alert
    except ImportError as exc:
        logger.warning("sk-alert path unavailable: %s", exc)
        return False
    return send_alert(message, level="warn")


def maybe_alert(
    payload: dict,
    *,
    state_path: Path,
    now: float | None = None,
    min_interval_s: float = ALERT_MIN_INTERVAL_S,
    send=None,
) -> dict:
    """Fire at most one sk-alert for the error-grade findings in ``payload``.

    Edge triggered on the fingerprint and rate limited on top of it, so the
    poll rate of the page never becomes the alert rate.

    Args:
        payload: A ``collect_drift`` result.
        state_path: Where the last alerted fingerprint is remembered, so a
            dashboard restart does not re-announce a condition Chef has
            already seen.
        now: Unix time, for tests.
        min_interval_s: Floor between two fires.
        send: ``(message) -> bool`` sender, for tests. Defaults to sk-alert.

    Returns:
        dict with ``fired``, a ``reason`` naming which gate decided, the
        ``fingerprint`` considered, and ``message`` when one was sent.
    """
    send = send or _default_send
    now = time.time() if now is None else now
    fingerprint = error_fingerprint(payload)
    state = _read_state(state_path)
    last_fingerprint = state.get("fingerprint")
    last_fire = float(state.get("last_fire") or 0.0)
    result = {"fired": False, "reason": "", "fingerprint": fingerprint, "message": ""}

    if fingerprint == last_fingerprint:
        result["reason"] = "unchanged"
        return result

    if not fingerprint:
        # Recovery is not an alert, but it IS an edge worth recording: without
        # this the same drift returning later would look unchanged and stay
        # silent forever. last_fire is left alone so a recurrence one second
        # later still fires.
        state["fingerprint"] = fingerprint
        _write_state(state_path, state)
        result["reason"] = "cleared"
        return result

    elapsed = now - last_fire
    if elapsed < min_interval_s:
        # The fingerprint is deliberately NOT recorded here. A suppressed edge
        # is deferred to the next poll after the cooldown, never swallowed.
        result["reason"] = "rate_limited"
        result["retry_in_s"] = round(min_interval_s - elapsed, 1)
        return result

    message = alert_message(payload, fingerprint)
    ok = bool(send(message))
    # last_fire advances even on failure, so a broken sk-alert is retried once
    # per cooldown instead of on every single poll. The fingerprint only
    # advances on success, so a dropped alert is still owed to the operator.
    state["last_fire"] = now
    if ok:
        state["fingerprint"] = fingerprint
    try:
        _write_state(state_path, state)
    except Exception as exc:  # noqa: BLE001 -- alerting is not worth a 500
        logger.warning("fleet drift alert state write failed: %s", exc)
    result["fired"] = ok
    result["reason"] = "fired" if ok else "send_failed"
    result["message"] = message
    return result


def get_drift(home: Path, *, paths=None, alert: bool = True, send=None, now=None) -> dict:
    """Assemble the Fleet Drift view for ``GET /api/fleet/drift``.

    Args:
        home: Agent home directory, which is where the alert edge state lives.
        paths: A ``FleetPaths``, or None for the live tree.
        alert: False to compute the report with no alerting side effect.
        send: Sender override, passed through to ``maybe_alert``.
        now: Unix time override, passed through to ``maybe_alert``.

    Returns:
        The ``collect_drift`` payload plus an ``alert`` key describing what
        the alert gate decided this poll (None when ``alert`` is False).
    """
    payload = collect_drift(paths)
    payload["alert"] = None
    if alert:
        try:
            payload["alert"] = maybe_alert(
                payload, state_path=default_state_path(home), now=now, send=send
            )
        except Exception as exc:  # noqa: BLE001 -- reporting outranks alerting
            payload["errors"].append(f"drift alert failed: {exc}")
    return payload
