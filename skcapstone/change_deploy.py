"""Change-deploy runner: the scheduled job that arms the DEPLOY edge for ITIL
changes (CM P3.1, card 6922f5cf).

Phase 3a of the change-management design
(docs/specs/2026-08-13-change-management-cab-ai-arch.md sections 5.2/5.3).
Every 60s the ``change-deploy-runner`` job (a ``jobs.d/`` drop-in, pinned to
one node) looks for ITIL changes whose status is ``scheduled``:

- window arrived (``window_start <= now <= window_end``): attempt the deploy
  pipeline through the ``set_deploy_dispatcher`` seam.
- window missed (``now > window_end`` without a deploy): append the
  ``window_missed`` event, which the fold sends back to ``approved`` (never a
  late fire, re-schedule required).

This card is PLAN-ONLY. The real merge/deploy executor is P3.2, a separate
skharness bridge (``skharness.autocode.change_deploy_bridge``) that wires
itself in behind ``SKAI_DEPLOY_BRIDGE=1``. With the seam unwired (the default,
and the only state this card ships with, since that bridge module does not
exist yet), every due change gets a "would-deploy" plan appended as a `note`
event on the change record and nothing executes: no merge, no push, no repo
mutation of any kind. This mirrors exactly how the existing ai-runner
(`agent_run.py`, R1) shipped canary-first: see `set_execute_dispatcher` /
`_maybe_wire_execute_bridge` there for the pattern this module deliberately
copies.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skcapstone.change_deploy")

_HOST = socket.gethostname()

# Default per-change claim window (CM P3.1 single-fire safety). Mirrors
# agent_run.claim_run's default lease_seconds=900: within this window, a
# second attempt to process the same change (an overlapping tick, a second
# runner process racing on the same node, a misfire) is refused outright
# rather than recording a second would-deploy plan or a second dispatch
# attempt. After it expires, the change (still `scheduled`, since nothing in
# this card ever moves it out of that status) is naturally reconsidered on a
# later tick.
DEFAULT_LEASE_SECONDS = 900

WORKER = "change-deploy-runner"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(value) -> Optional[datetime]:
    """Parse an ISO 8601 timestamp, tolerant of a trailing ``Z``.

    Returns ``None`` on anything unparseable (missing field, garbage string),
    which callers treat as fail-closed: an unfoldable window is skipped, not
    guessed at.
    """
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Scheduled-change scan
# ---------------------------------------------------------------------------


def _itil_manager(home: Path):
    from .itil import ITILManager

    return ITILManager(Path(home).expanduser())


def list_due(home: Path, now: Optional[datetime] = None) -> list[dict]:
    """Scheduled changes whose deploy window has arrived.

    ``window_start <= now <= window_end``. A change with no (or unparseable)
    ``scheduled_window`` is skipped, fail-closed: an unfoldable record is
    never treated as due.
    """
    now = now or _now()
    mgr = _itil_manager(home)
    due = []
    for chg in mgr.list_changes(status="scheduled"):
        window = chg.scheduled_window or {}
        start = _parse_iso(window.get("window_start"))
        end = _parse_iso(window.get("window_end"))
        if start is None or end is None:
            logger.info(
                "change-deploy-runner: %s is 'scheduled' but has no parseable "
                "window; skipping (fail-closed)",
                chg.id,
            )
            continue
        if start <= now <= end:
            due.append({"change": chg, "window": window})
    return due


def list_missed(home: Path, now: Optional[datetime] = None) -> list[dict]:
    """Scheduled changes whose window has closed without a deploy.

    ``now > window_end``. Same fail-closed skip on an unparseable window.
    """
    now = now or _now()
    mgr = _itil_manager(home)
    missed = []
    for chg in mgr.list_changes(status="scheduled"):
        window = chg.scheduled_window or {}
        end = _parse_iso(window.get("window_end"))
        if end is None:
            continue
        if now > end:
            missed.append({"change": chg, "window": window})
    return missed


# ---------------------------------------------------------------------------
# Single-fire lease (CM P3.1 concurrency safety)
# ---------------------------------------------------------------------------


def _lease_path(mgr, rid: str) -> Path:
    return mgr.changes_dir / rid / "deploy-lease.json"


def claim_deploy_lease(
    home: Path, change_id: str, worker: str = WORKER, lease_seconds: float = DEFAULT_LEASE_SECONDS
) -> bool:
    """Claim a per-change deploy lease.

    Returns ``True`` if the lease was claimed (no active lease existed, or the
    prior one expired), ``False`` if an active lease already blocks this
    change. Unlike ``agent_run.claim_run`` (which just appends a claim event
    and relies on its AgentRun state field to gate re-processing), a
    scheduled change has no such state machine to lean on here: it stays
    `scheduled` across ticks in plan-only canary mode, so the lease file
    itself is the single-fire guard, checked before claiming. A corrupt or
    unreadable lease file is treated as expired (safe to reclaim): this
    mechanism exists to prevent duplicate plan/dispatch attempts, not to hold
    a change hostage to a damaged lease file.

    Single-node pinning (the job's `nodes: [noroc2027]`) is the primary
    concurrency guard; this lease is the second layer for overlapping ticks
    on that one node (a tick that overruns 60s, a manual re-run while the
    scheduled tick is also in flight, etc).
    """
    mgr = _itil_manager(home)
    rid = mgr._resolve_id(mgr.changes_dir, change_id)
    lease_path = _lease_path(mgr, rid)
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    now = _now()
    with open(lease_path, "a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.seek(0)
            raw = fh.read().strip()
            if raw:
                try:
                    existing = json.loads(raw)
                    expires_at = _parse_iso(existing.get("expires"))
                except (json.JSONDecodeError, ValueError, TypeError):
                    expires_at = None
                if expires_at is not None and expires_at > now:
                    return False
            lease = {
                "worker": f"{worker}@{_HOST}",
                "change_id": change_id,
                "claimed_at": _iso(now),
                "expires": _iso(now + timedelta(seconds=lease_seconds)),
            }
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps(lease))
            fh.flush()
            return True
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Deploy dispatch seam (R1-mirror, card 6922f5cf)
# ---------------------------------------------------------------------------
#
# Deploy is NOT the ai-runner's sandboxed execute bridge and never will be:
# that bridge's no-merge property is structural (design doc section 5.2). A
# real deploy (merge + repo mutation) needs its own, separately-gated
# executor: skharness.autocode.change_deploy_bridge (P3.2, a later card).
# Until that module exists AND is explicitly enabled, deploy is FAIL-CLOSED:
# every due change gets a would-deploy plan recorded, never a dispatch.

_deploy_dispatcher = None  # Optional[Callable[[dict], dict]]


def set_deploy_dispatcher(fn) -> None:
    """Wire (or clear, with ``None``) the deploy pipeline dispatcher.

    ``fn(context) -> {"summary", "activity", "links"}``, the same shape
    ``agent_run.set_execute_dispatcher`` uses. Default ``None`` keeps deploy
    fail-closed: see ``_dispatch_deploy`` below, the one and only call site.
    """
    global _deploy_dispatcher
    _deploy_dispatcher = fn


def deploy_dispatch_available() -> bool:
    """True when a deploy pipeline dispatcher has been wired."""
    return _deploy_dispatcher is not None


def build_deploy_dispatcher():
    """Build the deploy dispatcher iff the skharness bridge is installed.

    Returns ``None`` (a first-class fail-closed outcome, not an error) when
    the bridge module is not importable, or when the bridge itself refuses to
    build one (missing prerequisites). This function does no env-var
    gating itself; ``_maybe_wire_deploy_bridge`` is the only caller and it is
    the one that checks ``SKAI_DEPLOY_BRIDGE``.
    """
    try:
        from skharness.autocode.change_deploy_bridge import (
            build_deploy_dispatcher as _build,
        )
    except ImportError:
        logger.info(
            "skharness.autocode.change_deploy_bridge is not installed; "
            "deploy stays fail-closed (plan-only)"
        )
        return None
    return _build()


def _maybe_wire_deploy_bridge() -> None:
    """Wire the skharness deploy bridge iff explicitly enabled and buildable.

    Inert by default; every failure path leaves deploy fail-closed. Mirrors
    ``agent_run._maybe_wire_execute_bridge`` exactly.
    """
    if os.environ.get("SKAI_DEPLOY_BRIDGE") != "1":
        return
    if deploy_dispatch_available():
        return
    fn = build_deploy_dispatcher()
    if fn is None:
        logger.info("deploy bridge prerequisites missing; deploy stays fail-closed (plan-only)")
        return
    set_deploy_dispatcher(fn)


def _dispatch_deploy(context: dict) -> dict:
    """The one and only call site for the deploy dispatcher.

    A raw/unwired dispatch is structurally refused here: when
    ``_deploy_dispatcher`` is ``None`` (the default, and the only state this
    card ships with), this function never calls anything and returns a
    ``refused`` result instead. There is no fallback "raw" dispatcher for
    deploy the way ``claude_dispatcher`` is agent_run's raw fallback for
    propose/dry-run: deploy has exactly one path, the wired bridge, or
    nothing at all.
    """
    if _deploy_dispatcher is None:
        return {
            "refused": True,
            "reason": "deploy dispatcher not wired (SKAI_DEPLOY_BRIDGE unset, or the "
            "skharness bridge is unavailable/refused its prerequisites); plan-only",
        }
    try:
        return _deploy_dispatcher(context)
    except Exception as exc:  # noqa: BLE001 - a dispatcher failure must never crash the tick
        logger.warning(
            "change-deploy-runner: dispatcher raised for %s: %s", context.get("change_id"), exc
        )
        return {"refused": True, "error": str(exc)}


# ---------------------------------------------------------------------------
# Per-change processing
# ---------------------------------------------------------------------------


def _record_would_deploy(mgr, rid: str, chg, window: dict, worker: str) -> None:
    """Append a `note` event describing what would run: the plan-only canary
    record. Uses the change record's pre-existing `note` event kind (already
    folded into `timeline` by `_fold_change`) rather than inventing a new
    event kind, so no change to skcoord.itil's fold is required for this
    card - the change record is exactly the "card" the design doc means when
    it says a would-deploy plan is "recorded on the change card" (section
    5.3): risk 5 of the design doc is explicit that the deploy executor reads
    only the change record, never the kanban card's `agent_run` meta.
    """
    prepared = chg.prepared_pr or {}
    validation = chg.validation or {}
    plan = (
        "would-deploy plan (canary, SKAI_DEPLOY_BRIDGE unset): window "
        f"{window.get('window_start')} to {window.get('window_end')}, "
        f"deploy_mode={window.get('deploy_mode', 'confirm')}, "
        f"prepared_pr={prepared.get('url')}, "
        f"validation_passed={validation.get('passed')}. "
        "Nothing executed: no merge, no push, no repo mutation."
    )
    mgr._append_event(mgr.changes_dir, rid, worker, "note", note=plan)


def _record_dispatch_result(mgr, rid: str, worker: str, result: dict) -> None:
    """Append a `note` event summarizing a wired dispatcher's result.

    Only reachable once P3.2 wires ``set_deploy_dispatcher`` (never in this
    card, since the bridge module it needs does not exist yet). Recording is
    intentionally the only thing this function does: any merge/status
    transition the deploy pipeline performs is owned entirely by the wired
    bridge, not by this runner.
    """
    summary = result.get("summary") or "deploy dispatcher returned no summary"
    mgr._append_event(mgr.changes_dir, rid, worker, "note", note=f"deploy dispatched: {summary}")


def process_due_change(home: Path, chg, window: dict, worker: str = WORKER) -> dict:
    """Process one change whose deploy window has arrived.

    Claims the per-change lease first; a lease miss (already claimed by an
    overlapping tick) is a clean skip, not an error. With the seam unwired
    (the default), records a would-deploy plan and dispatches nothing.
    """
    mgr = _itil_manager(home)
    rid = mgr._resolve_id(mgr.changes_dir, chg.id)

    if not claim_deploy_lease(home, chg.id, worker=worker):
        logger.info("change-deploy-runner: %s already leased this tick, skipping", chg.id)
        return {"change_id": chg.id, "action": "skipped", "reason": "active lease"}

    context = {
        "change_id": chg.id,
        "title": chg.title,
        "window": window,
        "prepared_pr": chg.prepared_pr,
        "validation": chg.validation,
    }
    result = _dispatch_deploy(context)
    if result.get("refused") or not deploy_dispatch_available():
        _record_would_deploy(mgr, rid, chg, window, worker)
        return {"change_id": chg.id, "action": "would-deploy", "dispatched": False}

    _record_dispatch_result(mgr, rid, worker, result)
    return {"change_id": chg.id, "action": "dispatched", "dispatched": True}


def process_missed_change(home: Path, chg, window: dict, worker: str = WORKER) -> dict:
    """Process one change whose deploy window closed without a deploy.

    Appends `window_missed`, which the fold sends back to `approved`
    (fail-closed: never a late fire; an explicit re-schedule is required).
    """
    mgr = _itil_manager(home)
    rid = mgr._resolve_id(mgr.changes_dir, chg.id)
    mgr._append_event(
        mgr.changes_dir,
        rid,
        worker,
        "window_missed",
        note=f"window closed at {window.get('window_end')} without a deploy",
    )
    return {"change_id": chg.id, "action": "window_missed"}


# ---------------------------------------------------------------------------
# The runner tick
# ---------------------------------------------------------------------------


def run_change_deploy_tick(
    home: Path, worker: str = WORKER, now: Optional[datetime] = None
) -> list[dict]:
    """Process one scheduler tick: missed windows first, then due changes.

    Fail-closed per item: any failure while processing one change is caught,
    logged, and skipped rather than raised - one bad record must never stop
    the tick from handling the rest, and must never be treated as a deploy.
    """
    now = now or _now()
    _maybe_wire_deploy_bridge()
    results: list[dict] = []

    for item in list_missed(home, now=now):
        chg = item["change"]
        try:
            results.append(process_missed_change(home, chg, item["window"], worker=worker))
        except Exception as exc:  # noqa: BLE001 - one bad record must not stop the tick
            logger.warning("change-deploy-runner: window_missed failed for %s: %s", chg.id, exc)

    for item in list_due(home, now=now):
        chg = item["change"]
        try:
            results.append(process_due_change(home, chg, item["window"], worker=worker))
        except Exception as exc:  # noqa: BLE001 - one bad record must not stop the tick
            logger.warning("change-deploy-runner: processing failed for %s: %s", chg.id, exc)

    return results


def run_change_deploy_job() -> None:
    """Zero-arg entrypoint for the ``change-deploy-runner`` jobs.d job.

    One tick: scan `scheduled` changes, handle missed windows, and record a
    would-deploy plan for each due change (dispatch stays fail-closed until
    `SKAI_DEPLOY_BRIDGE=1` AND the skharness bridge is installed AND its own
    prerequisites hold - see `_maybe_wire_deploy_bridge`).
    """
    from . import SHARED_ROOT

    home = Path(SHARED_ROOT).expanduser()
    results = run_change_deploy_tick(home)
    if results:
        logger.info(
            "change-deploy-runner processed %d change(s): %s",
            len(results),
            [r.get("action") for r in results],
        )
