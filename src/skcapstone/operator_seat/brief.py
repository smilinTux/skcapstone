"""Operator brief builder for the operator seat (Seat O4a).

Merges per-app observe outputs into one operator brief describing what is
firing and what is quiet. This is triage plumbing only; it does not decide
on fixes. Pure, no filesystem or network access.
"""

from __future__ import annotations

from typing import Any


def build_brief(
    observations: dict[str, list[dict[str, Any]]], problem_types: set[str]
) -> dict[str, Any]:
    """Merge per-app conditions into a single operator brief.

    Args:
        observations: Maps app name to a list of condition dicts, each with
            ``type`` (str) and ``status`` (one of True/False/Unknown as str).
        problem_types: Condition types that indicate a problem when their
            status is ``True`` (e.g. ``CrashLooping``). Any type not in this
            set is treated as a health type, which fires when its status is
            ``False`` (e.g. ``Ready``).

    Returns:
        A dict with ``firing`` and ``stale`` lists of ``{app, type, status}``,
        ``quiet`` (bool, true when nothing is firing or stale), ``apps``
        (sorted list of app names), and ``counts`` (``firing``/``stale``
        totals).
    """
    firing: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []

    for app in sorted(observations):
        for condition in observations[app]:
            condition_type = condition["type"]
            status = condition["status"]
            entry = {"app": app, "type": condition_type, "status": status}
            # Carry the object identity through so downstream (the proposer) knows
            # WHICH object is firing, not just the condition type.
            if "object" in condition:
                entry["object"] = condition["object"]

            if status == "Unknown":
                stale.append(entry)
                continue

            is_problem_type = condition_type in problem_types
            is_firing = (is_problem_type and status == "True") or (
                not is_problem_type and status == "False"
            )
            if is_firing:
                firing.append(entry)

    return {
        "firing": firing,
        "stale": stale,
        "quiet": not firing and not stale,
        "apps": sorted(observations),
        "counts": {"firing": len(firing), "stale": len(stale)},
    }
