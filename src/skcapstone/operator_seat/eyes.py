"""ATLAS Eyes: a read-only estate assessor that works WHILE the seat is frozen.

The freeze stops observation and actuation in the operator loop, which means a
frozen ATLAS reports "standing down. No observation, no action" and Chef is
blind to the operator estate exactly when he most needs to see it. But the
``observe`` verb is read-only and freeze-independent by contract, so this module
walks the whole plane without touching the freeze, the loop, or any timer.

Two observation lanes per registered Operatorapp, because they can (and do)
disagree:

  * **cli lane**: the app's DECLARED public contract, ``<spec.cli> observe``,
    run out-of-process with a hard timeout. This is the discovery-path truth:
    what any operator seat honoring the registration would see.
  * **seat lane**: the in-process adapter in ``loop.ADAPTERS``: the exact code
    an UNFROZEN ATLAS would run. This is what ATLAS itself would see.

Unknown is a first-class result, not an error. A condition reporting
``Unknown`` and an app that cannot be reached at all are DIFFERENT states and
render differently:

  * ``no-cli``       the declared binary does not exist on this node
  * ``cli-error``    the binary exists but rejects the observe verb
  * ``timeout``      the observe hung past the deadline
  * ``unparseable``  it answered, but not with contract JSON
  * ``ok`` + per-condition ``True``/``False``/``Unknown`` statuses, with any
    DECLARED condition the payload omitted surfaced as ``Unknown`` (absent)

Everything here is read-only: no ``act``, no freeze writes, no fleet-store
writes, no ITIL writes. Fail-soft per app: one dead app never blanks the pass.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import shlex
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..fleet import store
from ..fleet.paths import FleetPaths

SCHEMA = "skoperator.eyes/v1"

#: Hard per-app deadline (seconds) for the out-of-process cli lane.
CLI_TIMEOUT = 15.0

#: Hard per-app deadline (seconds) for the in-process seat lane. The worker
#: thread cannot be killed, but the pass never waits past this.
SEAT_TIMEOUT = 10.0

_UNREACHABLE = frozenset(
    {"no-cli", "cli-error", "timeout", "unparseable", "no-adapter", "error", "unregistered"}
)


class LaneConflictError(Exception):
    """Raised when the cli lane and the seat lane disagree about a condition.

    Per PR #179's design ("exactly one authoritative producer per condition;
    two authoritative readings = hard LaneConflictError rather than a silent
    preference"), a second reading for the same condition is never averaged,
    never silently preferred, and never dropped: it is a hard failure. Use
    :func:`assert_no_conflicts` to turn an assessment's conflicts into this
    exception, e.g. from a CI gate or a caller that must not proceed on a
    reading it cannot trust (card 504d0046).
    """


def assert_no_conflicts(assessment: dict) -> None:
    """Raise :class:`LaneConflictError` if any app in ``assessment`` has a lane
    conflict. Silent (returns None) when there are none.

    This is the hard-failure counterpart to ``render``'s ``!=`` lines: printing
    a conflict is necessary but not sufficient, because printed text is easy to
    skim past. A caller that needs to KNOW, not just be told, calls this after
    :func:`assess` and lets it raise. Used by ``atlas eyes --strict`` to turn
    "zero CONFLICT rows" into a script-checkable exit code instead of a report
    a human has to read carefully.
    """
    conflicts = [
        f"{app['name']}.{c['type']} (cli={c['cli']!r} seat={c['seat']!r})"
        for app in assessment.get("apps", [])
        for c in app.get("conflicts", [])
    ]
    if conflicts:
        raise LaneConflictError(
            "lane conflict: at least one reading is wrong for "
            + ", ".join(conflicts)
            + " (a lying lane must not be promoted to source of truth)"
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── condition classification ────────────────────────────────────────────────


def classify_condition(cond_type: str, status: str, problem_when_true: frozenset) -> str:
    """Classify one condition status into ``firing`` / ``quiet`` / ``unknown``.

    Polarity-aware: a problem-type condition fires when ``True``; a health-type
    condition fires when ``False``. Anything else (``Unknown`` included) is
    ``unknown``: never silently healthy.
    """
    if status == "Unknown":
        return "unknown"
    if cond_type in problem_when_true:
        return "firing" if status == "True" else "quiet"
    return "firing" if status == "False" else "quiet"


def _lane_conditions(
    payload: dict, declared: list[str], problem_when_true: frozenset
) -> list[dict]:
    """Normalize an observe payload's conditions, appending declared-but-absent
    conditions as ``Unknown`` (absent) so an omission can never pass as health."""
    out: list[dict] = []
    seen: set[str] = set()
    for cond in payload.get("conditions", []) or []:
        if not isinstance(cond, dict):
            continue
        ctype = str(cond.get("type", "?"))
        status = str(cond.get("status", "Unknown"))
        if status not in ("True", "False", "Unknown"):
            status = "Unknown"
        seen.add(ctype)
        entry = {
            "type": ctype,
            "status": status,
            "class": classify_condition(ctype, status, problem_when_true),
        }
        if cond.get("object"):
            entry["object"] = str(cond["object"])
        out.append(entry)
    for ctype in declared:
        if ctype not in seen:
            out.append({"type": ctype, "status": "Unknown", "class": "unknown", "absent": True})
    return out


# ── cli lane (out-of-process, declared contract) ────────────────────────────


def _default_run(argv: list[str], timeout: float) -> tuple[int, str, str]:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def observe_via_cli(
    cli: str,
    declared: list[str],
    problem_when_true: frozenset,
    *,
    timeout: float = CLI_TIMEOUT,
    run: Callable[[list[str], float], tuple[int, str, str]] | None = None,
) -> dict:
    """Run ``<cli> observe`` out-of-process and classify the outcome.

    Returns ``{"state": ..., "conditions": [...], "detail": ...}`` where state
    is ``ok`` | ``no-cli`` | ``cli-error`` | ``timeout`` | ``unparseable``.
    Never raises: every failure mode is a state, not an exception.
    """
    run = run or _default_run
    try:
        argv = shlex.split(cli) + ["observe"]
    except ValueError as exc:
        return {"state": "cli-error", "conditions": [], "detail": f"bad cli spec: {exc}"}
    if not argv or shutil.which(argv[0]) is None:
        return {
            "state": "no-cli",
            "conditions": [],
            "detail": f"binary {argv[0] if argv else '?'!r} not on PATH",
        }
    try:
        code, out, err = run(argv, timeout)
    except subprocess.TimeoutExpired:
        return {"state": "timeout", "conditions": [], "detail": f"no answer in {timeout:.0f}s"}
    except OSError as exc:
        return {"state": "cli-error", "conditions": [], "detail": str(exc)}
    if code != 0:
        first = next((ln for ln in (err or out).splitlines() if ln.strip()), "")
        return {"state": "cli-error", "conditions": [], "detail": f"exit {code}: {first[:120]}"}
    payload = _lenient_json(out)
    if payload is None or not isinstance(payload.get("conditions"), list):
        return {
            "state": "unparseable",
            "conditions": [],
            "detail": (out or "").strip()[:120] or "empty stdout",
        }
    return {
        "state": "ok",
        "conditions": _lane_conditions(payload, declared, problem_when_true),
        "detail": "",
    }


def _lenient_json(text: str) -> dict | None:
    """Parse the first JSON object out of stdout, tolerating warning preambles."""
    idx = text.find("{")
    if idx < 0:
        return None
    try:
        obj = json.loads(text[idx:])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


# ── seat lane (in-process, what an unfrozen ATLAS runs) ─────────────────────


def observe_via_seat(
    name: str,
    adapters: dict[str, Callable[..., dict]],
    declared: list[str],
    problem_when_true: frozenset,
    paths: FleetPaths,
    now_iso: str,
    *,
    timeout: float = SEAT_TIMEOUT,
) -> dict:
    """Run the in-process ``loop.ADAPTERS`` observe for one app, deadline-bounded.

    State is ``ok`` | ``no-adapter`` | ``timeout`` | ``error``. A hung adapter
    thread is abandoned (daemon executor), never waited on.
    """
    fn = adapters.get(name)
    if fn is None:
        return {
            "state": "no-adapter",
            "conditions": [],
            "detail": "not in loop.ADAPTERS: an unfrozen ATLAS would not observe it either",
        }
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"eyes-{name}")
    try:
        future = executor.submit(fn, paths, now_iso)
        try:
            payload = future.result(timeout=timeout)
        except _FutureTimeout:
            return {"state": "timeout", "conditions": [], "detail": f"no answer in {timeout:.0f}s"}
        except Exception as exc:  # noqa: BLE001 - fail soft per app, always
            return {"state": "error", "conditions": [], "detail": f"{type(exc).__name__}: {exc}"}
    finally:
        executor.shutdown(wait=False)
    if not isinstance(payload, dict):
        return {"state": "error", "conditions": [], "detail": "adapter returned non-dict"}
    return {
        "state": "ok",
        "conditions": _lane_conditions(payload, declared, problem_when_true),
        "detail": "",
    }


# ── lane merge ──────────────────────────────────────────────────────────────


def lane_conflicts(cli_lane: dict, seat_lane: dict) -> list[dict]:
    """Condition types where the two lanes returned DIFFERENT statuses.

    A conflict means at least one lane is lying or stale: it erodes trust in
    the whole reading and must be surfaced, not averaged away.
    """
    if cli_lane.get("state") != "ok" or seat_lane.get("state") != "ok":
        return []
    cli_map = {c["type"]: c["status"] for c in cli_lane["conditions"]}
    seat_map = {c["type"]: c["status"] for c in seat_lane["conditions"]}
    out = []
    for ctype in sorted(set(cli_map) & set(seat_map)):
        if cli_map[ctype] != seat_map[ctype]:
            out.append({"type": ctype, "cli": cli_map[ctype], "seat": seat_map[ctype]})
    return out


def app_verdict(cli_lane: dict, seat_lane: dict, conflicts: list[dict]) -> str:
    """One word per app: ``BLIND`` > ``FIRING`` > ``CONFLICT`` > ``UNKNOWN`` > ``OK``."""
    lanes = [cli_lane, seat_lane]
    if all(lane.get("state") in _UNREACHABLE for lane in lanes):
        return "BLIND"
    classes = {
        cond["class"] for lane in lanes if lane.get("state") == "ok" for cond in lane["conditions"]
    }
    if "firing" in classes:
        return "FIRING"
    if conflicts:
        return "CONFLICT"
    if "unknown" in classes or any(lane.get("state") in _UNREACHABLE for lane in lanes):
        return "UNKNOWN"
    return "OK"


# ── ITIL correlation (read-only) ────────────────────────────────────────────


def _match_app(app: str, texts: list[str]) -> bool:
    """True when ``app`` appears in any text with a non-alphanumeric left edge,
    so ``skchat`` matches ``skchat-webui-lumina`` but ``chat`` never matches it."""
    pat = re.compile(rf"(?<![a-z0-9]){re.escape(app.lower())}")
    return any(pat.search(t.lower()) for t in texts if t)


def _sev(value: Any) -> str:
    """Coerce a Severity enum (or anything) to its plain string value."""
    return str(getattr(value, "value", value))


def correlate_itil(app_names: list[str], itil_home: Path) -> dict:
    """Read-only ITIL snapshot + open incident/problem mapping per app.

    Fail-soft: an unreadable ITIL store yields ``{"available": False}`` with
    the reason, never an exception.
    """
    try:
        from skcoord.itil import OPEN_INCIDENT_STATUSES, ITILManager
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "detail": f"skcoord.itil import failed: {exc}"}
    try:
        mgr = ITILManager(itil_home)
        sink = io.StringIO()  # legacy-file warnings print to stdout; keep the report clean
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            status = mgr.get_status()
            incidents = [i for i in mgr.list_incidents() if i.status in OPEN_INCIDENT_STATUSES]
            problems = [p for p in mgr.list_problems() if p.status in ("investigating", "known")]
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "detail": f"{type(exc).__name__}: {exc}"}

    legacy = sorted(
        p.name
        for sub in ("incidents", "problems", "changes")
        for p in (mgr.itil_dir / sub).glob("*.json")
        if p.is_file()
    )

    by_app: dict[str, dict] = {name: {"incidents": [], "problems": []} for name in app_names}
    unmapped_incidents = []
    for inc in incidents:
        texts = [inc.title, *(inc.tags or []), *(inc.affected_services or [])]
        hit = False
        for name in app_names:
            if _match_app(name, texts):
                by_app[name]["incidents"].append(
                    {"id": inc.id, "severity": _sev(inc.severity), "title": inc.title}
                )
                hit = True
        if not hit:
            unmapped_incidents.append(
                {"id": inc.id, "severity": _sev(inc.severity), "title": inc.title}
            )
    for prob in problems:
        texts = [prob.title, *(prob.tags or [])]
        for name in app_names:
            if _match_app(name, texts):
                by_app[name]["problems"].append({"id": prob.id, "title": prob.title})

    return {
        "available": True,
        "open_incidents": status["incidents"]["open"],
        "by_severity": {k: v for k, v in status["incidents"]["by_severity"].items() if v},
        "active_problems": status["problems"]["active"],
        "pending_changes": status["changes"]["pending"],
        "kedb_entries": status["kedb"]["total"],
        "legacy_flat_files": legacy,
        "by_app": by_app,
        "unmapped_incidents": unmapped_incidents,
    }


# ── unregistered modules (context, read-only) ───────────────────────────────


def unregistered_modules(home: Path, registered: list[str]) -> list[dict]:
    """Shell modules that are NOT registered Operatorapps (e.g. a disabled skbrain).

    Context only: these are declared capabilities no operator seat can see at all.
    """
    path = home / "shell" / "modules.json"
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for mod in payload.get("modules", []) or []:
        mid = mod.get("id")
        if mid and mid not in registered:
            out.append({"id": mid, "enabled": bool(mod.get("enabled"))})
    return out


# ── the one pass ────────────────────────────────────────────────────────────


def assess(
    paths: FleetPaths,
    *,
    itil_home: Path | None = None,
    skcapstone_home: Path | None = None,
    cli_timeout: float = CLI_TIMEOUT,
    seat_timeout: float = SEAT_TIMEOUT,
    now_iso: str | None = None,
    run: Callable[[list[str], float], tuple[int, str, str]] | None = None,
    adapters: dict[str, Callable[..., dict]] | None = None,
    problem_when_true: frozenset | None = None,
) -> dict:
    """One read-only pass over the whole operator estate. Never writes anything.

    Args:
        paths: Fleet paths (freeze file + operatorapp registry live here).
        itil_home: skcapstone home holding ``coordination/itil`` (default: the
            fleet root's parent, i.e. ``~/.skcapstone``).
        skcapstone_home: home holding ``shell/modules.json`` (same default).
        cli_timeout / seat_timeout: hard per-app deadlines, seconds.
        now_iso / run / adapters / problem_when_true: injectable for tests.

    Returns:
        The ``skoperator.eyes/v1`` assessment dict (see ``render`` for shape use).
    """
    now = now_iso or _now_iso()
    if adapters is None or problem_when_true is None:
        from . import loop  # deferred: the loop pulls in the whole seat

        adapters = adapters if adapters is not None else loop.ADAPTERS
        problem_when_true = (
            problem_when_true if problem_when_true is not None else loop.PROBLEM_WHEN_TRUE
        )

    frozen = store.is_frozen(paths)
    freeze_reason = ""
    try:
        freeze_payload = json.loads(paths.freeze_path().read_text())
        freeze_reason = str(freeze_payload.get("reason", ""))
    except (OSError, json.JSONDecodeError):
        pass

    specs = store.list_specs(paths, "operatorapp")
    apps = []
    for spec_obj in sorted(specs, key=lambda s: s.get("name", "")):
        spec = spec_obj.get("spec", {}) or {}
        name = spec_obj.get("name") or spec.get("name") or "?"
        if spec.get("deleted"):
            continue
        declared = [str(c) for c in spec.get("conditions", []) or []]
        cli = str(spec.get("cli", "") or "")
        if cli:
            cli_lane = observe_via_cli(
                cli, declared, problem_when_true, timeout=cli_timeout, run=run
            )
        else:
            cli_lane = {"state": "no-cli", "conditions": [], "detail": "spec declares no cli"}
        seat_lane = observe_via_seat(
            name, adapters, declared, problem_when_true, paths, now, timeout=seat_timeout
        )
        conflicts = lane_conflicts(cli_lane, seat_lane)
        apps.append(
            {
                "name": name,
                "cli": cli,
                "declared_conditions": declared,
                "cli_lane": cli_lane,
                "seat_lane": seat_lane,
                "conflicts": conflicts,
                "verdict": app_verdict(cli_lane, seat_lane, conflicts),
            }
        )

    registered = {a["name"] for a in apps}
    for name in sorted(adapters):
        if name in registered:
            continue
        cli_lane = {
            "state": "unregistered",
            "conditions": [],
            "detail": "no Operatorapp registration; seat-only builtin",
        }
        seat_lane = observe_via_seat(
            name, adapters, [], problem_when_true, paths, now, timeout=seat_timeout
        )
        apps.append(
            {
                "name": name,
                "cli": "",
                "declared_conditions": [],
                "cli_lane": cli_lane,
                "seat_lane": seat_lane,
                "conflicts": [],
                "verdict": app_verdict(cli_lane, seat_lane, []),
            }
        )

    default_home = paths.root.parent if paths.root.name == "fleet" else Path.home() / ".skcapstone"
    home = skcapstone_home or default_home
    itil = correlate_itil([a["name"] for a in apps], itil_home or home)
    extra_modules = unregistered_modules(home, [a["name"] for a in apps])

    return {
        "schema": SCHEMA,
        "at": now,
        "frozen": frozen,
        "freeze_reason": freeze_reason,
        "apps": apps,
        "itil": itil,
        "unregistered_modules": extra_modules,
        "blind_spots": blind_spots(frozen, apps, itil, extra_modules),
    }


# ── blind spots ─────────────────────────────────────────────────────────────


def blind_spots(
    frozen: bool, apps: list[dict], itil: dict, extra_modules: list[dict]
) -> list[str]:
    """Plain sentences: what ATLAS would be blind to if unfrozen RIGHT NOW.

    The freeze hides everything; these are the holes that stay hidden even
    after an unfreeze, which is the part nobody is looking at.
    """
    out: list[str] = []
    for app in apps:
        name = app["name"]
        seat, cli = app["seat_lane"], app["cli_lane"]
        if cli["state"] == "unregistered":
            out.append(
                f"{name}: observed only by the built-in seat adapter; no Operatorapp "
                f"registration, so the discovery path cannot see it."
            )
        elif seat["state"] == "no-adapter" and cli["state"] != "ok":
            out.append(
                f"{name}: invisible even unfrozen. No in-process adapter and the declared "
                f"cli is dead ({cli['state']}: {cli['detail']})."
            )
        elif seat["state"] == "no-adapter":
            out.append(
                f"{name}: unfrozen ATLAS still would not see it (no in-process adapter); "
                f"only the cli contract answers."
            )
        elif cli["state"] != "ok":
            out.append(
                f"{name}: declared cli contract is dead ({cli['state']}: {cli['detail']}); "
                f"any seat honoring the registration instead of the built-in adapter sees nothing."
            )
        unknown_types: dict[str, dict] = {}
        if seat["state"] == "ok":
            for cond in seat["conditions"]:
                if cond["class"] != "unknown":
                    continue
                slot = unknown_types.setdefault(
                    cond["type"], {"count": 0, "absent": bool(cond.get("absent"))}
                )
                slot["count"] += 1
        for ctype, slot in unknown_types.items():
            why = "not reported by the adapter" if slot["absent"] else "probe failed"
            objs = f" across {slot['count']} objects" if slot["count"] > 1 else ""
            out.append(f"{name}.{ctype}: Unknown to an unfrozen ATLAS ({why}{objs}).")
        for conflict in app["conflicts"]:
            out.append(
                f"{name}.{conflict['type']}: lanes disagree (cli={conflict['cli']} vs "
                f"seat={conflict['seat']}); at least one reading is wrong."
            )
    if itil.get("available"):
        if itil["legacy_flat_files"]:
            files = ", ".join(itil["legacy_flat_files"])
            out.append(
                f"ITIL: {len(itil['legacy_flat_files'])} legacy flat files skipped on every "
                f"load ({files}); run itil_migrate_events.py or they stay invisible."
            )
        if itil["pending_changes"]:
            out.append(
                f"ITIL: {itil['pending_changes']} pending changes; at that depth the change "
                f"queue is noise, not a queue."
            )
    else:
        out.append(f"ITIL store unreadable: {itil.get('detail', 'unknown reason')}.")
    for mod in extra_modules:
        state = "enabled" if mod["enabled"] else "disabled"
        out.append(
            f"{mod['id']}: shell module ({state}) with no Operatorapp registration; "
            f"no operator seat can observe it at all."
        )
    return out


# ── rendering (phone-width, terse, truthful) ────────────────────────────────

_STATE_SHORT = {
    "ok": "ok",
    "no-cli": "NO CLI",
    "cli-error": "CLI ERR",
    "timeout": "TIMEOUT",
    "unparseable": "BAD JSON",
    "no-adapter": "NO ADAPTER",
    "error": "ERR",
    "unregistered": "NOT REGISTERED",
}


def _counted(types: list[str]) -> str:
    """Render a condition-type list as ``A,B x3``: duplicates counted, order kept."""
    from collections import Counter

    counts = Counter(types)
    seen: set[str] = set()
    parts = []
    for ctype in types:
        if ctype in seen:
            continue
        seen.add(ctype)
        n = counts[ctype]
        parts.append(f"{ctype} x{n}" if n > 1 else ctype)
    return ",".join(parts)


def _lane_summary(lane: dict) -> str:
    if lane["state"] != "ok":
        return _STATE_SHORT.get(lane["state"], lane["state"])
    firing = [c["type"] for c in lane["conditions"] if c["class"] == "firing"]
    unknown = [c["type"] for c in lane["conditions"] if c["class"] == "unknown"]
    bits = []
    if firing:
        bits.append("FIRING " + _counted(firing))
    if unknown:
        bits.append("? " + _counted(unknown))
    return "; ".join(bits) if bits else "all quiet"


def render(assessment: dict) -> str:
    """Render the assessment as a terse phone-readable report."""
    lines: list[str] = []
    frozen = "FROZEN" if assessment["frozen"] else "not frozen"
    lines.append(f"ATLAS EYES  {assessment['at']}  [{frozen}]")
    if assessment["frozen"]:
        reason = assessment["freeze_reason"] or "no reason recorded"
        lines.append(f"freeze: {reason}")
        lines.append("ATLAS is observing nothing. This pass is read-only and freeze-proof.")
    lines.append("")

    apps = assessment["apps"]
    seat_only = sum(1 for a in apps if a["cli_lane"]["state"] == "unregistered")
    suffix = f" + {seat_only} seat-only" if seat_only else ""
    lines.append(f"APPS ({len(apps) - seat_only} registered{suffix})")
    width = max((len(a["name"]) for a in apps), default=4)
    itil = assessment["itil"]
    for app in apps:
        lines.append(f" {app['verdict']:<8} {app['name']:<{width}}")
        lines.append(f"   cli:  {_lane_summary(app['cli_lane'])}")
        lines.append(f"   seat: {_lane_summary(app['seat_lane'])}")
        for conflict in app["conflicts"]:
            lines.append(
                f"   != {conflict['type']}: cli={conflict['cli']} seat={conflict['seat']}"
            )
        if itil.get("available"):
            hits = itil["by_app"].get(app["name"], {})
            for inc in hits.get("incidents", []):
                lines.append(f"   inc {inc['severity']} {inc['id']}: {inc['title'][:60]}")
            for prob in hits.get("problems", []):
                lines.append(f"   prb {prob['id']}: {prob['title'][:60]}")
    lines.append("")

    if itil.get("available"):
        sev = " ".join(f"{k}:{v}" for k, v in itil["by_severity"].items())
        lines.append(
            f"ITIL  {itil['open_incidents']} open incidents ({sev})  "
            f"{itil['active_problems']} active problems"
        )
        lines.append(
            f"      {itil['pending_changes']} PENDING CHANGES  "
            f"{itil['kedb_entries']} KEDB  "
            f"{len(itil['legacy_flat_files'])} legacy files unmigrated"
        )
        unmapped = itil["unmapped_incidents"]
        if unmapped:
            lines.append(f"      {len(unmapped)} open incidents map to no registered app:")
            for inc in unmapped:
                lines.append(f"        {inc['severity']} {inc['id']}: {inc['title'][:56]}")
    else:
        lines.append(f"ITIL  unreadable: {itil.get('detail', '?')}")
    lines.append("")

    lines.append("BLIND EVEN IF UNFROZEN")
    spots = assessment["blind_spots"]
    if spots:
        for spot in spots:
            lines.append(f" - {spot}")
    else:
        lines.append(" - none found")
    return "\n".join(lines)


__all__ = [
    "SCHEMA",
    "LaneConflictError",
    "assert_no_conflicts",
    "assess",
    "app_verdict",
    "blind_spots",
    "classify_condition",
    "correlate_itil",
    "lane_conflicts",
    "observe_via_cli",
    "observe_via_seat",
    "render",
    "unregistered_modules",
]
