"""Stable, mutually exclusive scheduler eligibility reporting."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Mapping

from .card import Column, Kind


@dataclass(frozen=True)
class SchedulerFacts:
    """Storage-neutral facts supplied by the SKCapstone runtime adapter."""

    card_id: str
    malformed: bool = False
    lifecycle_excluded: bool = False
    selector_excluded: bool = False
    terminal_cardstore: bool = False
    terminal_itil: bool = False
    superseded: bool = False
    owner_health: str | None = None
    human_gate: bool = False
    foreign_project: bool = False
    not_claimable: bool = False
    sensitive_category: bool = False
    dependency: bool = False
    awaiting_review: bool = False
    backoff: bool = False
    attempt_limit: bool = False
    host_pin_elsewhere: bool = False
    adapter_facets: tuple[str, ...] = ()


@dataclass(frozen=True)
class SchedulerDecision:
    """One card's exclusive scheduler result and non-counted diagnostics."""

    card_id: str
    primary_reason: str
    eligible: bool
    facets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.eligible != (self.primary_reason == "ready"):
            raise ValueError("eligible must be true exactly when primary_reason is ready")


@dataclass(frozen=True)
class PoolV2:
    """A complete partition of the cards evaluated on one host."""

    host: str
    population: int
    ready: int
    ineligible: int
    reasons: dict[str, int]

    def render(self) -> str:
        reasons = json.dumps(self.reasons, sort_keys=True, separators=(",", ":"))
        return (
            f"POOL_V2|{self.host}|population={self.population} ready={self.ready} "
            f"ineligible={self.ineligible} reasons={reasons}"
        )


_PRECEDENCE = (
    ("malformed", lambda f: f.malformed),
    ("terminal_cardstore", lambda f: f.terminal_cardstore),
    ("terminal_itil", lambda f: f.terminal_itil),
    ("superseded", lambda f: f.superseded),
    ("lifecycle_excluded", lambda f: f.lifecycle_excluded),
    ("selector_excluded", lambda f: f.selector_excluded),
    ("owned_dead", lambda f: f.owner_health == "dead"),
    ("owned_stale", lambda f: f.owner_health == "stale"),
    ("owned_live", lambda f: f.owner_health == "live"),
    ("human_gate", lambda f: f.human_gate),
    ("foreign_project", lambda f: f.foreign_project),
    ("not_claimable", lambda f: f.not_claimable),
    ("sensitive_category", lambda f: f.sensitive_category),
    ("dependency", lambda f: f.dependency),
    ("awaiting_review", lambda f: f.awaiting_review),
    ("backoff", lambda f: f.backoff),
    ("attempt_limit", lambda f: f.attempt_limit),
    ("host_pin_elsewhere", lambda f: f.host_pin_elsewhere),
)


def classify_scheduler(facts: SchedulerFacts) -> SchedulerDecision:
    """Classify facts without reading storage or applying side effects."""
    matched = tuple(reason for reason, predicate in _PRECEDENCE if predicate(facts))
    if not matched:
        return SchedulerDecision(facts.card_id, "ready", True, facts.adapter_facets)
    facets = tuple(dict.fromkeys((*matched[1:], *facts.adapter_facets)))
    return SchedulerDecision(facts.card_id, matched[0], False, facets)


def classify_scheduler_population(
    population: Iterable[SchedulerFacts],
) -> tuple[SchedulerDecision, ...]:
    """Classify one adapter-produced population without changing it."""
    return tuple(classify_scheduler(facts) for facts in population)


def folded_revision(card) -> str:
    """Hash the complete canonical folded card projection."""
    payload = card.model_dump(mode="json")
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


def card_scheduler_facts(
    card,
    cards: Mapping[str, object],
    evidence_events: Iterable[dict] = (),
) -> SchedulerFacts:
    """Build scheduler facts from one fold plus its separate evidence stream.

    This is the shared read-only adapter for scheduler diagnostics. Verdicts are
    accepted only from explicit evidence events, never lifecycle or links alone.
    Invalid evidence fails closed as malformed.
    """
    labels = {str(label).strip().lower().replace("_", "-") for label in card.labels}
    title = str(card.title or "")
    malformed = not card.id.strip() or not title.strip() or title.strip().lower() == "x"
    verdict = None
    for event in evidence_events:
        if not isinstance(event, dict):
            malformed = True
            continue
        if event.get("action") != "link":
            continue
        key = str(event.get("link_key") or "").strip().lower().replace("-", "_")
        if key not in {"verdict", "outcome", "result"}:
            continue
        value = event.get("link_value")
        if not isinstance(value, str) or not re.match(
            r"^\s*(?:PASS(?:_FOR_[A-Z_]+)?|BLOCKED|FAIL)(?:\b|$)", value, re.I
        ):
            malformed = True
            continue
        verdict = value.strip().upper()
    missing_dependency = any(
        dependency not in cards or cards[dependency].status != Column.DONE
        for dependency in card.dependencies
    )
    human_gate = "human-gate" in labels or "[HUMAN]" in title.upper()
    not_claimable = bool(labels & {"not-claimable", "sprint-container", "do-not-claim"})
    owner_health = "live" if card.owner else None
    terminal = card.status == Column.DONE or card.archived
    awaiting_review = card.status == Column.REVIEW or bool(
        verdict and verdict.startswith("PASS_FOR_REVIEW")
    )
    return SchedulerFacts(
        card_id=card.id,
        malformed=malformed,
        terminal_cardstore=terminal,
        owner_health=owner_health,
        human_gate=human_gate,
        not_claimable=not_claimable or card.kind not in {Kind.TASK, Kind.EPIC},
        dependency=missing_dependency,
        awaiting_review=awaiting_review,
        backoff=bool(verdict and verdict.startswith("BLOCKED")),
    )


def pool_v2(host: str, decisions: Iterable[SchedulerDecision]) -> PoolV2:
    """Build a validated primary-reason partition."""
    rows = tuple(decisions)
    ids = [row.card_id for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("POOL_V2 requires one decision per card")
    ready = sum(row.eligible for row in rows)
    reasons = dict(sorted(Counter(row.primary_reason for row in rows if not row.eligible).items()))
    ineligible = sum(reasons.values())
    if len(rows) != ready + ineligible:
        raise ValueError("POOL_V2 partition invariant failed")
    return PoolV2(host, len(rows), ready, ineligible, reasons)
