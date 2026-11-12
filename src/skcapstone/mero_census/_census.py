"""The Mero blocker census engine: bounds, findings, dedupe, and emission.

Card 8fa7d8eb moved this class verbatim from the single-module layout of card
2516480b, composing the read and detector mixins. One run reads every joined
signal, produces due findings, and optionally emits them as typed,
append-only recommendation events. Mero holds exactly OBSERVE and RECOMMEND;
every other mutation is refused by the seat boundary.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from skcapstone.card_store import Card, CardStore
from skcapstone.seat_boundaries import Action, BoundaryError, require_authority

from ._constants import (
    DEFAULT_RECOMMENDATION_SLA,
    DEFAULT_STALE_CLAIM_SLA,
    MAX_CARDS_EXAMINED,
    MAX_FINDINGS_PER_RUN,
    RECOMMENDATION_EVENT,
    RECOMMENDATION_SCHEMA,
    RISK_ORDER,
)
from ._detectors import CensusDetectorsMixin
from ._helpers import (
    _canonical_digest,
    _event_ref,
    _generation_key,
    _is_block,
    _now,
    _parse_ts,
    parse_recommendation_line,
    recommendation_event_to_json,
)
from ._reads import CensusReadsMixin
from ._report import CensusReport
from ._types import CensusFindingType, RiskClass

__all__ = ["MeroBlockerCensus"]

#: Schema tag of the census-local pagination checkpoint (card eacedb1a).
PAGINATION_SCHEMA = "skfleet.mero-census-pagination/v1"


class MeroBlockerCensus(CensusReadsMixin, CensusDetectorsMixin):
    """One bounded read-only pass over the board, plus its typed emissions.

    The census reads lifecycle, blocker attributes, worker joins, review
    joins, and SKMail signals, joins them per card, and produces findings.
    Nothing is written during the pass. Emission is a separate, explicit,
    append-only step reserved to the Mero seat.

    Worker, identity, and SKMail joins are injected as callables so the census
    never touches process state or mailboxes implicitly. Defaults observe
    nothing, which yields no worker-dependent findings.

    PAGINATION (card eacedb1a). One run examines at most ``max_cards`` cards:
    a durable, census-local checkpoint at ``<home>/mero_census/
    pagination.json`` records the window position, so successive bounded runs
    sweep the whole sorted card id list and the tail beyond the cap is never
    permanently starved. The checkpoint is the run's only write; it touches
    no card event, lifecycle field, or mailbox, and it is committed
    atomically after the pass so a crash re-examines the same window.
    """

    def __init__(
        self,
        home: Path,
        *,
        now: Callable[[], datetime] | None = None,
        process_reader: Callable[[str], dict] | None = None,
        identity_reader: Callable[[str], bool] | None = None,
        skmail_reader: Callable[[str], list[dict]] | None = None,
        max_cards: int = MAX_CARDS_EXAMINED,
        max_findings: int = MAX_FINDINGS_PER_RUN,
        stale_claim_sla: timedelta = DEFAULT_STALE_CLAIM_SLA,
        recommendation_sla: timedelta = DEFAULT_RECOMMENDATION_SLA,
    ) -> None:
        self.home = Path(home)
        self._now = now or _now
        self._process_reader = process_reader or (lambda cid: {})
        self._identity_reader = identity_reader or (lambda cid: True)
        self._skmail_reader = skmail_reader or (lambda cid: [])
        self.max_cards = int(max_cards)
        self.max_findings = int(max_findings)
        self.stale_claim_sla = stale_claim_sla
        self.recommendation_sla = recommendation_sla

    # -- finding construction --------------------------------------------------

    def _finding(
        self,
        card: Card,
        finding_type: CensusFindingType,
        facts: dict,
        *,
        risk: RiskClass,
        action: str,
        stop_conditions: list[str],
        source_events: list[dict],
        details: dict | None = None,
    ) -> dict:
        """Pin one finding: card, revisions, generations, sources, and action.

        The recommendation id digests the finding without its observation
        timestamp, so an unchanged board yields the identical id and the
        CardStore dedupes it by transition id.
        """
        details = dict(details or {})
        claim_state = self._claim_state(card, facts)
        claim_revision = claim_state["claim_revision"]
        status_value = str(getattr(card.status, "value", card.status))
        card_revision = _generation_key(
            card.id, card.owner or "", status_value, str(card.updated_at or "")
        )
        evidence_hashes = [_canonical_digest(_event_ref(event)) for event in source_events]
        blocker_generation = _generation_key(
            card.id,
            card.owner or "",
            status_value,
            claim_revision,
            *sorted(evidence_hashes),
        )
        generation = _generation_key(
            card.id, finding_type.value, blocker_generation, str(details.get("sla_state") or "")
        )
        payload: dict = {
            "schema": RECOMMENDATION_SCHEMA,
            "card_id": card.id,
            "finding_type": finding_type.value,
            "observed_by": "mero",
            "card_revision": card_revision,
            "claim_revision": claim_revision,
            "blocker_generation": blocker_generation,
            "generation": generation,
            "risk_class": risk.value,
            "proposed_consumer_action": action,
            "stop_conditions": [str(s) for s in stop_conditions],
            "source_events": [_event_ref(event) for event in source_events],
            "evidence_sha256": evidence_hashes,
            "details": details,
        }
        payload["observed_at"] = self._now().isoformat()
        # The recommendation id digests the SEMANTIC finding: neither the
        # observation timestamp nor card_revision participates. Emitting a
        # recommendation appends an event, which bumps the card's updated_at;
        # folding card_revision into the id would make every census run
        # mint a fresh id for an unchanged finding and defeat dedupe. Card
        # state that matters to a finding is pinned in blocker_generation
        # (owner, status, claim revision, evidence hashes) instead.
        payload["recommendation_id"] = (
            "mrc-"
            + _canonical_digest(
                {k: v for k, v in payload.items() if k not in ("observed_at", "card_revision")}
            )[:32]
        )
        return payload

    # -- due findings ---------------------------------------------------------

    def _due_findings(self, findings: list[dict]) -> tuple[list[dict], int]:
        """Keep new findings and SLA-missed ones; drop unchanged duplicates.

        A finding is new when no recommendation with its id exists for the
        card. It re-emits when the stored generation differs (a new
        authoritative generation) or when its age exceeds the recommendation
        SLA. Everything else is an unchanged duplicate.
        """
        store = CardStore(self.home)
        prior: dict[str, dict] = {}
        cache: dict[str, dict[str, dict]] = {}
        due: list[dict] = []
        unchanged = 0
        for finding in findings:
            cid = finding["card_id"]
            if cid not in cache:
                rows: dict[str, dict] = {}
                try:
                    events = store._read_events(cid)
                except Exception:  # noqa: BLE001 - no readable history means new
                    events = []
                for event in events:
                    if isinstance(event, dict) and event.get("action") == RECOMMENDATION_EVENT:
                        rid = str(event.get("recommendation_id") or "")
                        prev = rows.get(rid)
                        if prev is None or str(event.get("ts") or "") >= str(prev.get("ts") or ""):
                            rows[rid] = event
                cache[cid] = rows
            prior = cache[cid]
            stored = prior.get(finding["recommendation_id"])
            if stored is None:
                due.append(finding)
                continue
            if str(stored.get("generation") or "") != finding["generation"]:
                due.append(finding)
                continue
            stamp = _parse_ts(stored.get("ts") or stored.get("observed_at"))
            if stamp is None or self._now() - stamp >= self.recommendation_sla:
                due.append(finding)
                continue
            unchanged += 1
        return due, unchanged

    # -- durable pagination (card eacedb1a) -------------------------------------

    def _pagination_path(self) -> Path:
        """The census-local checkpoint path. No card or lifecycle state lives here."""
        return self.home / "mero_census" / "pagination.json"

    def _load_pagination(self) -> dict:
        """Read the checkpoint, tolerating absence or corruption.

        A missing, unreadable, or malformed checkpoint restarts the sweep at
        position zero; a partial pass is never trusted from damaged state.
        """
        fresh = {"position": 0, "cycle": 0, "covered_in_pass": 0}
        try:
            raw = json.loads(self._pagination_path().read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - absent or corrupt state means start over
            return dict(fresh)
        if not isinstance(raw, dict) or raw.get("schema") != PAGINATION_SCHEMA:
            return dict(fresh)
        state = dict(fresh)
        for key in state:
            try:
                state[key] = max(0, int(raw.get(key, state[key])))
            except (TypeError, ValueError):
                state[key] = 0
        return state

    def _save_pagination(self, state: dict) -> None:
        """Commit the checkpoint atomically so a crash never skips a window."""
        path = self._pagination_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
        tmp.write_text(
            json.dumps(
                {"schema": PAGINATION_SCHEMA, **state},
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    # -- the run -------------------------------------------------------------

    def run(self) -> CensusReport:
        """One bounded census pass over the next pagination window.

        The board itself is read without writes. The run's only durable
        effect is the census-local pagination checkpoint, committed
        atomically after the pass: a crash before the commit re-examines the
        same window on the next run.
        """
        started = self._now()
        store = CardStore(self.home)
        all_ids = store.list_card_ids()
        total = len(all_ids)
        state = self._load_pagination()
        position = min(state["position"], total)
        window = all_ids[position : position + self.max_cards]
        next_position = position + len(window)
        pass_complete = total > 0 and next_position >= total
        committed = {
            "position": 0 if pass_complete else next_position,
            "cycle": state["cycle"] + (1 if pass_complete else 0),
            "covered_in_pass": 0 if pass_complete else state["covered_in_pass"] + len(window),
        }
        coverage = {
            "schema": PAGINATION_SCHEMA,
            "window_start": position,
            "window_end": next_position,
            "position": committed["position"],
            "cycle": committed["cycle"],
            "covered_in_pass": committed["covered_in_pass"],
            "cards_total": total,
            "pass_complete": pass_complete,
        }
        done_ids: set[str] = set()
        void_ids: set[str] = set()
        cards: list[Card] = []
        for cid in window:
            try:
                card = store.fold(cid)
            except Exception:  # noqa: BLE001 - unreadable cards are skipped, not faked
                continue
            if card is None or card.meta.get("unreadable"):
                continue
            cards.append(card)
            status_value = str(getattr(card.status, "value", card.status))
            if status_value == "done" and not card.archived:
                done_ids.add(card.id)
            # A void event is the store's structural supersession marker; the
            # fold turns it into archived + a voided meta flag. Collect every
            # voided card id, of any status, for void-edge detection.
            if card.archived or card.meta.get("voided"):
                for event in self._read_events(cid):
                    if event.get("action") == "void":
                        void_ids.add(cid)
                        break
        findings: list[dict] = []
        selector_ready = {"ready": 0, "blocked": 0, "total_open": 0}
        for card in cards:
            facts = self._card_facts(card)
            status_value = str(getattr(card.status, "value", card.status))
            # Voided cards fold to archived with a voided marker; both shapes
            # are terminal for this census.
            facts["lifecycle_terminal"] = (
                status_value == "done" or card.archived or bool(card.meta.get("voided"))
            )
            if not facts["lifecycle_terminal"]:
                selector_ready["total_open"] += 1
                outcome = self._latest_outcome(facts)
                if outcome is not None and _is_block(outcome[2]):
                    selector_ready["blocked"] += 1
                else:
                    selector_ready["ready"] += 1
            findings.extend(self._detect_dead_claim(card, facts))
            findings.extend(self._detect_stale_claim(card, facts))
            findings.extend(self._detect_completed_dependency(card, facts, done_ids))
            findings.extend(self._detect_void_dependency_edge(card, facts, void_ids))
            findings.extend(self._detect_contradictory_verdicts(card, facts))
            findings.extend(self._detect_malformed_blocker_referents(card, facts))
            findings.extend(self._detect_superseded_live_card(card, facts, done_ids))
            findings.extend(self._detect_review_identity_gap(card, facts))
        due, unchanged = self._due_findings(findings)
        risk_index = {name: index for index, name in enumerate(RISK_ORDER)}
        due.sort(
            key=lambda f: (risk_index.get(f["risk_class"], 99), f["card_id"], f["finding_type"])
        )
        suppressed_by_bound = max(0, len(due) - self.max_findings)
        counts: dict[str, int] = {}
        for finding in due[: self.max_findings]:
            counts[finding["finding_type"]] = counts.get(finding["finding_type"], 0) + 1
        self._save_pagination(committed)
        return CensusReport(
            census_id="mrc-"
            + _generation_key(started.isoformat(), self.home, len(cards), len(findings)),
            observed_at=started.isoformat(),
            cards_examined=len(cards),
            cards_total=total,
            truncated=total > len(window),
            findings=due[: self.max_findings],
            suppressed_unchanged=unchanged,
            suppressed_by_bound=suppressed_by_bound,
            selector_ready=selector_ready,
            counts=counts,
            coverage=coverage,
        )

    # -- emission ------------------------------------------------------------

    def emit(self, report: CensusReport, *, actor: str = "mero") -> list[dict]:
        """Append the report's findings as typed events. Mero seat only.

        Every payload is serialized with ``json.dumps`` and appended through
        ``CardStore.append_event``, which dedupes by the recommendation id.
        Nothing here claims, releases, launches, stops, merges, deploys,
        creates, mutates lifecycle state, reruns the selector, or touches
        protected data it has no authority over.
        """
        require_authority(actor, Action.RECOMMEND)
        if actor.strip().lower() != "mero":
            raise BoundaryError("only mero may emit census recommendations")
        store = CardStore(self.home)
        emitted: list[dict] = []
        for finding in report.findings:
            # The timestamp is deliberately NOT part of the recommendation id
            # digest: see _finding. Emitting the same finding twice produces
            # the same transition id, and the store returns the durable event.
            payload = dict(finding)
            line = recommendation_event_to_json(payload)
            parsed = parse_recommendation_line(line)
            card_id = str(parsed.pop("card_id"))
            event = store.append_event(
                card_id,
                RECOMMENDATION_EVENT,
                "mero",
                transition_id=str(parsed["recommendation_id"]),
                **parsed,
            )
            emitted.append(event)
        return emitted
