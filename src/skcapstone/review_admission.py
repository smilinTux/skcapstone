"""Shared, read-only governed-review admission diagnostics."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

LOGICAL_REVIEWER_SEATS = frozenset({"link", "mero", "seraph"})

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
_COMBINED_BLOCKED_ON_RE = re.compile(
    r"^\s*(dependency|card|human|capability)\s+"
    r"(?:referent\s*[=:]\s*)?(card:[0-9a-f]{8}|approval:[\w./:@-]+|"
    r"ac:\d+|free|capability:[\w./:@-]+)\s*$",
    re.IGNORECASE,
)
_CARD_REFERENT_RE = re.compile(r"^card:([0-9a-f]{8})$", re.IGNORECASE)
_REVIEW_GENERATION_KEYS = (
    "producer_identity",
    "candidate_evidence_sha256",
    "link_source_card",
    "link_head_revision",
)
_OUTCOME_KEY_RE = re.compile(r"verdict|outcome|result|disposition|review_decision", re.IGNORECASE)
_TERMINAL_REVIEW_RE = re.compile(
    r"^\s*(?:PASS(?=\s*(?::|$))|FAIL(?=\s*(?::|_|$))|" r"BLOCKED(?=\s|:|_|\||$))",
    re.IGNORECASE,
)
_ITIL_DISPATCH_HOLDS = {
    "incident": frozenset({"detected"}),
    "problem": frozenset({"known_error"}),
}


@dataclass(frozen=True)
class ReviewGenerationEligibility:
    """Eligibility of the currently bound independent-review generation."""

    applicable: bool
    eligible: bool
    reason: str | None = None


def _review_generation(values: Mapping[str, object]) -> tuple[str, str, str, str] | None:
    """Return one fully typed candidate generation, or ``None``."""
    producer = str(values.get("producer_identity") or "").strip()
    evidence = str(values.get("candidate_evidence_sha256") or "").strip().lower()
    source = str(values.get("link_source_card") or "").strip()
    head = str(values.get("link_head_revision") or "").strip().lower()
    if (
        not producer
        or not source
        or not _DIGEST_RE.fullmatch(evidence)
        or not re.fullmatch(r"[0-9a-f]{40}", head)
    ):
        return None
    return producer, evidence, source, head


def _review_outcome_value(event: Mapping[str, object]) -> str:
    """Return an outcome-shaped value from one event, if present."""
    action = str(event.get("action") or "")
    if action == "link":
        if not _OUTCOME_KEY_RE.search(str(event.get("link_key") or "")):
            return ""
        return str(event.get("link_value") or "")
    if action not in {"verdict", "blocked", "evidence"}:
        return ""
    for key in ("verdict", "outcome", "result", "disposition", "review_decision", "value"):
        value = event.get(key)
        if value is not None:
            return str(value)
    return ""


def _explicit_itil_dispatch_hold(core: Mapping[str, object]) -> bool:
    """Return whether a non-task ITIL projection is explicitly parked."""
    meta = core.get("meta") if isinstance(core.get("meta"), Mapping) else {}
    raw_kind = core.get("kind") or meta.get("kind")
    kind = str(getattr(raw_kind, "value", raw_kind) or "").strip().lower()
    status = str(meta.get("itil_status") or "").strip().lower()
    return status in _ITIL_DISPATCH_HOLDS.get(kind, ())


def review_generation_eligibility(
    core: Mapping[str, object],
    labels: Sequence[str],
    events: Sequence[Mapping[str, object]],
) -> ReviewGenerationEligibility:
    """Return whether the current exact review generation may be launched.

    PASS, FAIL-family, BLOCKED, and completion events retire only the fully typed
    candidate generation present when the event was recorded. Reopening or
    rewriting the same binding cannot revive it; a different exact binding can.
    Explicit non-task ITIL workflow holds are also ineligible. Ordinary cards
    remain outside this gate.
    """
    if _explicit_itil_dispatch_hold(core):
        return ReviewGenerationEligibility(
            applicable=True,
            eligible=False,
            reason="itil-dispatch-hold",
        )
    if "review" not in {str(label).strip().lower() for label in labels}:
        return ReviewGenerationEligibility(applicable=False, eligible=True)

    links = core.get("links") if isinstance(core.get("links"), Mapping) else {}
    meta = core.get("meta") if isinstance(core.get("meta"), Mapping) else {}
    values = {key: links.get(key) or meta.get(key) for key in _REVIEW_GENERATION_KEYS}
    terminal: set[tuple[str, str, str, str]] = set()
    ordered = sorted(
        events,
        key=lambda event: (
            str(event.get("ts") or ""),
            str(event.get("writer") or ""),
            int(event.get("seq") or 0),
            str(event.get("event_id") or ""),
        ),
    )
    for event in ordered:
        action = str(event.get("action") or "")
        if action == "link":
            key = str(event.get("link_key") or "").strip().lower().replace("-", "_")
            if key in _REVIEW_GENERATION_KEYS:
                values[key] = event.get("link_value")
        generation = _review_generation(values)
        if generation is None:
            continue
        if action == "complete" or _TERMINAL_REVIEW_RE.match(_review_outcome_value(event)):
            terminal.add(generation)

    current = _review_generation(values)
    if current is not None and current in terminal:
        return ReviewGenerationEligibility(
            applicable=True,
            eligible=False,
            reason="terminal-review-generation",
        )
    return ReviewGenerationEligibility(applicable=True, eligible=True)


def card_review_generation_eligibility(store, card) -> ReviewGenerationEligibility:
    """Evaluate one folded card against its immutable core and full event union."""
    raw_core = store._load_core(card.id)  # CardStore has no public raw-core/event reader.
    if isinstance(raw_core, Mapping):
        core = dict(raw_core)
        core.setdefault("links", {})
        core.setdefault("meta", card.meta)
    else:
        core = {"links": card.links, "meta": card.meta}
    core.setdefault("kind", getattr(card.kind, "value", card.kind))
    events = [*store._read_events(card.id), *store._legacy_events(card.id)]
    return review_generation_eligibility(core, card.labels, events)


def parse_blocked_on_link(value: object) -> tuple[str, str | None] | None:
    """Return ``(category, referent)`` from a blocked_on link value.

    Args:
        value: Raw ``blocked_on`` link text.

    Returns:
        Category and optional referent, or None when the value is not actionable.
    """
    text = str(value or "").strip()
    if not text:
        return None
    lower = text.lower()
    if lower in {"dependency", "card", "human", "capability"}:
        return lower, None
    combined = _COMBINED_BLOCKED_ON_RE.match(text)
    if combined:
        return combined.group(1).lower(), combined.group(2).lower()
    return None


def dependency_blocker_unresolved(
    home: Path,
    core: Mapping[str, object],
    labels: Sequence[str] | None = None,
) -> bool:
    """True when a hashed dependency blocker still makes redispatch unsafe.

    A machine-readable ``blocked_on=dependency`` plus ``evidence_sha256`` parks
    the exact review generation until the named dependency publishes reachable
    candidate bytes or completes with a non-BLOCKED verdict. Temporary labels
    such as ``do-not-claim`` are not required once this gate is live.

    Args:
        home: Estate home containing CardStore and evidence roots.
        core: Folded card core/links/meta mapping.
        labels: Optional labels; unused for the hold itself, kept for callers.

    Returns:
        True when claim/dispatch must stay suppressed.
    """
    del labels  # Hold is driven by typed blocker links, not temporary fences.
    links = core.get("links") if isinstance(core.get("links"), Mapping) else {}
    meta = core.get("meta") if isinstance(core.get("meta"), Mapping) else {}
    blocked_on = links.get("blocked_on") or meta.get("blocked_on")
    evidence_sha = links.get("evidence_sha256") or meta.get("evidence_sha256")
    parsed = parse_blocked_on_link(blocked_on)
    if parsed is None or parsed[0] != "dependency" or not parsed[1]:
        return False
    if not _DIGEST_RE.fullmatch(str(evidence_sha or "").strip()):
        return False
    match = _CARD_REFERENT_RE.fullmatch(parsed[1])
    if match is None:
        return True
    dep_id = match.group(1).lower()
    from .card_store import CardStore

    dependency = CardStore(Path(home).expanduser()).fold(dep_id)
    if dependency is None:
        return True
    dep_links = dependency.links if isinstance(dependency.links, Mapping) else {}
    dep_meta = dependency.meta if isinstance(dependency.meta, Mapping) else {}
    verdict = str(
        dep_links.get("verdict")
        or dep_links.get("outcome")
        or dep_meta.get("verdict")
        or dep_meta.get("outcome")
        or ""
    )
    if (
        dependency.status.value == "done"
        and verdict
        and not re.match(r"^\s*BLOCKED\b", verdict, re.IGNORECASE)
    ):
        return False
    work = Path(home).expanduser() / "evidence" / "work" / dep_id
    if work.is_dir():
        try:
            # Bounded: only the dependency's own work root, shallow enough that
            # resolution means published bytes rather than an estate walk.
            for path in work.iterdir():
                if path.is_symlink():
                    continue
                if path.is_file():
                    return False
                if path.is_dir():
                    for child in path.iterdir():
                        if child.is_file() and not child.is_symlink():
                            return False
        except OSError:
            return True
    return True


def qualified_reviewer_seats(core: Mapping[str, object] | None = None) -> frozenset[str]:
    """Return default and configured logical reviewer seats, failing closed."""
    meta = core.get("meta") if isinstance(core, Mapping) else {}
    raw = meta.get("qualified_reviewer_seats", []) if isinstance(meta, dict) else []
    if not isinstance(raw, list):
        return LOGICAL_REVIEWER_SEATS
    configured = {
        str(value).strip().casefold()
        for value in raw
        if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9-]*", value.strip())
    }
    return LOGICAL_REVIEWER_SEATS | configured


def governed_review_seat(
    labels: Sequence[str], qualified_seats: frozenset[str] = LOGICAL_REVIEWER_SEATS
) -> str | None:
    """Return one supported logical review seat, or fail closed with ``None``."""
    seats = {
        str(label).strip().lower().removeprefix("seat-")
        for label in labels
        if str(label).strip().lower().startswith("seat-")
    }
    admitted = seats & qualified_seats
    return next(iter(admitted)) if len(seats) == len(admitted) == 1 else None


def reviewer_capacity(
    home: Path,
    core: Mapping[str, object],
    labels: Sequence[str],
    producer: str,
    reviewer: str,
) -> tuple[int, int]:
    """Return occupied and total qualified SKGateway slots for this card."""
    evaluation = reviewer_capacity_evaluation(home, core, labels, producer, reviewer)
    return sum(evaluation["occupancy"].values()), int(evaluation["logical_available"])


def reviewer_capacity_evaluation(
    home: Path,
    core: Mapping[str, object],
    labels: Sequence[str],
    producer: str,
    reviewer: str,
) -> dict[str, object]:
    """Return the revisioned capacity decision shared by claim and diagnostics."""
    from .fleet.review_capacity import (
        evaluate_review_capacity,
    )

    title = str(core.get("title") or "")
    title_sizes = re.findall(r"\[(S|M|L|XL)\]", title)
    label_sizes = {
        value
        for value, route in {"S": "sk-s", "M": "sk-m", "L": "sk-l", "XL": "sk-xl"}.items()
        if route in {str(label).strip().lower() for label in labels}
    }
    size = (
        title_sizes[0]
        if title and len(title_sizes) == 1
        else next(iter(label_sizes)) if not title and len(label_sizes) == 1 else None
    )
    path = home / "evidence" / "fleet-review-routes.json"
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        snapshot = {}
    return evaluate_review_capacity(
        snapshot,
        size or "",
        labels,
        producer,
        reviewer,
        declared_seat=governed_review_seat(labels, qualified_reviewer_seats(core)),
        qualified_seats=qualified_reviewer_seats(core),
    )


def reviewer_candidate_reasons(
    identity: str,
    *,
    producer: str,
    declared_seat: str | None = None,
    qualified_seats: set[str] | frozenset[str] = LOGICAL_REVIEWER_SEATS,
    available: bool = True,
    capacity_available: bool = True,
    expected_claim_revision: str | None = None,
    current_claim_revision: str | None = None,
) -> tuple[str, ...]:
    """Evaluate one reviewer with the same host-neutral facts used by placement."""
    normalized = identity.strip().casefold().replace("_", "-")
    seat = str(declared_seat or "").strip().casefold()
    if not seat:
        matches = {
            candidate
            for candidate in qualified_seats
            if normalized == candidate
            or normalized.startswith(candidate + "-")
            or ("-" + candidate + "-") in ("-" + normalized + "-")
        }
        seat = next(iter(matches)) if len(matches) == 1 else ""
    reasons: list[str] = []
    if not seat or seat not in qualified_seats:
        reasons.append("unqualified-reviewer")
    producer_normalized = producer.strip().casefold().replace("_", "-")
    producer_seat = {
        candidate
        for candidate in qualified_seats
        if producer_normalized == candidate
        or producer_normalized.startswith(candidate + "-")
        or ("-" + candidate + "-") in ("-" + producer_normalized + "-")
    }
    if normalized == producer_normalized or (seat and producer_seat == {seat}):
        reasons.append("producer-self-review")
    if not available:
        reasons.append("reviewer-unavailable")
    if not capacity_available:
        reasons.append("capacity")
    if expected_claim_revision != current_claim_revision and (
        expected_claim_revision is not None or current_claim_revision is not None
    ):
        reasons.append("stale-generation")
    return tuple(reasons)


def governed_review_metadata(
    core: Mapping[str, object], labels: Sequence[str]
) -> tuple[str, str] | None:
    """Return typed producer evidence for an explicitly labelled review."""
    normalized = {str(label).strip().lower() for label in labels}
    if "review" not in normalized:
        return None
    links = core.get("links") if isinstance(core.get("links"), dict) else {}
    meta = core.get("meta") if isinstance(core.get("meta"), dict) else {}
    producer = str(links.get("producer_identity") or meta.get("producer_identity") or "").strip()
    evidence = (
        str(links.get("candidate_evidence_sha256") or meta.get("candidate_evidence_sha256") or "")
        .strip()
        .lower()
    )
    if producer or evidence:
        return (
            (producer, evidence) if producer and re.fullmatch(r"[0-9a-f]{64}", evidence) else None
        )
    description = str(core.get("description") or "")
    producer_match = re.search(r"Producer identity:\s*([^.]*)\.", description)
    evidence_match = re.search(r"sha256=([0-9a-f]{64})(?:\.|\s|$)", description)
    if not producer_match or not producer_match.group(1).strip() or not evidence_match:
        return None
    return producer_match.group(1).strip(), evidence_match.group(1)


def governed_review_source_binding(
    core: Mapping[str, object], labels: Sequence[str]
) -> tuple[str, str] | None:
    """Return the immutable source card and revision for Seraph review work."""
    if "review" not in {str(label).strip().lower() for label in labels}:
        return None
    links = core.get("links") if isinstance(core.get("links"), dict) else {}
    meta = core.get("meta") if isinstance(core.get("meta"), dict) else {}
    source = str(links.get("link_source_card") or meta.get("link_source_card") or "").strip()
    head = str(links.get("link_head_revision") or meta.get("link_head_revision") or "").strip()
    if not source or not re.fullmatch(r"[0-9a-f]{40}", head):
        return None
    return source, head


def governed_review_gate_reasons(
    core: Mapping[str, object],
    labels: Sequence[str],
    *,
    dependency_blocked: bool = False,
    owned: bool = False,
    capacity_available: bool = True,
    dependency_blocker_holds: bool = False,
) -> tuple[str, ...]:
    """Return stable reason codes used by CLI and POOL_V2 diagnostics."""
    normalized = {str(label).strip().lower() for label in labels}
    if "review" not in normalized:
        return ()
    reasons = []
    if governed_review_seat(tuple(normalized), qualified_reviewer_seats(core)) is None:
        reasons.append("wrong-seat")
    if governed_review_metadata(core, labels) is None:
        reasons.append("absent-typed-metadata")
    if governed_review_source_binding(core, labels) is None:
        reasons.append("absent-source-binding")
    if dependency_blocked:
        reasons.append("dependency")
    if dependency_blocker_holds:
        reasons.append("dependency-blocker")
    if owned:
        reasons.append("ownership")
    if not capacity_available:
        reasons.append("capacity")
    return tuple(reasons)


def assert_governed_review_claim(home: Path, card_id: str, agent: str) -> None:
    """Fail closed when a SKCapstone claim bypasses governed review admission."""
    from .card_store import CardStore

    store = CardStore(home)
    card = store.fold(card_id)
    if card is None:
        return
    generation = card_review_generation_eligibility(store, card)
    if not generation.eligible and generation.reason:
        raise ValueError("claim denied: " + generation.reason)
    labels = [str(label).strip().lower() for label in card.labels]
    if "review" not in labels:
        return
    core = {
        "title": card.title,
        "description": card.description,
        "links": card.links,
        "meta": card.meta,
    }
    reasons = governed_review_gate_reasons(
        core,
        labels,
        dependency_blocker_holds=dependency_blocker_unresolved(home, core, labels),
    )
    reviewer = agent.strip().lower()
    elastic = re.fullmatch(
        rf"pi-codex-review-[a-z0-9][a-z0-9-]*-{re.escape(card_id.lower())}", reviewer
    )
    metadata = governed_review_metadata(core, labels)
    producer = metadata[0] if metadata else ""
    capacity = reviewer_capacity_evaluation(home, core, labels, producer, reviewer)
    candidate_reasons = reviewer_candidate_reasons(
        reviewer,
        producer=producer,
        declared_seat=(
            governed_review_seat(labels, qualified_reviewer_seats(core)) if elastic else None
        ),
        qualified_seats=qualified_reviewer_seats(core),
        capacity_available=capacity["reason"] == "eligible",
    )
    if "unqualified-reviewer" in candidate_reasons:
        reasons = ("wrong-reviewer", *reasons)
    reasons = (
        *(reason for reason in candidate_reasons if reason != "unqualified-reviewer"),
        *reasons,
    )
    if reasons:
        raise ValueError("governed review claim denied: " + ", ".join(dict.fromkeys(reasons)))
