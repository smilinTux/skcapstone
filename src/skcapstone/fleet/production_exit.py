"""Keep native producer custody until independent review and completion."""

import os
from pathlib import Path

from skcoord.card_store import CardStore

from .source_bundle import publish_source
from .source_transport import _authority


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
            artifact = publish_source(
                home,
                {"card_id": args.card, "repository": repository, "base_revision": base},
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
