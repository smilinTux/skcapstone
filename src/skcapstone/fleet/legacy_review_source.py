"""Exact pre-dispatch source evidence for local hosted independent reviews."""

import hashlib
import os
import re
import stat
from pathlib import Path

from skcoord.card_store import CardStore

from ..seraph_review_cardstore import _latest_outcome
from .source_bundle import MAX_EVIDENCE, _binding


def _digest(path: Path, limit: int) -> str:
    """Hash bounded, owned regular legacy bytes, including old group-write mode.

    The board-pinned hash supplies integrity. Do not rewrite historical files
    or weaken the modern source-bundle permission contract.
    """
    if path.resolve() != path.absolute():
        raise ValueError("redirected legacy evidence")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("unsafe legacy evidence")
        raw = f.read(limit + 1)
    if not 0 < len(raw) <= limit:
        raise ValueError("legacy evidence exceeds bound")
    return hashlib.sha256(raw).hexdigest()


def verified_legacy_review_source(home: Path, core: dict, producer: str) -> bool:
    """Verify legacy custody without inventing a producer provider or launch.

    This permits only the existing local hosted review path. Native remote
    source manifests, current producer custody and source-only review contracts
    must continue through their own checks. The latest native verdict, its
    bytes and an independently hash-bound source receipt must all agree.
    """
    try:
        # Rotate passes the on-disk cards/<id>/core.json, which carries only
        # initial_labels and creation-time meta: labels and links written later
        # (review bindings, PR corrections) live in the fold. Reading the raw
        # core found no labels, so every legacy review stayed blocked on
        # source-provider-evidence-required (497f0f02, f519c41a, 2026-10-11).
        store = CardStore(home)
        folded = store.fold(str(core.get("id") or ""))
        if folded is None or folded.archived:
            return False
        core = {
            "id": core.get("id"),
            "labels": list(folded.labels),
            "meta": dict(folded.meta),
            "links": dict(folded.links),
        }
        labels = set(core["labels"])
        parent = _binding(core, "link_source_card") or ""
        head = _binding(core, "link_head_revision") or ""
        evidence = _binding(core, "candidate_evidence_sha256") or ""
        repository = _binding(core, "repository") or ""
        if (
            not {"review", "review-distinct-agent"} <= labels
            or "source-only" in labels
            or {"hold", "do-not-claim"} & labels
            or not re.fullmatch(r"[0-9a-f]{8}", parent)
            or parent == core.get("id")
            or not re.fullmatch(r"[0-9a-f]{40}", head)
            or not re.fullmatch(r"[0-9a-f]{64}", evidence)
            or not producer
            or _binding(core, "producer_identity") != producer
        ):
            return False
        root = home / "evidence/work" / parent
        # Never downgrade a modern source, including a malformed manifest.
        if os.path.lexists(root / "source-bundles"):
            return False
        source = store.fold(parent)
        if (
            source is None
            or source.archived
            or source.owner
            or source.status.value != "review"
            or source.meta.get("claim_conflicts")
            or {"hold", "do-not-claim"} & set(source.labels)
        ):
            return False
        source_core = {"meta": source.meta, "links": source.links}
        for key in ("repository", "base_ref", "base_revision"):
            if not _binding(core, key) or _binding(core, key) != _binding(source_core, key):
                return False
        pr = _binding(core, "pr")
        if (
            not repository.startswith("https://")
            or not pr
            or pr != _binding(source_core, "pr")
            or not re.fullmatch(
                re.escape(repository.removesuffix(".git")) + r"/pulls/[1-9][0-9]*", pr
            )
        ):
            return False
        outcome = _latest_outcome(store, parent)
        events = store._read_events(parent) + store._legacy_events(parent)
        if any(
            e.get("action") == "claim" and e.get("ts", "") > outcome.get("ts", "") for e in events
        ):
            return False
        tree = outcome.get("candidate_tree", "")
        if (
            outcome.get("action") != "verdict"
            or outcome.get("expected_claim_revision") is not None
            or outcome.get("verdict") != "PASS_FOR_REVIEW"
            or outcome.get("writer") != producer
            or outcome.get("candidate_commit") != head
            or not re.fullmatch(r"[0-9a-f]{40}", tree)
            or _binding(core, "candidate_tree") not in (None, tree)
        ):
            return False
        candidate = Path(outcome.get("candidate_path", ""))
        if not candidate.is_relative_to(root) or _digest(candidate, 64 * 1024**2) != outcome.get(
            "candidate_sha256"
        ):
            return False
        for key in ("evidence", "repair_evidence"):
            value = source.links.get(key, "")
            path, _, pinned = value.partition("#sha256=")
            pinned = pinned or source.links.get(key + "_sha256")
            if pinned != evidence:
                continue
            receipt = Path(path)
            if receipt.is_relative_to(root) and _digest(receipt, MAX_EVIDENCE) == evidence:
                return True
        return False
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False
