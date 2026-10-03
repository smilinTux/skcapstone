"""Expose Chef's review opener for one exact owned, typed source generation."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

from skcoord.card_store import CardStore, card_mutation_lock

from .link_review_work import (
    ReviewWorkResult,
    card_generation,
    reconcile_review_work,
)
from .seraph_review_cardstore import LiveCardStoreGateway, _binding, _latest_outcome


def _candidate(home: Path, card_id: str, outcome: dict) -> None:
    """Verify bounded shared candidate bytes without following a symlink."""
    path = Path(str(outcome.get("candidate_path") or ""))
    root = home.resolve() / "evidence" / "work" / card_id
    try:
        if (
            not path.is_absolute()
            or path.resolve(strict=True) != path
            or not path.is_relative_to(root)
        ):
            raise ValueError("guarded review candidate path invalid")
        for field in ("candidate_commit", "candidate_tree"):
            if not re.fullmatch(r"[0-9a-f]{40}", str(outcome.get(field) or "")):
                raise ValueError("guarded review candidate source invalid")
        if not re.fullmatch(r"refs/heads/[^\s]+", str(outcome.get("candidate_ref") or "")):
            raise ValueError("guarded review candidate ref invalid")
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("guarded review candidate not regular")
            data = stream.read(262145)
        if (
            not data
            or len(data) > 262144
            or hashlib.sha256(data).hexdigest() != outcome.get("candidate_sha256")
        ):
            raise ValueError("guarded review candidate bytes changed")
    except OSError as exc:
        raise ValueError("guarded review candidate unavailable") from exc


def open_guarded_review(
    home: Path,
    card_id: str,
    producer: str,
    *,
    expected_source_revision: str,
    expected_claim_revision: str,
) -> ReviewWorkResult:
    """Create/reconcile one independent review without claiming or launching it.

    The source lock is shared with native claim and verdict writes. The existing
    Link helper owns review identity and reviewer preflight. Provider admission,
    actual worker liveness, test execution and completion remain separate gates.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", expected_source_revision):
        raise ValueError("guarded review source revision invalid")
    if not re.fullmatch(r"[0-9a-f]{32}", expected_claim_revision):
        raise ValueError("guarded review claim revision invalid")
    with card_mutation_lock(home, card_id):
        store = CardStore(home)
        source = store.fold(card_id)
        snapshot = LiveCardStoreGateway(home).read_card(card_id)
        if (
            source is None
            or source.owner != producer
            or source.meta.get("_claim_revision") != expected_claim_revision
            or source.meta.get("claim_conflicts")
            or source.archived
            or source.status.value not in {"ready", "doing", "review"}
            or "source-only" not in source.labels
            or "review" in source.labels
            or snapshot.revision != expected_source_revision
        ):
            raise ValueError("guarded review source or claim changed")
        outcome = _latest_outcome(store, card_id)
        if (
            outcome.get("action") != "verdict"
            or outcome.get("writer") != producer
            or outcome.get("verdict") not in {"PASS_FOR_REVIEW", "PASS_FOR_REREVIEW"}
        ):
            raise ValueError("guarded review typed provisional outcome required")
        _candidate(home, card_id, outcome)
        generation = card_generation(source)
        digest = outcome["candidate_sha256"]
        from .review_replacement import replacement_binding

        lineage = replacement_binding(home, card_id, outcome["candidate_commit"])
        review_id = lineage.pop("review_card_id")
        binding = {
            "source_revision": expected_source_revision,
            "producer_identity": producer,
            "candidate_evidence_sha256": digest,
            **{key: outcome[key] for key in ("candidate_path", "candidate_tree", "candidate_ref")},
            **lineage,
        }
        existing = store.fold(review_id)
        if existing is not None and any(
            existing.meta.get(key) != value for key, value in binding.items()
        ):
            raise ValueError("guarded review existing generation conflict")
        item = {
            "source_card": card_id,
            "source_owner": producer,
            "head_revision": outcome["candidate_commit"],
            "card_generation": generation,
            "base_revision": _binding(source, "base_revision"),
            "base_ref": _binding(source, "base_ref"),
            "workspace_repository": _binding(source, "repository"),
            "reviewer_candidates": [{"identity": "pi-seraph-fiber-" + review_id}],
            **binding,
        }
        result = reconcile_review_work(home, item, evidence_sha256=digest)
        # A cross-host sync can still arrive despite this host's advisory lock.
        # Refuse dispatch on drift; preserve any already-created review for audit.
        if LiveCardStoreGateway(home).read_card(card_id).revision != expected_source_revision:
            raise ValueError("guarded review source changed during reconciliation")
        _candidate(home, card_id, outcome)
        if (
            replacement_binding(home, card_id, outcome["candidate_commit"]).get("review_card_id")
            != review_id
        ):
            raise ValueError("guarded review replacement changed during reconciliation")
        if not result.launchable:
            raise ValueError("guarded review refused: " + result.reason)
        return result
