"""ATLAS Soak: the Phase 3 dual-read instrument (docs/OPERATOR_PLANE_MIGRATION.md).

Phase 3 of the operator-plane migration asks every app to clear the same gate
before its old lane is demoted:

    register endpoint (v2 spec) -> dual-read for one week (endpoint
    authoritative, old lane advisory) -> zero LaneConflict and zero
    Unknown-regression -> demote old lane

Nobody could run that soak: there was no instrument recording both lanes over
time and answering the two gate questions. This module is that instrument.
It does not replace :mod:`skcapstone.operator_seat.eyes` -- it is built
entirely ON TOP of it, exactly as card 90b5b277's brief demands ("a third
independent reading is exactly the disease this epic is curing"):

  * :func:`record` calls :func:`eyes.assess` for the one read-only pass, then
    reduces it to a compact soak sample and appends it to an append-only,
    day-and-node-partitioned artifact under ``<fleet-root>/atlas/soak/``.
  * :func:`report` reads back a window of those samples and answers the two
    gate questions -- **LaneConflict count** and **Unknown-regression
    count** -- per app, plus a readiness verdict.

Two lanes, same names Phase 5 already gives them
--------------------------------------------------
Per the migration doc's own language ("Seat built-in adapters: advisory
through each app's dual-read window, then DELETED"), the two lanes this
module compares are:

  * **endpoint lane** -- derived from eyes' ``cli_lane``, which (per Phase 2's
    v2 precedence in ``eyes.resolve_cli_lane``) IS the endpoint reading once
    an app declares one. This is the NEW, would-be-authoritative lane.
  * **old lane** -- eyes' ``seat_lane``, the in-process built-in adapter.
    This is the lane Phase 5 explicitly names ADVISORY during dual-read and
    slated for deletion after two clean weeks.

Eyes' per-app dict does not expose the raw ``spec.endpoint`` field, only the
resolved ``cli_lane`` state, which collapses "no endpoint declared" and
"endpoint declared but this pass's cli-local fallback also failed" into
overlapping state names for a v1 spec. Rather than growing eyes' return
shape (a shared, actively-edited module -- see the card's note that a third
independent reading is the disease, not a reason to also grow surface area
on the one true probe), this module makes ONE extra read-only
``store.list_specs`` call -- the exact same call ``eyes.assess`` itself
already makes internally -- to read back ``spec.endpoint`` per app. That is
metadata, not a probe: no subprocess, no adapter thread, no timeout, no
retry. See :func:`_endpoint_registrations`.

Unknown is still first-class: three flavours, never collapsed
-----------------------------------------------------------------
  * ``no-endpoint-registered`` -- the app's spec does not declare
    ``endpoint`` at all. This is EVERY app's state today (card 90b5b277
    Phase 3 dual-read has not begun for anyone yet).
  * ``endpoint-unreachable`` -- an endpoint IS declared, but this pass could
    not get a reading from it (today: always ``endpoint-pending``, because
    the signed transport client is Phase 3+ work per
    ``eyes.resolve_cli_lane``'s own docstring; later: ``timeout`` /
    ``cli-error`` / ``unparseable`` once that client exists).
  * a per-condition ``Unknown`` INSIDE an ``ok`` endpoint reading -- the
    endpoint answered, but is honestly unsure about this one condition. This
    is the flavour that feeds :func:`_unknown_regressions`.

Read-only, freeze-independent, bounded
-----------------------------------------
:func:`record` never calls ``act``, never writes to ``fleet/objects/``,
never touches the freeze file, and never enables the HTTP surface -- it only
reads (via ``eyes.assess`` and one ``store.list_specs`` call) and appends one
JSON line to its own artifact directory. Because ``eyes.assess`` is
freeze-independent by construction, so is this. Growth is bounded by daily,
per-node file partitioning plus retention pruning (:data:`DEFAULT_RETENTION_DAYS`,
default 21 days -- enough to cover the ratified one-week dual-read window,
Phase 5's two-clean-weeks adapter-deletion gate, and a few days of slack for
whoever is reading the report) run automatically at the end of every
:func:`record` call.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..fleet import store
from ..fleet.paths import FleetPaths, self_node_name
from . import eyes

SCHEMA = "skoperator.soak/v1"

#: Default retention window for soak sample files, in days. Generous relative
#: to the ratified one-week dual-read gate and Phase 5's two-clean-weeks
#: adapter-deletion gate, so a report can always look back far enough to
#: answer either question; old files past this age are pruned on every
#: `record()` call so the artifact never grows without bound.
DEFAULT_RETENTION_DAYS = 21

#: Gate defaults for "ready to demote" (see `_verdict`). The span requirement
#: is the literal ratified text ("dual-read for one week"); the sample-count
#: floor guards against a report reading two lucky, widely-spaced passes as a
#: full week of clean soak.
DEFAULT_MIN_SPAN_DAYS = 7
DEFAULT_MIN_SAMPLES = 7

_DAY_FMT = "%Y-%m-%d"
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().strftime(_TS_FMT)


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, _TS_FMT).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


# ── where the artifact lives ────────────────────────────────────────────────


def soak_dir(paths: FleetPaths) -> Path:
    """``<fleet-root>/atlas/soak/`` -- a sibling of the existing
    ``atlas/state`` and ``atlas/brief`` dirs, on the same Syncthing-shared
    tree, but its own subdir so retention pruning never touches anything
    else planted under ``atlas/``.
    """
    return paths.root / "atlas" / "soak"


def _sample_path(paths: FleetPaths, *, node: str, when: datetime) -> Path:
    """One file per (node, day): bounds any single file's growth to one
    node's passes in one day, and lets multiple nodes soak concurrently
    without ever appending to the same file (no interleaved-write risk on a
    Syncthing-shared tree)."""
    return soak_dir(paths) / f"{node}-{when.strftime(_DAY_FMT)}.jsonl"


# ── endpoint registration lookup (metadata read, not a probe) ──────────────


def _endpoint_registrations(paths: FleetPaths) -> dict[str, str | None]:
    """``{app_name: spec.endpoint}`` for every non-deleted Operatorapp.

    A second read of the same specs `eyes.assess` already reads internally,
    for the one field it does not expose (see the module docstring). No
    subprocess, no adapter call, no timeout: a static JSON read exactly like
    every other read-only ``store.list_specs`` call in this codebase.
    """
    out: dict[str, str | None] = {}
    for spec_obj in store.list_specs(paths, "operatorapp"):
        spec = spec_obj.get("spec", {}) or {}
        if spec.get("deleted"):
            continue
        name = spec_obj.get("name") or spec.get("name") or "?"
        out[name] = spec.get("endpoint") or None
    return out


# ── reducing one eyes.assess() pass to a soak sample ────────────────────────


def _endpoint_reading(endpoint: str | None, cli_lane: dict) -> dict:
    """Classify the ENDPOINT lane for one app (see module docstring)."""
    if not endpoint:
        return {"flavor": "no-endpoint-registered", "conditions": {}}
    if cli_lane.get("state") != "ok":
        return {
            "flavor": "endpoint-unreachable",
            "raw_state": cli_lane.get("state"),
            "detail": cli_lane.get("detail", ""),
            "conditions": {},
        }
    return {
        "flavor": "ok",
        "conditions": {c["type"]: c["status"] for c in cli_lane["conditions"]},
    }


def _old_lane_reading(seat_lane: dict) -> dict:
    """Classify the OLD lane (built-in seat adapter -- Phase 5: advisory
    through dual-read, deleted after two clean weeks)."""
    if seat_lane.get("state") != "ok":
        return {
            "flavor": "unreachable",
            "raw_state": seat_lane.get("state"),
            "detail": seat_lane.get("detail", ""),
            "conditions": {},
        }
    return {
        "flavor": "ok",
        "conditions": {c["type"]: c["status"] for c in seat_lane["conditions"]},
    }


def capture(assessment: dict, endpoints: dict[str, str | None]) -> dict:
    """Reduce one ``eyes.assess()`` pass to a compact soak sample.

    Pure function, no I/O: :func:`record` is the thin wrapper that calls
    ``eyes.assess`` and this function, then appends the result. Kept
    separate so tests can exercise the reduction against a hand-built
    assessment without touching a filesystem or a subprocess.
    """
    apps = []
    for app in assessment.get("apps", []):
        name = app["name"]
        endpoint = endpoints.get(name)
        apps.append(
            {
                "name": name,
                "endpoint": _endpoint_reading(endpoint, app.get("cli_lane", {})),
                "old": _old_lane_reading(app.get("seat_lane", {})),
            }
        )
    return {
        "schema": SCHEMA,
        "at": assessment.get("at", _now_iso()),
        "frozen": bool(assessment.get("frozen", False)),
        "apps": apps,
    }


# ── recording (the only write this module ever does) ───────────────────────


def prune(
    paths: FleetPaths, *, retention_days: int = DEFAULT_RETENTION_DAYS, now: datetime | None = None
) -> list[Path]:
    """Delete sample files older than ``retention_days``. Returns what it removed.

    File age is read from the filename's date, not mtime, so pruning is
    deterministic and independent of Syncthing's own mtime handling.
    """
    directory = soak_dir(paths)
    if not directory.is_dir():
        return []
    cutoff = (now or _now()) - timedelta(days=retention_days)
    removed = []
    for p in directory.glob("*.jsonl"):
        # filenames are "<node>-<YYYY-MM-DD>.jsonl"; the date is always the
        # last 3 dash-separated fields, however many dashes the node name
        # itself contains.
        parts = p.stem.split("-")
        if len(parts) < 3:
            continue
        date_str = "-".join(parts[-3:])
        try:
            file_day = datetime.strptime(date_str, _DAY_FMT).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if file_day < cutoff.replace(hour=0, minute=0, second=0, microsecond=0):
            with contextlib.suppress(OSError):
                p.unlink()
                removed.append(p)
    return removed


def record(
    paths: FleetPaths,
    *,
    node: str | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    assess_fn: Callable[..., dict] | None = None,
    now_iso: str | None = None,
    **eyes_kwargs: Any,
) -> dict:
    """One soak pass: assess (via `eyes`), reduce, append, prune. Read-only
    except for the single append to this module's own artifact directory.

    Args:
        paths: Fleet paths (same tree `eyes.assess` reads).
        node: This process's node name for the sample filename (default
            `fleet.paths.self_node_name()`).
        retention_days: Passed to `prune`, run after every successful append.
        assess_fn: Injectable replacement for `eyes.assess` (tests only).
        now_iso: Injectable timestamp (tests only); also threaded into
            `eyes.assess` so the sample and the assessment agree.
        **eyes_kwargs: Forwarded to `eyes.assess` (e.g. `cli_timeout`).

    Returns:
        `{"sample": <the recorded dict>, "path": <Path written to>}`.
    """
    assess_fn = assess_fn or eyes.assess
    now = now_iso or _now_iso()
    assessment = assess_fn(paths, now_iso=now, **eyes_kwargs)
    endpoints = _endpoint_registrations(paths)
    sample = capture(assessment, endpoints)

    node = node or self_node_name()
    when = _parse_ts(sample["at"]) or _now()
    directory = soak_dir(paths)
    directory.mkdir(parents=True, exist_ok=True)
    path = _sample_path(paths, node=node, when=when)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(sample, sort_keys=True) + "\n")

    prune(paths, retention_days=retention_days, now=when)
    return {"sample": sample, "path": path}


# ── reading a window of samples back ────────────────────────────────────────


def _iter_samples(paths: FleetPaths, *, since: datetime, until: datetime):
    directory = soak_dir(paths)
    if not directory.is_dir():
        return
    for p in sorted(directory.glob("*.jsonl")):
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                sample = json.loads(line)
            except json.JSONDecodeError:
                continue
            at = _parse_ts(sample.get("at", ""))
            if at is None or not (since <= at <= until):
                continue
            yield sample


# ── the two gate metrics ────────────────────────────────────────────────────


@dataclass
class AppSoak:
    """Accumulated soak evidence for one app over a report window."""

    name: str
    total_samples: int = 0
    endpoint_registered_samples: int = 0
    comparable_samples: int = 0
    lane_conflicts: int = 0
    unknown_regressions: int = 0
    conflict_examples: list[str] = field(default_factory=list)
    regression_examples: list[str] = field(default_factory=list)
    first_at: str | None = None
    last_at: str | None = None

    @property
    def span_days(self) -> float:
        first, last = _parse_ts(self.first_at or ""), _parse_ts(self.last_at or "")
        if first is None or last is None:
            return 0.0
        return max(0.0, (last - first).total_seconds() / 86400.0)

    def verdict(self, *, min_span_days: float, min_samples: int) -> str:
        if self.endpoint_registered_samples == 0:
            return "NO-ENDPOINT"
        if self.comparable_samples == 0:
            return "PENDING"
        if self.lane_conflicts or self.unknown_regressions:
            return "BLOCKED"
        if self.span_days >= min_span_days and self.comparable_samples >= min_samples:
            return "READY"
        return "SOAKING"


def report(
    paths: FleetPaths,
    *,
    window_days: int = DEFAULT_RETENTION_DAYS,
    min_span_days: float = DEFAULT_MIN_SPAN_DAYS,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    now: datetime | None = None,
    example_limit: int = 5,
) -> dict:
    """Read back `window_days` of samples and answer the two gate questions
    per app: LaneConflict count and Unknown-regression count, plus a
    readiness verdict (see `AppSoak.verdict`).

    A condition contributes to either counter ONLY when both lanes actually
    produced a reading (`endpoint.flavor == "ok"` and `old.flavor == "ok"`)
    for that sample -- comparing a real reading against "no endpoint
    registered" would be meaningless, not evidence of parity.
    """
    now = now or _now()
    since = now - timedelta(days=window_days)
    by_app: dict[str, AppSoak] = {}
    sample_count = 0

    for sample in _iter_samples(paths, since=since, until=now):
        sample_count += 1
        at = sample.get("at", "")
        for app in sample.get("apps", []):
            name = app.get("name", "?")
            acc = by_app.setdefault(name, AppSoak(name=name))
            acc.total_samples += 1
            if acc.first_at is None or at < acc.first_at:
                acc.first_at = at
            if acc.last_at is None or at > acc.last_at:
                acc.last_at = at

            endpoint = app.get("endpoint", {})
            old = app.get("old", {})
            if endpoint.get("flavor") != "no-endpoint-registered":
                acc.endpoint_registered_samples += 1
            if endpoint.get("flavor") != "ok" or old.get("flavor") != "ok":
                continue
            acc.comparable_samples += 1
            endpoint_conds = endpoint.get("conditions", {})
            old_conds = old.get("conditions", {})
            for ctype in sorted(set(endpoint_conds) & set(old_conds)):
                e_status, o_status = endpoint_conds[ctype], old_conds[ctype]
                if e_status != o_status:
                    acc.lane_conflicts += 1
                    if len(acc.conflict_examples) < example_limit:
                        acc.conflict_examples.append(
                            f"{at} {ctype}: endpoint={e_status!r} old={o_status!r}"
                        )
                if o_status in ("True", "False") and e_status == "Unknown":
                    acc.unknown_regressions += 1
                    if len(acc.regression_examples) < example_limit:
                        acc.regression_examples.append(
                            f"{at} {ctype}: old={o_status!r} -> endpoint=Unknown"
                        )

    apps = []
    for name in sorted(by_app):
        acc = by_app[name]
        apps.append(
            {
                "name": name,
                "total_samples": acc.total_samples,
                "endpoint_registered_samples": acc.endpoint_registered_samples,
                "comparable_samples": acc.comparable_samples,
                "span_days": round(acc.span_days, 2),
                "lane_conflicts": acc.lane_conflicts,
                "unknown_regressions": acc.unknown_regressions,
                "conflict_examples": acc.conflict_examples,
                "regression_examples": acc.regression_examples,
                "verdict": acc.verdict(min_span_days=min_span_days, min_samples=min_samples),
            }
        )

    return {
        "schema": "skoperator.soak-report/v1",
        "at": now.strftime(_TS_FMT),
        "window_days": window_days,
        "min_span_days": min_span_days,
        "min_samples": min_samples,
        "sample_passes": sample_count,
        "apps": apps,
    }


# ── rendering ────────────────────────────────────────────────────────────────

_VERDICT_NOTE = {
    "NO-ENDPOINT": "no v2 endpoint registered in this window; nothing to compare yet",
    "PENDING": "endpoint registered but never produced a reading in this window",
    "BLOCKED": "do not demote: at least one conflict or Unknown-regression in this window",
    "SOAKING": "clean so far, window/sample floor not met yet",
    "READY": "clean for the full window: safe to demote the old lane",
}


def render(rep: dict) -> str:
    """Terse phone-readable report, same register as `eyes.render`."""
    lines: list[str] = []
    lines.append(
        f"ATLAS SOAK  {rep['at']}  window={rep['window_days']}d  "
        f"gate: span>={rep['min_span_days']}d samples>={rep['min_samples']}"
    )
    lines.append("")

    apps = rep["apps"]
    if not apps or rep["sample_passes"] == 0:
        lines.append("No soak samples recorded yet.")
        lines.append(
            "Nothing to compare: run `skcapstone atlas soak record` on a schedule "
            "(hourly or daily cron) to start accumulating dual-read evidence."
        )
        return "\n".join(lines)

    width = max((len(a["name"]) for a in apps), default=4)
    for app in apps:
        lines.append(
            f" {app['verdict']:<11} {app['name']:<{width}}  {_VERDICT_NOTE[app['verdict']]}"
        )
        lines.append(
            f"   samples: {app['total_samples']} total, "
            f"{app['endpoint_registered_samples']} endpoint-registered, "
            f"{app['comparable_samples']} comparable  span={app['span_days']}d"
        )
        if app["endpoint_registered_samples"] == 0:
            continue
        lines.append(
            f"   LaneConflict={app['lane_conflicts']}  "
            f"Unknown-regression={app['unknown_regressions']}"
        )
        for ex in app["conflict_examples"]:
            lines.append(f"   != {ex}")
        for ex in app["regression_examples"]:
            lines.append(f"   ?! {ex}")
    lines.append("")

    ready = [a["name"] for a in apps if a["verdict"] == "READY"]
    blocked = [a["name"] for a in apps if a["verdict"] == "BLOCKED"]
    lines.append(f"READY TO DEMOTE ({len(ready)}): {', '.join(ready) if ready else 'none'}")
    lines.append(f"BLOCKED ({len(blocked)}): {', '.join(blocked) if blocked else 'none'}")
    return "\n".join(lines)


__all__ = [
    "SCHEMA",
    "DEFAULT_RETENTION_DAYS",
    "DEFAULT_MIN_SPAN_DAYS",
    "DEFAULT_MIN_SAMPLES",
    "AppSoak",
    "capture",
    "prune",
    "record",
    "render",
    "report",
    "soak_dir",
]
