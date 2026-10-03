"""CronController (Phase 4 CronController, step 2): read-time CronJob rows.

Runs on the control-plane node, mirroring ServiceController's read-time
conventions. Read-time only: never writes status (sknoded-owned) and never
edits spec (operator-owned). skscheduler wiring for CronJob placement is a
later operational card (see cron.py); until then a CronJob's node is
whatever placement record (if any) already exists for it.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import store
from .conditions import _cond
from .cron import CronJobSpecError, is_missed, next_run, normalize_cronjob_spec
from .paths import FleetPaths


@dataclass(frozen=True)
class CronRow:
    """One row of skfleet get cronjobs (read-time derivation, nothing persisted)."""

    name: str
    node: str | None
    schedule: str
    enabled: bool
    last_run: str | None
    next_run: str | None
    missed: bool


def _status_for(merged: dict, target: str | None) -> dict | None:
    for st in merged.get("statuses", []):
        if target is None or st.get("node") == target:
            return st
    return None


def cron_rows(paths: FleetPaths, now_iso: str) -> list[CronRow]:
    """All CronJobs with placement, observed lastRun, and MissedRun derivation.

    The baseline for next_run/missed math is status.lastRun when a run has
    been observed; otherwise the spec's own updatedAt, which is its
    first-seen timestamp for a spec that has never been re-applied, so a
    freshly created CronJob is not immediately flagged as missed.
    """
    rows: list[CronRow] = []
    for payload in store.list_specs(paths, "cronjob"):
        name = payload["name"]
        if payload.get("spec", {}).get("deleted"):
            continue
        try:
            spec = normalize_cronjob_spec(payload.get("spec", {}))
        except CronJobSpecError:
            continue
        merged = store.merged(paths, "cronjob", name) or {}
        placement = merged.get("placement")
        target = placement.get("node") if placement else None
        status = _status_for(merged, target)
        last_run = (status or {}).get("status", {}).get("lastRun")
        baseline = last_run or payload.get("updatedAt")
        schedule = spec["schedule"]
        rows.append(
            CronRow(
                name=name,
                node=target,
                schedule=schedule,
                enabled=bool(spec["enabled"]),
                last_run=last_run,
                next_run=next_run(schedule, baseline),
                missed=is_missed(schedule, baseline, now_iso),
            )
        )
    return rows


def cronjob_conditions(spec: dict, status: dict, now_iso: str) -> list[dict]:
    """Derive the single MissedRun condition for one CronJob.

    Args:
        spec: A normalized CronJob spec (at least 'schedule').
        status: The observed status dict; 'lastRun' when a run happened.
        now_iso: Current time, ISO8601 UTC.
    """
    last_run = status.get("lastRun")
    baseline = last_run or now_iso
    missed = is_missed(spec["schedule"], baseline, now_iso)
    message = f"schedule {spec['schedule']} last ran {last_run or 'never'}"
    return [_cond("MissedRun", missed, "ScheduleCheck", message, now_iso)]
