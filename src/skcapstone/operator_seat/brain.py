"""Hybrid-brain router and human report formatter for the operator seat (Seat O4b).

Pure routing from an observed brief to the cheap local pass or the decision
model, plus rendering a concise human report. No filesystem or network
access, no CLI wiring; callers own reading the brief and any wiring.
"""

from __future__ import annotations

from typing import Any


def route_brain(brief: dict[str, Any]) -> str:
    """Route a brief to the cheap local pass or the decision pass.

    Args:
        brief: Observed state for one app. Recognized key: ``quiet`` (bool),
            True when there is nothing firing and nothing ambiguous to
            reason about.

    Returns:
        ``"ornith"`` when ``brief["quiet"]`` is True, otherwise
        ``"claude"``.
    """
    return "ornith" if brief.get("quiet") else "claude"


def format_report(brief: dict[str, Any], proposals: list[dict[str, Any]]) -> str:
    """Format a concise human report from a brief and its proposals.

    Args:
        brief: Observed state for one app. Recognized keys: ``quiet``
            (bool), ``app`` (str, app name), and ``conditions`` (list of
            dicts with ``app``, ``type``, and ``status`` keys; a condition
            is firing when ``status`` is ``"True"``).
        proposals: Proposed changes. Recognized keys per entry:
            ``change_class`` (str), ``action`` (str), and ``rationale``
            (str, one line).

    Returns:
        A multi-line operator report, or "all quiet, no action" when the
        brief is quiet and there are no proposals.
    """
    if brief.get("quiet") and not proposals:
        return "all quiet, no action"

    # ``build_brief`` exposes already polarity-aware ``firing`` and ``stale``
    # lists.  Reading the old, nonexistent ``conditions`` field made reports
    # print "none" while health conditions were actively firing.
    firing = brief.get("firing")
    if firing is None:  # backwards-compatible input for direct formatter callers
        firing = [c for c in (brief.get("conditions") or []) if c.get("status") == "True"]
    stale = brief.get("stale") or []

    lines: list[str] = []

    lines.append("firing conditions:")
    if firing:
        for condition in firing:
            app = condition.get("app", "unknown")
            lines.append(f"  {app}: {condition.get('type')}={condition.get('status')}")
    else:
        lines.append("  none")

    lines.append("stale conditions:")
    if stale:
        for condition in stale:
            lines.append(f"  {condition.get('app', 'unknown')}: {condition.get('type')}=Unknown")
    else:
        lines.append("  none")

    lines.append("proposals:")
    if proposals:
        for proposal in proposals:
            change_class = proposal.get("change_class", "unknown")
            action = proposal.get("action", "unspecified")
            rationale = proposal.get("rationale", "")
            lines.append(f"  {change_class}, {action}, {rationale}")
    else:
        lines.append("  none")

    return "\n".join(lines)
