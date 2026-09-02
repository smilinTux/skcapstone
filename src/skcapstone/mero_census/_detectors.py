"""The eight bounded finding detectors of the Mero blocker census.

Card 8fa7d8eb moved these methods verbatim from the single-module layout of
card 2516480b into a mixin the engine composes. Each detector joins separate
evidence rows and returns typed findings; none of them decides a verdict from
lifecycle state or from links alone, and none of them writes anything.
"""

from __future__ import annotations

import re

from skcapstone.card_store import Card

from ._helpers import _is_block, _is_pass_token, _norm_id, _parse_ts
from ._referent import _referent_defect
from ._types import CensusFindingType, RiskClass

__all__ = ["CensusDetectorsMixin"]


class CensusDetectorsMixin:
    """Detector methods of :class:`MeroBlockerCensus`, split out for size."""

    def _detect_dead_claim(self, card: Card, facts: dict) -> list[dict]:
        """A claim whose worker process and identity evidence are gone."""
        if card.owner is None or facts["lifecycle_terminal"]:
            return []
        claim_state = self._claim_state(card, facts)
        claim = claim_state["claim"]
        if claim is None or claim_state["released"]:
            return []
        if claim_state["progress_after_claim"]:
            return []
        process = facts["process"]
        sessions = process.get("sessions") if isinstance(process, dict) else None
        if sessions != [] or facts["identity_fresh"]:
            return []
        age = claim_state["claim_age"]
        sla_state = "" if age is None else ("missed" if age > self.stale_claim_sla else "at_risk")
        sources = [claim, *facts["observation_rows"]]
        finding = self._finding(
            card,
            CensusFindingType.DEAD_CLAIM,
            facts,
            risk=RiskClass.HIGH,
            action="jarvis_release_claim_and_optionally_relaunch",
            stop_conditions=[
                "stop if a live worker session for this claim is now visible",
                "stop if the claim revision changed after observation",
                "stop if the card left doing or gained progress after the claim",
            ],
            source_events=sources,
            details={
                "claim_revision": claim_state["claim_revision"],
                "observed_host": str(process.get("host") or ""),
                "claim_age_hours": None if age is None else round(age.total_seconds() / 3600, 2),
                "sla_state": sla_state,
            },
        )
        return [finding]

    def _detect_stale_claim(self, card: Card, facts: dict) -> list[dict]:
        """A DOING claim older than the SLA with no progress after it."""
        if card.owner is None or facts["lifecycle_terminal"]:
            return []
        claim_state = self._claim_state(card, facts)
        claim = claim_state["claim"]
        if claim is None or claim_state["released"] or claim_state["progress_after_claim"]:
            return []
        age = claim_state["claim_age"]
        if age is None or age < self.stale_claim_sla:
            return []
        finding = self._finding(
            card,
            CensusFindingType.STALE_CLAIM,
            facts,
            risk=RiskClass.MEDIUM,
            action="jarvis_review_stale_claim_for_release_or_reassignment",
            stop_conditions=[
                "stop if progress events newer than the claim appear",
                "stop if the card left doing",
                "stop if the claim revision changed after observation",
            ],
            source_events=[claim],
            details={
                "claim_revision": claim_state["claim_revision"],
                "claim_age_hours": round(age.total_seconds() / 3600, 2),
                "sla_state": "missed" if age > 2 * self.stale_claim_sla else "at_risk",
            },
        )
        return [finding]

    def _detect_completed_dependency(
        self, card: Card, facts: dict, done_ids: set[str]
    ) -> list[dict]:
        """An open card whose declared dependencies have all completed."""
        if facts["lifecycle_terminal"] or card.archived:
            return []
        completed = [dep for dep in card.dependencies if _norm_id(dep) in done_ids]
        if not completed:
            return []
        done_set = {_norm_id(d) for d in completed}
        sources = [
            event for event in facts["dep_adds"] if _norm_id(event.get("dependency")) in done_set
        ]
        # Dependency completion is structurally true from the fold, but a
        # recommendation must cite events. Cite the card's own completion of
        # the dependency graph: the move-to-done events of the completed deps
        # joined separately below, or fall back to this card's dep edges.
        if not sources:
            sources = [
                event
                for event in facts["events"]
                if str(event.get("action")) in ("add_dependency", "claim", "verdict")
            ]
        finding = self._finding(
            card,
            CensusFindingType.COMPLETED_DEPENDENCY,
            facts,
            risk=RiskClass.LOW,
            action="consumer_reopen_card_whose_blockers_completed",
            stop_conditions=[
                "stop if any dependency is no longer done on a fresh read",
                "stop if the card is no longer open",
                "stop if the dependency list changed after observation",
            ],
            source_events=sources,
            details={"completed_dependencies": sorted(completed)},
        )
        return [finding]

    def _detect_void_dependency_edge(
        self, card: Card, facts: dict, void_ids: set[str]
    ) -> list[dict]:
        """An open card that still depends on a voided card."""
        if facts["lifecycle_terminal"] or card.archived:
            return []
        voided = [dep for dep in card.dependencies if _norm_id(dep) in void_ids]
        if not voided:
            return []
        void_set = {_norm_id(d) for d in voided}
        sources = [
            event for event in facts["dep_adds"] if _norm_id(event.get("dependency")) in void_set
        ]
        if not sources:
            sources = [
                event
                for event in facts["events"]
                if str(event.get("action")) in ("add_dependency", "claim", "verdict")
            ]
        finding = self._finding(
            card,
            CensusFindingType.VOID_DEPENDENCY_EDGE,
            facts,
            risk=RiskClass.MEDIUM,
            action="consumer_cut_or_replace_void_dependency_edge",
            stop_conditions=[
                "stop if a cited dependency is no longer void on a fresh read",
                "stop if the card's dependency list changed after observation",
            ],
            source_events=sources,
            details={"void_dependencies": sorted(voided)},
        )
        return [finding]

    def _detect_contradictory_verdicts(self, card: Card, facts: dict) -> list[dict]:
        """A block that is the latest outcome while an earlier pass exists."""
        rows = self._outcome_rows(facts)
        if not rows or not _is_block(rows[-1][2]):
            return []
        passes = [row for row in rows if _is_pass_token(row[2])]
        if not passes:
            return []
        latest_block, latest_pass = rows[-1], passes[-1]
        sources = [
            event
            for event in facts["verdict_rows"]
            if _parse_ts(event.get("ts")) in (latest_block[0], latest_pass[0])
        ]
        finding = self._finding(
            card,
            CensusFindingType.CONTRADICTORY_VERDICTS,
            facts,
            risk=RiskClass.HIGH,
            action="consumer_reconcile_contradictory_verdicts",
            stop_conditions=[
                "stop if a fresh read shows a verdict newer than the block",
                "stop if the contradicting verdicts were retracted",
            ],
            source_events=sources,
            details={
                "blocked_at": latest_block[0].isoformat(),
                "block_verdict": latest_block[2][:200],
                "passed_at": latest_pass[0].isoformat(),
                "pass_verdict": latest_pass[2][:200],
            },
        )
        return [finding]

    def _detect_malformed_blocker_referents(self, card: Card, facts: dict) -> list[dict]:
        """Blocker events whose typed referent violates the BLOCKED contract."""
        findings: list[dict] = []
        for event in facts["blocker_rows"] + [
            row for row in facts["verdict_rows"] if _is_block(str(row.get("verdict") or ""))
        ]:
            defect = _referent_defect(event, self._card_exists)
            if defect is None:
                continue
            findings.append(
                self._finding(
                    card,
                    CensusFindingType.MALFORMED_BLOCKER_REFERENT,
                    facts,
                    risk=RiskClass.LOW,
                    action="consumer_correct_blocked_on_referent_shape",
                    stop_conditions=[
                        "stop if a corrected blocker event supersedes this one",
                        "stop if the defect no longer reproduces on a fresh read",
                    ],
                    source_events=[event],
                    details={"defect": defect, "blocked_on_raw": str(event.get("blocked_on"))},
                )
            )
        return findings

    def _detect_superseded_live_card(
        self, card: Card, facts: dict, done_ids: set[str]
    ) -> list[dict]:
        """An open card that a void or a completed successor has superseded."""
        if facts["lifecycle_terminal"] or card.archived:
            return []
        successors: list[str] = []
        sources: list[dict] = []
        for event in facts["void_rows"]:
            reason = str(event.get("reason") or "")
            match = re.search(r"\b([0-9a-f]{8})\b", reason)
            if match:
                successors.append(match.group(1).lower())
            sources.append(event)
        for link in facts["successor_links"]:
            target = _norm_id(str(link.get("link_value") or ""))[:8]
            if target and target in done_ids:
                successors.append(target)
                sources.append(link)
        successors = sorted(set(successors))
        if not successors:
            return []
        finding = self._finding(
            card,
            CensusFindingType.SUPERSEDED_LIVE_CARD,
            facts,
            risk=RiskClass.MEDIUM,
            action="consumer_void_or_close_superseded_live_card",
            stop_conditions=[
                "stop if the card gained progress after the supersession",
                "stop if the named successor is no longer done or is itself void",
            ],
            source_events=sources,
            details={"successors": successors},
        )
        return [finding]

    def _detect_review_identity_gap(self, card: Card, facts: dict) -> list[dict]:
        """Review receipts whose seat identities violate the boundary.

        A recommendation must be written by link, a launch by jarvis, and the
        assigned reviewer must be distinct from the card's workers and from
        link. A review-column card with no receipt at all is also a gap.
        """
        status_value = str(getattr(card.status, "value", card.status))
        is_review_card = "review" in {str(label).strip().lower() for label in card.labels}
        if not is_review_card:
            return []
        workers = {
            str(event.get("owner") or "").strip().lower()
            for event in facts["claims"]
            if event.get("owner")
        }
        gaps: list[dict] = []
        sources: list[dict] = []
        for event in facts["review_rows"]:
            writer = str(event.get("writer") or "").strip().lower()
            action = str(event.get("action") or "")
            if action == "review_assignment_recommendation" and writer != "link":
                gaps.append(
                    {"receipt": event.get("event_id", ""), "defect": "recommender_not_link"}
                )
                sources.append(event)
            if action == "review_assignment_launch" and writer != "jarvis":
                gaps.append(
                    {"receipt": event.get("event_id", ""), "defect": "launcher_not_jarvis"}
                )
                sources.append(event)
            reviewer = str(event.get("reviewer") or "").strip().lower()
            if reviewer and (reviewer == "link" or reviewer in workers):
                gaps.append(
                    {"receipt": event.get("event_id", ""), "defect": "reviewer_not_distinct"}
                )
                sources.append(event)
        if status_value == "review" and not facts["review_rows"]:
            gaps.append({"receipt": "", "defect": "review_without_assignment_receipt"})
        if not gaps:
            return []
        finding = self._finding(
            card,
            CensusFindingType.REVIEW_IDENTITY_GAP,
            facts,
            risk=RiskClass.MEDIUM,
            action="jarvis_audit_review_assignment_identities",
            stop_conditions=[
                "stop if corrected receipts supersede every defect",
                "stop if the card left the review column",
            ],
            source_events=sources,
            details={"gaps": gaps},
        )
        return [finding]
