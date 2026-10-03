"""Read-only recurring blocker census and typed recommendations for Mero.

Card 2516480b extends Mero from one-shot observation to a recurring, bounded
census. Mero reads CardStore lifecycle, blocker attributes, worker joins,
review joins, and SKMail signals, then emits typed append-only
recommendations. Jarvis or another explicitly fenced consumer performs any
mutation; Mero never does.

WHY A CENSUS MODULE AND NOT MORE ROTATION LOGIC. The rotation decides what to
launch and runs on every host every cycle. The census only answers "what on
the board is stuck, and what should a consumer consider doing about it", so it
lives beside the other board-repair modules (``blocker_referent``) rather than
inside the scheduler.

BOUNDED. One run examines at most ``MAX_CARDS_EXAMINED`` cards and emits at
most ``MAX_FINDINGS_PER_RUN`` recommendations, so a pathological board cannot
make the census unbounded.

TYPED, APPEND-ONLY OUTPUT. Each finding becomes one
``mero_blocker_recommendation`` event carrying the card id, the card revision,
the claim revision, the blocker generation, the source events joined, evidence
hashes, a risk class, the proposed consumer action, and stop conditions. The
payload is built with ``json.dumps`` and every line is parsed with
``json.loads`` before use; nothing is ever concatenated into JSON.

STRUCTURAL JOIN, NOT LIFECYCLE INFERENCE. A verdict is never inferred from
lifecycle state or from links alone. Verdict events, blocker events, worker
claims, review receipts, and SKMail signals are joined as separate evidence
rows, and every finding cites the exact events it joined.

DEDUPLICATION. ``recommendation_id`` is a deterministic digest of the finding
without its observation timestamp, and it is passed as the CardStore
``transition_id``, so re-running the census on an unchanged board appends
nothing. A finding re-emits only when its authoritative generation changes
(state, claim, blocker evidence, or verdict set) or when its SLA is missed.

AUTHORITY. Every emission calls ``require_authority("mero",
Action.RECOMMEND)``. Mero holds exactly OBSERVE and RECOMMEND; claim, release,
launch, stop, reassign, rotate, repair, merge, deploy, card creation and card
mutation, selector reruns, and any read of protected data are all
outside its seat. The negative tests prove each refusal.

LAYOUT NOTE (card 8fa7d8eb). The 1027-line single module of card 2516480b is
decomposed, behavior preserved, into this package: ``_types`` (the finding
and risk enums), ``_constants`` (bounds, SLAs, and board vocabulary),
``_helpers`` (pure parsing, digest, and serializer/parser functions),
``_referent`` (the BLOCKED-contract referent judge), ``_reads`` (board reads
and derived claim facts), ``_detectors`` (the eight finding detectors),
``_census`` (the engine: bounds, dedupe, run, emission), and this facade.
Every public name of the old module is re-exported here unchanged, so
``from skcapstone import mero_census`` keeps working for every existing
consumer and test.
"""

from __future__ import annotations

from pathlib import Path

from ._census import MeroBlockerCensus
from ._constants import (
    DEFAULT_RECOMMENDATION_SLA,
    DEFAULT_STALE_CLAIM_SLA,
    MAX_CARDS_EXAMINED,
    MAX_FINDINGS_PER_RUN,
    RECOMMENDATION_EVENT,
    RECOMMENDATION_SCHEMA,
    RISK_ORDER,
)
from ._helpers import (
    parse_recommendation_line,
    recommendation_event_to_json,
)
from ._report import CensusReport
from ._types import CensusFindingType
from ._types import RiskClass as RiskClass

__all__ = [
    "CensusFindingType",
    "CensusReport",
    "MeroBlockerCensus",
    "MAX_CARDS_EXAMINED",
    "MAX_FINDINGS_PER_RUN",
    "DEFAULT_STALE_CLAIM_SLA",
    "DEFAULT_RECOMMENDATION_SLA",
    "RECOMMENDATION_EVENT",
    "RECOMMENDATION_SCHEMA",
    "RISK_ORDER",
    "recommendation_event_to_json",
    "parse_recommendation_line",
    "run_blocker_census",
]


def run_blocker_census(
    home: Path,
    *,
    emit: bool = False,
    actor: str = "mero",
    **kwargs: object,
) -> CensusReport:
    """Run one census and optionally append its recommendations.

    With ``emit=False`` (the default) the census is a pure read: no event, no
    lifecycle change, no mail, no mutation of any kind.
    """
    census = MeroBlockerCensus(home, **kwargs)  # type: ignore[arg-type]
    report = census.run()
    if emit:
        census.emit(report, actor=actor)
    return report
