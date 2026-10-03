"""skos operator adapter: Atlas manages skos too (O7 app adapter).

Conformant to the adapter contract (explain / observe / act). The health probe is
injectable so tests never touch a live skos; the default reads the skos scheduler
status and fails safe (reports healthy) rather than raising a false alarm.

Four conditions, two of them about skwatchdog (the daily digest skos narrates):

  * ``SchedulerAlive``       the skos scheduler-as-code pipeline is running jobs.
  * ``GtdSinkDraining``      the GTD ingest sink is not backed up on failed items.
  * ``WatchdogDigestFresh``  the published digest (``<watchdog>/digests/latest/
    digest.json``, written by ``skos.watchdog.publish.publish_digest``) is newer
    than 26h. This is the narrator-went-quiet signal and the one nobody notices
    on their own: a missing morning DM reads to a human as "nothing happened",
    not as "the thing that tells me what happened is broken". 26h matches the
    window skos' own scheduler probe uses (``operator_probe._SCHEDULER_MAX_AGE_S``),
    so a digest that runs daily has a real margin before it reads as quiet.
  * ``GradingBacklog``       the latest digest carries a ``GradingGap`` event whose
    ``meta.budget_exhausted`` is true, i.e. ``GRADE_RUN_BUDGET_S`` ran out mid-list
    and the run left replies ungraded. Deliberately NARROW: the same ``GradingGap``
    kind is also emitted when the grader was unreachable or a reply did not parse
    (``skos.watchdog.adapters.grading.GradingAdapter.collect``), and that is grader
    AVAILABILITY, not backlog. Only ``budget_exhausted`` means "there was more work
    than the run had time for". Do not widen this to any GradingGap: it would turn
    every skgateway blip into a backlog alarm and make the real signal worthless.

Observation is strictly READ-ONLY. The watchdog probes resolve skos' state root by
mirroring ``skos.watchdog.cursor.watchdog_home()``'s precedence rather than calling
it, because that function (and ``publish.digests_dir`` / ``latest_dir``) mkdirs as a
side effect: an operator that creates the store it is only supposed to look at
manufactures the state it reports on.

Fail-safe posture differs by half, on purpose. The scheduler/GTD probes fail SAFE
(healthy) when skos is unreachable, as they always have. The watchdog probes fail to
UNKNOWN, never to healthy: an unresolvable path, an absent digest, or an unparseable
one is exactly the "the narrator went quiet" case, so reporting it as fresh would
silence the signal this condition exists to raise. Unknown surfaces as stale in the
operator brief (``brief.build_brief``), which is the honest answer.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

CONDITIONS = [
    "SchedulerAlive",
    "GtdSinkDraining",
    "WatchdogDigestFresh",
    "GradingBacklog",
]

#: Condition types that indicate a PROBLEM when their status is "True" (the rest
#: are health-type and indicate a problem when "False"). ``GradingBacklog`` is a
#: problem type: a backlog EXISTS when it is True. The other three are health
#: types. The operator brief reads this polarity through ``loop.PROBLEM_WHEN_TRUE``;
#: without the declaration a backlog condition would be read upside down and fire
#: precisely when grading was healthy.
PROBLEM_WHEN_TRUE = frozenset({"GradingBacklog"})

#: A published digest older than this reads as "the narrator went quiet". Matches
#: skos' own scheduler staleness window (operator_probe._SCHEDULER_MAX_AGE_S).
_DIGEST_MAX_AGE_S = 26 * 3600

#: The published-digest filename (skos.watchdog.publish.DIGEST_JSON_NAME) and the
#: two path segments below the watchdog root (publish.digests_dir / latest_dir).
_DIGEST_JSON_NAME = "digest.json"
_DIGEST_SEGMENTS = ("digests", "latest")

#: The event kind the grading adapter emits for any ungraded replies, and the
#: meta flag that separates "ran out of time" (backlog) from "grader was
#: unavailable" (not backlog). See the module docstring.
_GRADING_GAP_KIND = "GradingGap"
_BUDGET_FLAG = "budget_exhausted"

#: Health-type conditions (fire when status is False): a dead scheduler or a
#: stalled GTD ingest sink both read as False -> firing.
_ACTIONS = [
    {
        "name": "restart_service",
        "standard": True,
        "reversible": True,
        "blast_radius": "low",
        "runbook": "restart the skscheduler service",
        "kedb_refs": [],
    },
    {
        "name": "replay_errors",
        "standard": True,
        "reversible": True,
        "blast_radius": "low",
        "runbook": "replay the skos error-recovery queue",
        "kedb_refs": [],
    },
]


def _b(value: bool) -> str:
    return "True" if value else "False"


def _tri(value: Optional[bool]) -> str:
    """Tri-state condition status. ``None`` is the honest Unknown (see the module
    docstring): it is NOT collapsed to healthy."""
    if value is None:
        return "Unknown"
    return _b(bool(value))


# --- read-only watchdog signal readers (unknown on any failure) --------------


def _watchdog_home() -> Optional[Path]:
    """skos' watchdog state root, resolved WITHOUT creating it.

    Mirrors ``skos.watchdog.cursor.watchdog_home()``'s precedence exactly
    (``SK_WATCHDOG_DIR`` > ``<SKCAPSTONE_HOME>/watchdog`` > ``~/.skcapstone/
    watchdog``) but never mkdirs, because observing must not manufacture the store
    it observes. An empty override falls back to the default rather than resolving
    against the cwd. Returns None when nothing resolvable (Unknown, not healthy).
    """
    try:
        env = (os.environ.get("SK_WATCHDOG_DIR") or "").strip()
        if env:
            return Path(env).expanduser()
        home = (os.environ.get("SKCAPSTONE_HOME") or "").strip()
        base = Path(home).expanduser() if home else Path.home() / ".skcapstone"
        return base / "watchdog"
    except Exception:
        return None


def _digest_path() -> Optional[Path]:
    """``<watchdog root>/digests/latest/digest.json``, the file the digest run
    publishes and a served host (and the Flutter Digest tab) fetches."""
    root = _watchdog_home()
    if root is None:
        return None
    return root.joinpath(*_DIGEST_SEGMENTS, _DIGEST_JSON_NAME)


def _read_digest() -> Optional[dict]:
    """The published digest as a dict, or None (Unknown) when it is absent,
    unreadable, not JSON, or not an object. Never raises, never writes, and never
    creates a parent dir on the way to looking."""
    p = _digest_path()
    if p is None:
        return None
    try:
        if not p.is_file():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _parse_iso(ts) -> Optional[float]:
    """Epoch seconds for an ISO8601 stamp (naive reads as UTC), or None."""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _digest_age_s(digest: Optional[dict], *, now: Optional[float] = None) -> Optional[float]:
    """Age of the published digest in seconds, or None when unknown.

    Prefers the digest's OWN window end (what the run actually covered) over the
    file's mtime, so a re-published stale digest cannot look fresh just because
    the bytes were rewritten. Falls back to mtime when the window is missing or
    unparseable.

    On the wire that field is spelled ``"to"``, not ``"until"``:
    ``skos.watchdog.port.Window`` names its attribute ``until`` but
    ``Window.to_dict()`` serialises it as ``{"from": since, "to": until}``, and
    every digest.json on disk carries ``{"from", "to"}``. Reading only ``until``
    therefore never matched, and this function fell through to mtime on every
    real digest, silently defeating the very staleness check it exists to make.
    Both spellings are accepted, ``until`` first, so a hand-written or future
    payload using the attribute name still works. skos' own
    ``operator_probe._window_end`` accepts the same pair; keep them in step.
    """
    if digest is None:
        return None
    published: Optional[float] = None
    window = digest.get("window")
    if isinstance(window, dict):
        published = _parse_iso(window.get("until"))
        if published is None:
            published = _parse_iso(window.get("to"))
    if published is None:
        p = _digest_path()
        try:
            published = p.stat().st_mtime if p is not None else None
        except OSError:
            published = None
    if published is None:
        return None
    return max(0.0, (time.time() if now is None else now) - published)


def _digest_events(digest: dict) -> Iterator[dict]:
    """Every event carried by a digest. ``assemble_digest`` puts problem- and
    notable-severity events in these two lists (info events are only counted, not
    carried), and ``GradingGap`` is emitted at notable severity."""
    for key in ("problems", "notable"):
        value = digest.get(key)
        if not isinstance(value, list):
            continue
        for event in value:
            if isinstance(event, dict):
                yield event


# --- pure probe logic (unit-tested directly) ---------------------------------


def _digest_fresh(age_s: Optional[float]) -> Optional[bool]:
    """The freshness rule. An unknown age stays UNKNOWN rather than collapsing to
    fresh: a digest nobody can read is the quiet-narrator case itself."""
    if age_s is None:
        return None
    return age_s <= _DIGEST_MAX_AGE_S


def _grading_backlog(digest: Optional[dict]) -> Optional[bool]:
    """True only when a ``GradingGap`` event says the run's time budget ran out.

    A GradingGap with ``budget_exhausted`` false (or absent) is grader
    AVAILABILITY, not backlog, and must not fire here (module docstring). The
    check is deliberately strict about the flag being a real boolean true.
    """
    if digest is None:
        return None
    for event in _digest_events(digest):
        if event.get("kind") != _GRADING_GAP_KIND:
            continue
        meta = event.get("meta")
        if isinstance(meta, dict) and meta.get(_BUDGET_FLAG) is True:
            return True
    return False


def _default_probe() -> dict:
    """Best-effort skos health.

    The scheduler/GTD halves DELEGATE to skos' own operator-facet contract
    (``skos.operator_probe``, the exact module ``skos operator observe`` runs)
    instead of maintaining a second, independent signal reader here. This is a
    fix for card 504d0046 (ATLAS Eyes PR #178 first run):

      * ``scheduler_alive`` used to shell out to ``skos scheduler status``, a
        subcommand that does not exist on the installed CLI (exit 2, "no such
        command"), which read as a confidently WRONG ``SchedulerAlive=False``
        regardless of whether the scheduler was actually alive.
      * ``gtd_draining`` was hardcoded ``None`` (never implemented), which read
        as ``GtdSinkDraining=Unknown`` even though the real signal (the GTD
        quarantine backlog depth) was available the whole time.

    Delegating to the real, tested probe (the cron-ledger newest-run age and the
    quarantine-backlog depth) makes this ONE real signal with two callers
    (in-process seat, out-of-process cli), so the two lanes cannot drift again
    short of the underlying probe itself changing behavior mid-flight. Both
    halves fail SAFE (healthy, matching their prior documented posture) when
    skos is not importable or the probe raises.

    The two watchdog halves are untouched: they already fail to UNKNOWN (None),
    never to healthy, and were never part of this conflict. Each half is read
    independently, so a failing scheduler probe never hides the digest reading
    and vice versa.
    """
    try:
        from skos.operator_probe import observe as _skos_operator_observe

        by_type = {c["type"]: c["status"] for c in _skos_operator_observe()["conditions"]}
        alive = by_type.get("SchedulerAlive") == "True"
        draining = by_type.get("GtdSinkDraining") == "True"
    except Exception:
        alive = None
        draining = None
    digest = _read_digest()
    return {
        "scheduler_alive": alive,
        "gtd_draining": draining,
        "digest_fresh": _digest_fresh(_digest_age_s(digest)),
        "grading_backlog": _grading_backlog(digest),
    }


def skos_explain() -> dict:
    """skos' self-description in the adapter-contract shape."""
    return {
        "kinds": ["scheduler", "gtd", "watchdog"],
        "conditions": list(CONDITIONS),
        "actions": [dict(a) for a in _ACTIONS],
    }


def skos_observe(probe: Callable[[], dict] | None = None) -> dict:
    """Read-only skos health snapshot in the adapter-contract shape."""
    st = (probe or _default_probe)()
    return {
        "conditions": [
            {
                "type": "SchedulerAlive",
                "status": _tri(st.get("scheduler_alive")),
                "object": "skscheduler",
            },
            {
                "type": "GtdSinkDraining",
                "status": _tri(st.get("gtd_draining")),
                "object": "gtd-sink",
            },
            {
                "type": "WatchdogDigestFresh",
                "status": _tri(st.get("digest_fresh")),
                "object": "watchdog-digest",
            },
            {
                "type": "GradingBacklog",
                "status": _tri(st.get("grading_backlog")),
                "object": "grading-loop",
            },
        ]
    }


#: A loop-compatible observe (paths, now_iso) -> {conditions}; ignores both.
def observe(paths=None, now_iso: str | None = None) -> dict:
    return skos_observe()


__all__ = ["skos_explain", "skos_observe", "observe", "CONDITIONS", "PROBLEM_WHEN_TRUE"]
