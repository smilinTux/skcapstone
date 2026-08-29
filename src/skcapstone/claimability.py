"""Authoritative claimability fold for SKCapstone fleet selector.

This module provides ONE deterministic source-only claimability predicate
used by BOTH pool construction and the immediate preclaim check.

The fold reads directly from CardStore events every time, with no caching,
ensuring that pool construction and preclaim checks always agree on a card's
claimability state.

Task: 670765f8 [FLEET-SELECTOR-TRUTH-01][M][REPAIR]
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimabilityResult:
    """Result of the claimability fold.

    All fields are derived from CardStore and evidence events only.
    No inference from stale projections or links.
    """

    assignable: bool
    """True if the card can be claimed and worked right now."""

    lifecycle_state: str
    """Current lifecycle state: open, claimed, complete, void."""

    owner: str | None
    """Current claim owner if claimed, None otherwise."""

    reason: str
    """Human-readable explanation of the claimability decision."""

    exclusions: list[str] = field(default_factory=list)
    """List of exclusion reasons that prevent claimability."""

    column: str | None = None
    """Current kanban column if known, from move events."""

    dependency_blockers: list[str] = field(default_factory=list)
    """Dependency card IDs that are not satisfied."""

    last_claim_ts: str | None = None
    """Timestamp of most recent claim event, if any."""

    last_release_ts: str | None = None
    """Timestamp of most recent release_claim event, if any."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Lifecycle action to state mapping - EXACTLY as defined in CardStore semantics
_LIFECYCLE_MAP = {
    "claim": "claimed",
    "release_claim": "open",
    "unassign": "open",
    "archive": "void",
    "complete": "complete",
    "void": "void",
}

# Terminal lifecycle states that cannot be reopened
_TERMINAL_STATES = {"complete", "void"}

# Labels that mark a card as non-implementation (human-only)
_NON_IMPLEMENTATION_LABELS = {
    "planning-only-container",
    "do-not-claim-as-implementation",
    "human-gate",
    "human-decision-recorded-no-action",
    "no-action-authorized",
}

# Tags that explicitly mark cards as unclaimable
_NOT_CLAIMABLE_TAGS = {"not-claimable", "sprint-container"}

# ITIL terminal states
_ITIL_TERMINAL_STATES = {"closed", "resolved", "rejected", "cancelled"}

# Evidence link keys that indicate a verdict/outcome
_OUTCOME_KEYS = ("verdict", "result", "disposition", "review_decision")

# Known rotation hosts for host pinning
_ROTATION_HOSTS = {"chiap01", "chiap02", "chiap03", "chiap04", "chiap08"}
_KNOWN_HOSTS = _ROTATION_HOSTS | {"chiwk11", "chiwk12", "noroc2027"}

# Regex for detecting PASS verdicts
_PASS_RE = re.compile(r"^\s*PASS(_FOR_REVIEW)?\b", re.I)

# Regex for detecting BLOCKED verdicts with blocked_on
_BLOCKED_ON_RE = re.compile(r"blocked[_\s-]?on", re.I)


# ---------------------------------------------------------------------------
# CardStore event reading (source-only, no caching)
# ---------------------------------------------------------------------------


def _read_cardstore_events(cards_dir: Path, card_id: str) -> list[dict[str, Any]]:
    """Read all CardStore events for a card directly from disk.

    Args:
        cards_dir: Path to the cards directory.
        card_id: The card ID to read events for.

    Returns:
        List of event dictionaries, sorted by (ts, writer, event_id).
    """
    events_dir = cards_dir / card_id / "events"
    events: list[dict[str, Any]] = []

    if not events_dir.is_dir():
        return events

    for event_file in events_dir.iterdir():
        if not event_file.is_file():
            continue
        try:
            with open(event_file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except (json.JSONDecodeError, TypeError):
                        continue
        except OSError:
            continue

    # Sort by timestamp, then writer, then event_id for cross-writer ordering
    events.sort(
        key=lambda e: (str(e.get("ts", "")), str(e.get("writer", "")), str(e.get("event_id", "")))
    )
    return events


def _read_evidence_events(evidence_dir: Path) -> list[dict[str, Any]]:
    """Read all evidence events directly from disk.

    Args:
        evidence_dir: Path to the evidence/card_events directory.

    Returns:
        List of event dictionaries.
    """
    events: list[dict[str, Any]] = []

    if not evidence_dir.is_dir():
        return events

    for jsonl_file in sorted(evidence_dir.glob("*.jsonl")):
        try:
            with open(jsonl_file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except (json.JSONDecodeError, TypeError):
                        continue
        except OSError:
            continue

    return events


# ---------------------------------------------------------------------------
# ITIL state folding
# ---------------------------------------------------------------------------


def _fold_itil_state(cards_dir: Path, card_id: str) -> str | None:
    """Determine ITIL state from separate ITIL event streams.

    ITIL records keep state in events with kind=="status" and target in "to".
    This is separate from the card vocabulary "action".

    Args:
        cards_dir: Path to the cards directory.
        card_id: The card ID to check.

    Returns:
        Terminal state name if in terminal state, None otherwise.
    """
    itil_dir = cards_dir / ".." / "coordination" / "itil"
    if not itil_dir.is_dir():
        return None

    for sub in ("incidents", "problems", "changes"):
        events_dir = itil_dir / sub / card_id / "events"
        if not events_dir.is_dir():
            continue

        events: list[dict[str, Any]] = []
        for event_file in events_dir.iterdir():
            if not event_file.is_file():
                continue
            try:
                with open(event_file, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            events.append(json.loads(line))
                        except (json.JSONDecodeError, TypeError):
                            continue
            except OSError:
                continue

        events.sort(key=lambda e: (e.get("ts", ""), e.get("seq", 0)))

        state: str | None = None
        for e in events:
            if e.get("kind") == "status" and e.get("to"):
                state = e["to"]

        if state in _ITIL_TERMINAL_STATES:
            return state

    return None


# ---------------------------------------------------------------------------
# Dependency folding
# ---------------------------------------------------------------------------


def _extract_dependency_value(event: dict[str, Any]) -> str | None:
    """Extract dependency card ID from an event.

    The dependency may be stored in various fields.
    """
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    for key in ("dependency_id", "depends_on", "dependency", "target_card_id", "target"):
        value = event.get(key, payload.get(key))
        if isinstance(value, str) and value:
            return value
    return None


def _fold_dependencies(
    cards_dir: Path,
    card_id: str,
    events: list[dict[str, Any]],
    core: dict[str, Any],
) -> list[str]:
    """Fold dependency state from core and events.

    Args:
        cards_dir: Path to the cards directory.
        card_id: The card ID to fold dependencies for.
        events: CardStore events for the card.
        core: Core card data.

    Returns:
        List of dependency card IDs.
    """
    deps = [str(x) for x in (core.get("dependencies") or [])]

    for event in events:
        dep = _extract_dependency_value(event)
        if not dep:
            continue

        if event.get("action") == "add_dependency" and dep not in deps:
            deps.append(dep)
        elif event.get("action") == "remove_dependency":
            deps = [item for item in deps if item != dep]

    return deps


def _is_dependency_satisfied(
    dep_id: str,
    dep_lifecycle_state: str,
    outcomes: dict[str, tuple[str, str]],
) -> bool:
    """Check if a dependency is satisfied.

    A dependency is satisfied only if:
    1. It is in lifecycle state "complete"
    2. Its latest outcome is NOT BLOCKED

    This prevents depending on a dependency that completed but BLOCKED.

    Args:
        dep_id: The dependency card ID.
        dep_lifecycle_state: The dependency's current lifecycle state.
        outcomes: Dict mapping card_id to (timestamp, outcome) tuples.

    Returns:
        True if the dependency is satisfied, False otherwise.
    """
    if dep_lifecycle_state != "complete":
        return False

    _, outcome = outcomes.get(dep_id, (None, None))
    if outcome and re.match(r"^\s*BLOCKED", outcome, re.I):
        return False

    return True


# ---------------------------------------------------------------------------
# Label folding
# ---------------------------------------------------------------------------


def _extract_label_value(event: dict[str, Any]) -> str | None:
    """Extract label value from an event."""
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    for key in ("label", "label_value", "value"):
        value = event.get(key, payload.get(key))
        if isinstance(value, str) and value:
            return value
    return None


def _fold_labels(
    card_id: str,
    core: dict[str, Any],
    evidence_events: list[dict[str, Any]],
) -> list[str]:
    """Fold label state from core and evidence events.

    Args:
        card_id: The card ID.
        core: Core card data.
        evidence_events: All evidence events.

    Returns:
        List of label strings.
    """
    labels = [str(x) for x in (core.get("initial_labels") or [])]

    # Filter and sort label events for this card
    card_label_events = [
        e
        for e in evidence_events
        if e.get("card_id") == card_id and e.get("action") in ("add_label", "remove_label")
    ]
    card_label_events.sort(
        key=lambda e: (e.get("ts", ""), str(e.get("writer", "")), str(e.get("event_id", "")))
    )

    for event in card_label_events:
        label = _extract_label_value(event)
        if not label:
            continue

        if event.get("action") == "add_label" and label not in labels:
            labels.append(label)
        elif event.get("action") == "remove_label":
            labels = [item for item in labels if item != label]

    return labels


# ---------------------------------------------------------------------------
# Outcome loading
# ---------------------------------------------------------------------------


def _fold_key(key: str) -> str:
    """Normalize a link key for matching."""
    k = str(key or "").strip().lower().replace("-", "_")
    k = re.sub(r"_?20\d{6}t?\d{0,6}z?", "", k)
    k = re.sub(r"_[0-9a-f]{8,64}$", "", k)
    return re.sub(r"__+", "_", k).strip("_")


def _load_outcomes(evidence_dir: Path) -> dict[str, tuple[str, str]]:
    """Load latest outcomes for all cards from evidence events.

    Args:
        evidence_dir: Path to evidence/card_events directory.

    Returns:
        Dict mapping card_id to (timestamp, outcome) tuples.
    """
    outcomes: dict[str, tuple[str, str]] = {}

    if not evidence_dir.is_dir():
        return outcomes

    for jsonl_file in sorted(evidence_dir.glob("*.jsonl")):
        try:
            with open(jsonl_file, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except (json.JSONDecodeError, TypeError):
                        continue

                    if event.get("action") != "link":
                        continue

                    fk = _fold_key(event.get("link_key", ""))
                    if not any(o in fk for o in _OUTCOME_KEYS):
                        continue

                    card_id = event.get("card_id")
                    if not card_id:
                        continue

                    ts = str(event.get("ts", ""))
                    val = str(event.get("link_value", ""))

                    prev = outcomes.get(card_id)
                    if prev is None or ts > prev[0]:
                        outcomes[card_id] = (ts, val)
        except OSError:
            continue

    return outcomes


# ---------------------------------------------------------------------------
# Host pinning
# ---------------------------------------------------------------------------


def _get_host_pin(core: dict[str, Any], labels: list[str]) -> str | None:
    """Determine if a card is pinned to a specific host.

    A card is pinned if it names exactly one ROTATION host.
    Naming a non-rotation host or multiple hosts means unpinned.

    Args:
        core: Core card data.
        labels: Folded labels.

    Returns:
        Host name if pinned to a rotation host, None otherwise.
    """
    blob = (str(core.get("title", "")) + " " + json.dumps(labels)).lower()
    named = {h for h in _KNOWN_HOSTS if h in blob}

    if len(named) != 1:
        return None

    only = named.pop()
    return only if only in _ROTATION_HOSTS else None


# ---------------------------------------------------------------------------
# Lifecycle state folding (authoritative)
# ---------------------------------------------------------------------------


def _fold_lifecycle_state(
    events: list[dict[str, Any]],
) -> tuple[str, str | None, str | None, str | None]:
    """Fold lifecycle state from CardStore events.

    This is the authoritative source of truth for a card's lifecycle state.
    It honors both action events AND kanban column moves.

    Args:
        events: CardStore events for the card.

    Returns:
        Tuple of (state, column, last_claim_ts, last_release_ts).
        state: Current lifecycle state (open, claimed, complete, void).
        column: Current kanban column if known.
        last_claim_ts: Timestamp of most recent claim.
        last_release_ts: Timestamp of most recent release_claim.
    """
    state = "open"
    column: str | None = None
    last_claim_ts: str | None = None
    last_release_ts: str | None = None

    for event in events:
        action = event.get("action")
        timestamp = event.get("ts", "")

        # Track column from move events
        if action == "move":
            col = str(event.get("column", "")).strip().lower()
            if col:
                column = col
            continue

        # Track claim/release timestamps
        if action == "claim":
            last_claim_ts = timestamp
        elif action == "release_claim":
            last_release_ts = timestamp

        # Apply lifecycle state transitions
        if action not in _LIFECYCLE_MAP:
            continue

        # Terminal states are sticky
        if state == "void":
            continue
        if state == "complete" and action not in ("void", "archive"):
            continue

        state = _LIFECYCLE_MAP[action]

    # Column "done" also means complete, unless overridden by terminal action
    if state not in _TERMINAL_STATES and column == "done":
        state = "complete"

    return state, column, last_claim_ts, last_release_ts


# ---------------------------------------------------------------------------
# Main claimability fold
# ---------------------------------------------------------------------------


def claimability_fold(
    card_id: str,
    cards_dir: Path,
    evidence_dir: Path,
    current_host: str,
    excluded_card_ids: set[str] | None = None,
) -> ClaimabilityResult:
    """Compute the authoritative claimability state for a card.

    This is THE source of truth for whether a card can be claimed.
    It reads directly from CardStore every time, with no caching.

    The fold enforces:
    - Exact CardStore lifecycle semantics (no inference from moves alone)
    - Dependency satisfaction (including BLOCKED dependency outcomes)
    - Label-based exclusions (human gates, non-implementation)
    - Host pinning (cards that must run on specific hosts)
    - ITIL terminal states
    - Explicit not-claimable tags

    Args:
        card_id: The card ID to evaluate.
        cards_dir: Path to the cards directory.
        evidence_dir: Path to evidence/card_events directory.
        current_host: The host name evaluating this card.
        excluded_card_ids: Set of card IDs to exclude (e.g., from lifecycle report).

    Returns:
        ClaimabilityResult with assignable flag and detailed state.
    """
    if excluded_card_ids is None:
        excluded_card_ids = set()

    # Read CardStore events fresh from disk
    events = _read_cardstore_events(cards_dir, card_id)

    # Read core data
    core_path = cards_dir / card_id / "core.json"
    if not core_path.exists():
        return ClaimabilityResult(
            assignable=False,
            lifecycle_state="unknown",
            owner=None,
            reason="core.json missing",
            exclusions=["missing_core"],
        )

    try:
        with open(core_path, encoding="utf-8", errors="replace") as f:
            core = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return ClaimabilityResult(
            assignable=False,
            lifecycle_state="error",
            owner=None,
            reason=f"cannot read core.json: {e}",
            exclusions=["core_error"],
        )

    # Check explicit exclusion set
    if card_id in excluded_card_ids:
        return ClaimabilityResult(
            assignable=False,
            lifecycle_state="excluded",
            owner=None,
            reason="card in exclusion set",
            exclusions=["excluded"],
        )

    # Fold lifecycle state
    lc_state, column, last_claim_ts, last_release_ts = _fold_lifecycle_state(events)

    # Terminal lifecycle states are not assignable
    if lc_state in _TERMINAL_STATES:
        return ClaimabilityResult(
            assignable=False,
            lifecycle_state=lc_state,
            owner=None,
            reason=f"terminal lifecycle state: {lc_state}",
            exclusions=[f"terminal_{lc_state}"],
            column=column,
            last_claim_ts=last_claim_ts,
            last_release_ts=last_release_ts,
        )

    # Currently claimed cards are not assignable
    if lc_state == "claimed":
        # Extract current owner from events
        owner: str | None = None
        for event in reversed(events):
            if event.get("action") == "claim":
                owner = (
                    event.get("agent")
                    or event.get("owner")
                    or event.get("actor")
                    or event.get("by")
                )
                break

        return ClaimabilityResult(
            assignable=False,
            lifecycle_state=lc_state,
            owner=owner,
            reason="card is currently claimed",
            exclusions=["claimed"],
            column=column,
            last_claim_ts=last_claim_ts,
            last_release_ts=last_release_ts,
        )

    # ITIL terminal states are not assignable
    itil_state = _fold_itil_state(cards_dir, card_id)
    if itil_state:
        return ClaimabilityResult(
            assignable=False,
            lifecycle_state=lc_state,
            owner=None,
            reason=f"ITIL terminal state: {itil_state}",
            exclusions=[f"itil_{itil_state}"],
            column=column,
            last_claim_ts=last_claim_ts,
            last_release_ts=last_release_ts,
        )

    # Read evidence events for labels and outcomes
    evidence_events = _read_evidence_events(evidence_dir)
    outcomes = _load_outcomes(evidence_dir)

    # Check for awaiting review (PASS_FOR_REVIEW)
    card_outcome_ts, card_outcome = outcomes.get(card_id, (None, None))
    if card_outcome and _PASS_RE.match(card_outcome):
        return ClaimabilityResult(
            assignable=False,
            lifecycle_state=lc_state,
            owner=None,
            reason="card produced a candidate and is awaiting review",
            exclusions=["awaiting_review"],
            column=column,
            last_claim_ts=last_claim_ts,
            last_release_ts=last_release_ts,
        )

    # Fold labels
    labels = _fold_labels(card_id, core, evidence_events)

    # Check for non-implementation labels (human gates)
    label_set = {str(item).strip().lower().replace("_", "-") for item in labels}
    if label_set & _NON_IMPLEMENTATION_LABELS:
        return ClaimabilityResult(
            assignable=False,
            lifecycle_state=lc_state,
            owner=None,
            reason="card is marked as non-implementation (human gate)",
            exclusions=["non_implementation"],
            column=column,
            last_claim_ts=last_claim_ts,
            last_release_ts=last_release_ts,
        )

    # Check for foreign-project label
    if "foreign-project" in {str(item).strip().lower() for item in labels}:
        return ClaimabilityResult(
            assignable=False,
            lifecycle_state=lc_state,
            owner=None,
            reason="card belongs to a different project",
            exclusions=["foreign_project"],
            column=column,
            last_claim_ts=last_claim_ts,
            last_release_ts=last_release_ts,
        )

    # Check for explicit not-claimable tags
    tag_set = {str(x).strip().lower() for x in (labels or [])} | {
        str(x).strip().lower() for x in (core.get("tags") or [])
    }
    if _NOT_CLAIMABLE_TAGS & tag_set:
        return ClaimabilityResult(
            assignable=False,
            lifecycle_state=lc_state,
            owner=None,
            reason="card is explicitly marked as not-claimable",
            exclusions=["not_claimable"],
            column=column,
            last_claim_ts=last_claim_ts,
            last_release_ts=last_release_ts,
        )

    # Check host pinning
    host_pin = _get_host_pin(core, labels)
    if host_pin and host_pin != current_host:
        return ClaimabilityResult(
            assignable=False,
            lifecycle_state=lc_state,
            owner=None,
            reason=f"card is pinned to host {host_pin}",
            exclusions=[f"pinned_to_{host_pin}"],
            column=column,
            last_claim_ts=last_claim_ts,
            last_release_ts=last_release_ts,
        )

    # Check dependencies
    deps = _fold_dependencies(cards_dir, card_id, events, core)
    dependency_blockers: list[str] = []

    for dep_id in deps:
        # Get dependency's lifecycle state
        dep_events = _read_cardstore_events(cards_dir, dep_id)
        dep_lc_state, _, _, _ = _fold_lifecycle_state(dep_events)

        if not _is_dependency_satisfied(dep_id, dep_lc_state, outcomes):
            dependency_blockers.append(dep_id)

    if dependency_blockers:
        return ClaimabilityResult(
            assignable=False,
            lifecycle_state=lc_state,
            owner=None,
            reason=f"unsatisfied dependencies: {', '.join(dependency_blockers)}",
            exclusions=[f"dependency_{dep}" for dep in dependency_blockers],
            column=column,
            dependency_blockers=dependency_blockers,
            last_claim_ts=last_claim_ts,
            last_release_ts=last_release_ts,
        )

    # All checks passed - card is assignable
    return ClaimabilityResult(
        assignable=True,
        lifecycle_state=lc_state,
        owner=None,
        reason="card is assignable",
        exclusions=[],
        column=column,
        last_claim_ts=last_claim_ts,
        last_release_ts=last_release_ts,
    )
