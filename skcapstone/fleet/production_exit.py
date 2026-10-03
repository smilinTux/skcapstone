"""Keep native producer custody until independent review and completion."""

import os
import re
import time
from pathlib import Path

from skcoord.card_store import CardStore, card_mutation_lock
from skcoord.coordination import Board, _board_mutation_lock

from ..blocked_verdict import is_blocked_verdict, validate_blocked_verdict
from ..seraph_review_cardstore import _latest_outcome
from .source_bundle import MAX_EVIDENCE, _binding, _read, _root, _sha, publish_source
from .source_transport import _authority


def release_blocked(home: Path, request: dict, owner: str, claim: str) -> dict | None:
    """After caller-proven death, release only a valid exact typed BLOCKED generation."""
    card_id = request["card_id"]
    store = CardStore(home)
    row = store.fold(card_id)
    if row is None or not is_blocked_verdict(
        "verdict", _latest_outcome(store, card_id).get("verdict")
    ):
        return None
    try:
        # Preserve native lock order and execute the same locked Board mutation.
        with _board_mutation_lock(home), card_mutation_lock(home, card_id):
            row = store.fold(card_id)
            outcome = _latest_outcome(store, card_id)
            verdict = outcome.get("verdict", "")
            if (
                row is None
                or "source-only" not in row.labels
                or row.owner != owner
                or getattr(row.status, "value", row.status) in {"done", "archived"}
                or row.archived
                or row.meta.get("_claim_revision") != claim
                or outcome.get("action") != "verdict"
                or outcome.get("writer") != owner
                or outcome.get("expected_claim_revision") not in (None, claim)
                or not is_blocked_verdict("verdict", verdict)
            ):
                return None
            validate_blocked_verdict("verdict", verdict)
            for key in ("repository", "base_revision"):
                if _binding({"meta": row.meta, "links": row.links}, key) != request.get(key):
                    return None
            claims = [
                event
                for event in store._read_events(card_id)
                if event.get("action") == "claim"
                and (event.get("claim_revision") or event.get("event_id")) == claim
            ]
            if len(claims) != 1 or str(outcome.get("ts", "")) < str(claims[0].get("ts", "")):
                return None
            if any(
                not re.fullmatch(r"[0-9a-f]{40}", str(outcome.get(key, "")))
                for key in ("candidate_commit", "candidate_tree")
            ):
                return None
            if not re.fullmatch(
                r"refs/heads/[A-Za-z0-9][A-Za-z0-9._/-]{0,180}",
                str(outcome.get("candidate_ref", "")),
            ):
                return None
            evidence = Path(str(outcome.get("candidate_path", "")))
            if _root(home, card_id).parent not in evidence.parents or _sha(
                _read(evidence, MAX_EVIDENCE)
            ) != outcome.get("candidate_sha256"):
                return None
            released = Board(home)._release_claim_locked(
                owner, card_id, actor=owner, expected_claim_revision=claim
            )
            if not released:
                return None
            return {
                "state": "blocked",
                "claim_released": True,
                "process_terminal": True,
                "reason": verdict,
                "outcome_event": outcome.get("event_id"),
            }
    except (OSError, ValueError, KeyError, TypeError):
        return None


def retry_disposition(args, terminal_check) -> dict | None:
    """Bound publication retries and recheck process/cgroup death before each attempt."""
    prior = getattr(args, "production_source_disposition", None)
    if prior is not None and prior["state"] != "awaiting-evidence":
        return prior
    for delay in (0, 5, 10):
        if getattr(args, "production_publication_attempts", 0) >= 3:
            break
        if delay:
            time.sleep(delay)
        args.production_publication_attempts = (
            getattr(args, "production_publication_attempts", 0) + 1
        )
        if hasattr(args, "production_source_disposition"):
            del args.production_source_disposition
        prior = disposition(args, terminal_proven=terminal_check())
        if prior is None or prior["state"] != "awaiting-evidence" or not prior["process_terminal"]:
            break
    return prior


def disposition(args, *, terminal_proven: bool) -> dict | None:
    """Publish stopped production source only; leave reviewer/legacy exits alone."""
    if "SKFLEET_PRODUCTION_POLICY" not in os.environ:
        return None
    existing = getattr(args, "production_source_disposition", None)
    if existing is not None:
        return existing
    state = {
        "state": "awaiting-evidence",
        "claim_released": False,
        "process_terminal": terminal_proven,
    }
    try:
        home = Path.home() / ".skcapstone"
        card = CardStore(home).fold(args.card)
        if card is not None and (
            "review" in {str(label).lower() for label in card.labels}
            or "[REVIEW]" in card.title.upper()
            or card.meta.get("link_source_card")
            or card.links.get("link_source_card")
        ):
            return None
        if not terminal_proven:
            state["reason"] = "exact-process-and-cgroup-death-unproven"
        else:
            repository = getattr(args, "source_repository", "")
            base = getattr(args, "source_base_revision", "")
            if not repository or not base:
                raise ValueError("exact source binding missing")
            if card is None or "source-only" not in card.labels:
                raise ValueError("source-only authorization missing")
            _authority()
            request = {"card_id": args.card, "repository": repository, "base_revision": base}
            blocked = release_blocked(home, request, args.owner, args.claim_revision)
            if blocked is not None:
                args.production_source_disposition = blocked
                return blocked
            artifact = publish_source(
                home,
                request,
                args.owner,
                args.claim_revision,
                Path.cwd().resolve(),
            )
            state = {
                "state": "awaiting-review",
                "claim_released": False,
                "process_terminal": True,
                "source_artifact": artifact,
            }
    except (OSError, ValueError, KeyError, TypeError):
        state["reason"] = "exact-typed-candidate-publication-pending"
    args.production_source_disposition = state
    return state
