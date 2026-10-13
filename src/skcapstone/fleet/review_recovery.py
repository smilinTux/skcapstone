"""Narrow proof helpers for superseding obsolete review generations."""

import json
import os
import subprocess
from pathlib import Path


def failed_review_exit(value, request, status):
    """Bind a failed managed review exit to the acknowledged native generation."""
    execution = status.get("execution") or {}
    return bool(
        value.get("schema") == "skfleet.production-review-exit/v1"
        and type(value.get("exit_code")) is int
        and value["exit_code"] != 0
        and value.get("card") == request.get("card_id") == status.get("card_id")
        and value.get("owner") == request.get("reviewer") == status.get("owner")
        and value.get("claim_revision") == status.get("claim_revision")
        and value.get("execution") == execution
        and all(value.get(k) == execution.get(k) for k in ("host", "unit", "invocation"))
        and value.get("source_head") == request.get("source", {}).get("head")
        and value.get("lane") == request.get("production", {}).get("family")
    )


def acceptance_absent(home, card, claim):
    """Do not discard an acceptance transaction or its successful review custody."""
    from .production_acceptance import review_directory

    directory = review_directory(home, card, claim)
    if os.path.lexists(directory):
        raise ValueError("review acceptance custody exists")


def supersession_binding(current, request):
    """Record old criteria and the new snapshot without asserting a verdict."""
    from .production_builder import digest

    core = current.model_dump(mode="json")
    return {
        "disposition": "superseded",
        "superseded_card_sha256": digest(core),
        "criteria_offered": request.get("criteria_sha256"),
        "criteria_current": digest(
            {
                k: core.get(k)
                for k in (
                    "title",
                    "description",
                    "acceptance_criteria",
                    "labels",
                    "dependencies",
                    "links",
                )
            }
        ),
    }


def recover_unused_offers(paths, home, actor):
    """Sweep expired offers without statuses using the full prestart verifier."""
    from . import review_retire as retire

    results = []
    for path in sorted((paths.root / "dispatch").glob("node-*/*.json")):
        card, node = path.stem, path.parent.name
        if not retire.CARD.fullmatch(card):
            continue
        if os.path.lexists(retire.dispatch.status_path(paths, node, card)):
            continue
        try:
            raw = retire._raw(path)
            request = json.loads(raw)
            if request.get("work_kind") != "review":
                continue
            current = retire.CardStore(home).fold(card)
            outcome = retire.retire_prestart(
                paths,
                Path(home),
                node,
                card,
                request_sha256=retire.source_bundle._sha(raw),
                card_sha256=retire.review_state_revision(current),
                previous_owner=None,
                previous_claim_revision=None,
                actor=actor,
                reason="automatic: expired unused review offer superseded",
                superseded=True,
                apply=True,
            )
            results.append(dict(card=card, node=node, state=outcome["state"]))
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            results.append(dict(card=card, node=node, state="refused", reason=str(exc)[:160]))
    return results


def retire_policy_drifted_offers(paths, home, actor):
    """Retire unconsumed review offers whose captured policy is no longer current.

    The builder node holds such offers untouched (POLICY_DRIFT_HELD) and leaves
    retirement to the authority. The full prestart verifier decides: any claim,
    launch, status, live unit or session leaves the offer held (refused).
    Retired offers no longer exist as request files, so cycles are idempotent.
    The normal offer path then re-offers under the current policy.
    """
    from . import review_dispatch
    from . import review_retire as retire

    try:
        current_policy = retire.production_builder.policy()
    except (OSError, ValueError, KeyError, TypeError):
        return []
    if current_policy is None:
        return []
    results = []
    for path in sorted((paths.root / "dispatch").glob("node-*/*.json")):
        card, node = path.stem, path.parent.name
        if not retire.CARD.fullmatch(card):
            continue
        try:
            raw = retire._raw(path)
            request = json.loads(raw)
            if (
                request.get("work_kind") != "review"
                or not isinstance(request.get("policy"), dict)
                or review_dispatch._offer_policy_current(request, current_policy)
                or os.path.lexists(retire.dispatch.status_path(paths, node, card))
            ):
                continue
            current = retire.CardStore(home).fold(card)
            outcome = retire.retire_prestart(
                paths,
                Path(home),
                node,
                card,
                request_sha256=retire.source_bundle._sha(raw),
                card_sha256=retire.review_state_revision(current),
                previous_owner=None,
                previous_claim_revision=None,
                actor=actor,
                reason="automatic: unconsumed review offer retired, production policy changed",
                superseded=True,
                policy_drifted=True,
                apply=True,
            )
            results.append(
                dict(
                    card=card,
                    node=node,
                    state=outcome["state"],
                    request_id=request.get("request_id"),
                    old_digest=retire.production_builder.digest(request["policy"]),
                    new_digest=retire.production_builder.digest(current_policy),
                )
            )
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
            results.append(dict(card=card, node=node, state="refused", reason=str(exc)[:160]))
    return results


def snapshot_proven(receipt, value, request):
    """Verify supersession snapshots without invalidating older receipt formats."""
    from . import review_retire as retire

    if "disposition" not in value:
        return True
    try:
        core = json.loads(retire._raw(receipt.parent / "superseded-card.json"))
        from types import SimpleNamespace

        current = SimpleNamespace(model_dump=lambda **kw: core)
        return all(value.get(k) == v for k, v in supersession_binding(current, request).items())
    except (OSError, ValueError, TypeError):
        return False
