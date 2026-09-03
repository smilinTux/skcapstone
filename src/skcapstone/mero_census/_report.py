"""The typed result envelope of the Mero blocker census.

Card 8fa7d8eb moved ``CensusReport`` verbatim from the single-module layout
of card 2516480b. One report per run: bounds reached, findings due, what was
suppressed and why, and the selector-ready counts. No mutations live here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["CensusReport"]


@dataclass
class CensusReport:
    """The bounded result of one census run. No mutations are recorded here."""

    census_id: str
    observed_at: str
    cards_examined: int
    cards_total: int
    truncated: bool
    findings: list[dict] = field(default_factory=list)
    suppressed_unchanged: int = 0
    suppressed_by_bound: int = 0
    selector_ready: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)
