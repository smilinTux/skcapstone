"""Board reads and derived claim facts of the Mero blocker census.

Card 8fa7d8eb moved these methods verbatim from the single-module layout of
card 2516480b into a mixin the engine composes. Every joined signal class is
kept as its own evidence list; verdicts are never inferred from lifecycle
state or from links alone, and nothing here writes anything.
"""

from __future__ import annotations

from datetime import datetime, timezone

from skcapstone.card_store import Card, CardStore

from ._constants import (
    _BLOCKER_ACTIONS,
    _OUTCOME_KEY_RE,
    _SUCCESSOR_KEY_RE,
)
from ._helpers import _parse_ts, _verdict_head

__all__ = ["CensusReadsMixin"]


class CensusReadsMixin:
    """Read-side methods of :class:`MeroBlockerCensus`, split out for size."""

    # -- board reads ---------------------------------------------------------

    def _card_exists(self, cid: str) -> object:
        """Resolve a card id or 8-plus-hex reference, or None if unknown.

        Referents may cite a full id or a longer prefix; the store's ids are
        8 hex chars, so a longer citation resolves through its first 8.
        """
        store = CardStore(self.home)
        for candidate in (cid, cid[:8]):
            if not candidate:
                continue
            try:
                card = store.fold(candidate)
            except Exception:  # noqa: BLE001 - an unreadable target is not a card
                continue
            if card is not None:
                return card
        return None

    def _read_events(self, cid: str) -> list[dict]:
        """Parse every event line for a card through the store's reader."""
        store = CardStore(self.home)
        try:
            rows = store._read_events(cid)
        except Exception:  # noqa: BLE001 - unreadable evidence yields no rows
            return []
        return [row for row in rows if isinstance(row, dict)]

    def _card_facts(self, card: Card) -> dict:
        """Join every census signal for one card in a single event read.

        Verdict rows, blocker rows, claims, releases, voids, review receipts,
        dependency edits, progress, observations, and SKMail signals are kept
        as separate evidence lists. Nothing is inferred across stores here.
        """
        facts: dict = {
            "events": [],
            "claims": [],
            "releases": [],
            "verdict_rows": [],
            "blocker_rows": [],
            "void_rows": [],
            "review_rows": [],
            "dep_adds": [],
            "dep_removes": [],
            "progress_rows": [],
            "observation_rows": [],
            "successor_links": [],
        }
        events = self._read_events(card.id)
        facts["events"] = events
        for event in events:
            action = str(event.get("action") or "")
            if action == "claim":
                facts["claims"].append(event)
            elif action == "release_claim":
                facts["releases"].append(event)
            elif action == "void":
                facts["void_rows"].append(event)
            elif action in ("review_assignment_launch", "review_assignment_recommendation"):
                facts["review_rows"].append(event)
            elif action == "add_dependency":
                facts["dep_adds"].append(event)
            elif action == "remove_dependency":
                facts["dep_removes"].append(event)
            elif action in _BLOCKER_ACTIONS:
                facts["blocker_rows"].append(event)
            elif action in (
                "move",
                "describe",
                "amend_criteria",
                "set_priority",
                "priority",
                "add_label",
                "remove_label",
                "link",
            ):
                facts["progress_rows"].append(event)
            elif action == "mero_observation":
                facts["observation_rows"].append(event)
            if action in ("verdict", "evidence", "record_verdict") or _OUTCOME_KEY_RE.search(
                action
            ):
                verdict = event.get("verdict")
                if verdict is None and isinstance(event.get("outcome"), str):
                    verdict = event["outcome"]
                if isinstance(verdict, str) and verdict.strip():
                    facts["verdict_rows"].append(event)
            if action == "link" and _SUCCESSOR_KEY_RE.search(str(event.get("link_key") or "")):
                facts["successor_links"].append(event)
        facts["skmail"] = self._skmail_reader(card.id)
        facts["process"] = self._process_reader(card.id)
        facts["identity_fresh"] = bool(self._identity_reader(card.id))
        return facts

    # -- derived facts -------------------------------------------------------

    @staticmethod
    def _latest(rows: list[dict]) -> dict | None:
        """The newest row by parsed timestamp, falling back to list order."""
        best: dict | None = None
        best_key: tuple[datetime, int] | None = None
        for index, row in enumerate(rows):
            stamp = _parse_ts(row.get("ts"))
            key = (stamp or datetime.min.replace(tzinfo=timezone.utc), index)
            if best_key is None or key > best_key:
                best, best_key = row, key
        return best

    def _claim_state(self, card: Card, facts: dict) -> dict:
        """Claim liveness facts derived only from joined events."""
        claim = self._latest(facts["claims"])
        state = {
            "claim": claim,
            "claim_revision": str((claim or {}).get("claim_revision") or ""),
            "released": False,
            "progress_after_claim": False,
            "claim_age": None,
        }
        if claim is None:
            return state
        claim_ts = _parse_ts(claim.get("ts"))
        state["claim_age"] = self._now() - claim_ts if claim_ts is not None else None
        for release in facts["releases"]:
            release_ts = _parse_ts(release.get("ts"))
            if release_ts is None or claim_ts is None or release_ts >= claim_ts:
                state["released"] = True
                break
        for progress in facts["progress_rows"]:
            progress_ts = _parse_ts(progress.get("ts"))
            if progress_ts is not None and claim_ts is not None and progress_ts > claim_ts:
                state["progress_after_claim"] = True
                break
        return state

    @staticmethod
    def _outcome_rows(facts: dict) -> list[tuple[datetime, str, str]]:
        """Verdict rows as (parsed ts, verdict head, raw verdict), ordered."""
        rows: list[tuple[datetime, str, str]] = []
        for event in facts["verdict_rows"]:
            raw = str(event.get("verdict") or event.get("outcome") or "")
            stamp = _parse_ts(event.get("ts")) or datetime.min.replace(tzinfo=timezone.utc)
            rows.append((stamp, _verdict_head(raw), raw))
        rows.sort(key=lambda row: row[0])
        return rows

    def _latest_outcome(self, facts: dict) -> tuple[datetime, str, str] | None:
        rows = self._outcome_rows(facts)
        return rows[-1] if rows else None
