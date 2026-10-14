"""Read-only recovery proofs for released, already published modern producers."""

import json
import re
from datetime import datetime
from pathlib import Path

from skcoord.card_store import CardStore

from . import source_bundle as bundle


def prove(home, source, manifest):
    """Require intact bytes, the latest exact PASS/claim, and its native release."""
    from .production_acceptance import _sealed_source_outcome

    owner, claim = manifest.get("owner"), manifest.get("claim_revision")
    if (
        source is None
        or source.archived
        or source.owner is not None
        or source.status.value not in {"backlog", "ready", "review"}
        or source.meta.get("_claim_revision")
        or source.meta.get("claim_conflicts")
        or "source-only" not in source.labels
        or {"hold", "do-not-claim", "human-gate"}.intersection(source.labels)
        or not re.fullmatch(r"[0-9a-f]{32}", str(claim))
        or not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("head")))
    ):
        raise ValueError("released producer custody changed")
    store = CardStore(home)
    outcome = _sealed_source_outcome(
        store, source.id, owner, claim, manifest["head"], manifest["tree"]
    )
    events = store._read_events(source.id) + store._legacy_events(source.id)
    claims = [e for e in events if e.get("action") == "claim"]
    latest = max(claims, key=lambda e: (e.get("ts", ""), e.get("writer", ""), e.get("seq", 0)))
    releases = [
        e
        for e in events
        if e.get("action") == "release_claim"
        and e.get("released_owner") == owner
        and e.get("expected_claim_revision") == claim
    ]
    if (
        (latest.get("claim_revision") or latest.get("event_id")) != claim
        or latest.get("owner") != owner
        or len(releases) != 1
        or not re.fullmatch(r"[0-9a-f]{32}", str(releases[0].get("event_id")))
        or datetime.fromisoformat(releases[0]["ts"]) <= datetime.fromisoformat(outcome["ts"])
        or outcome.get("candidate_sha256") != manifest.get("evidence_sha256")
        or outcome.get("candidate_ref") != manifest.get("ref")
    ):
        raise ValueError("released producer provenance differs")
    root = bundle._root(Path(home), source.id)
    saved = json.loads(bundle._read(root / (manifest["head"] + ".json"), bundle.MAX_EVIDENCE))
    raw = bundle._read(root / (manifest["bundle_sha256"] + ".bundle"), bundle.MAX_BUNDLE)
    evidence = bundle._read(root / (manifest["evidence_sha256"] + ".md"), bundle.MAX_EVIDENCE)
    if (
        saved != manifest
        or saved.get("schema") != "skfleet.source-bundle/v1"
        or saved.get("card") != source.id
        or len(raw) != manifest["bundle_bytes"]
        or bundle._sha(raw) != manifest["bundle_sha256"]
        or bundle._sha(evidence) != manifest["evidence_sha256"]
    ):
        raise ValueError("released producer artifacts differ")
    return releases[0]


def _before_release(home, source, release):
    """Build a read-only event view with one exact release excluded."""

    class BeforeRelease(CardStore):
        def _read_events(self, card):
            return [
                e
                for e in super()._read_events(card)
                if card != source.id or e.get("event_id") != release["event_id"]
            ]

        def _legacy_events(self, card):
            return [
                e
                for e in super()._legacy_events(card)
                if card != source.id or e.get("event_id") != release["event_id"]
            ]

    return BeforeRelease(home)


def revision_matches(home, source, release, pinned):
    """Replay without one exact release, never mutate or recreate a live claim."""
    from ..seraph_review_cardstore import LiveCardStoreGateway

    return (
        LiveCardStoreGateway(home)
        .read_card(source.id, _store=_before_release(home, source, release))
        .revision
        == pinned
    )


def current_attempt(home, source, release, manifest):
    """Keep native replacement authority while resolving the released candidate."""
    from ..review_replacement import _record_binding
    from ..review_work_identity import card_generation, review_card_id

    before = _before_release(home, source, release).fold(source.id)
    predecessor = review_card_id(
        source.id, manifest["head"], card_generation(before), manifest["evidence_sha256"]
    )
    return _record_binding(Path(home), predecessor, live_source=False)["review_card_id"]


def terminal(home, source, manifest, status, path):
    """Recognize a retained successful producer observation after exact release."""
    from .builder_dispatch import AUTHORITY_RELEASED
    from .production_builder import unit_name
    from .production_review_custody import unit_terminal
    from .production_review_finish import read_json

    prove(home, source, manifest)
    request = read_json(Path(home) / "fleet/dispatch" / path.parent.parent.name / path.name)
    bound = request.get("production") or {}
    artifact = status.get("source_artifact")
    if (
        status.get("schema") != "skfleet.builder-dispatch-status/v1"
        or status.get("state") != "blocked"
        or status.get("error") != AUTHORITY_RELEASED
        or status.get("claim_released") is not True
        or status.get("card_id") != source.id
        or status.get("node") != path.parent.parent.name
        or status.get("owner") != manifest["owner"]
        or status.get("claim_revision") != manifest["claim_revision"]
        or (status.get("writer") or {}).get("role") != "sknoded"
        or (status.get("writer") or {}).get("node") != status["node"]
        or not (
            type(status.get("exit_code")) is int
            and status["exit_code"] == 0
            or status.get("exit_code") is None
            and status.get("terminal_proof") == "qualified-terminal"
        )
        or request.get("request_id") != status.get("request_id")
        or request.get("card_id") != source.id
        or request.get("repository") != manifest["repository"]
        or request.get("base_revision") != manifest["base_revision"]
        or bound != status.get("production")
        or status.get("unit") != unit_name(request, status.get("attempt"))
        or artifact is not None
        and any(artifact.get(k) != v for k, v in manifest.items())
        or not bound.get("host")
        or not bound.get("family")
    ):
        raise ValueError("released producer terminal differs")
    unit_terminal(status["unit"], status.get("invocation"), host=bound["host"])
    return dict(
        unit=status["unit"],
        invocation=status["invocation"],
        host=bound["host"],
        request_id=status["request_id"],
        family=bound["family"],
        node=status["node"],
    )


def direct_review_view(home, source, manifest=None):
    """Read one proven released direct generation without restoring its claim."""
    from ..seraph_review_cardstore import _latest_outcome
    from .paths import FleetPaths
    from .production_acceptance import _direct_seat_terminal
    from .production_custody import retains_source_custody, source_binding
    from .production_review_custody import unit_terminal
    from .source_transport import _packet

    home = Path(home)
    if source is None or source.owner is not None:
        raise ValueError("released direct source must be unowned")
    if manifest is None:
        outcome = _latest_outcome(CardStore(home), source.id)
        manifest = _packet(home, source.id, outcome.get("candidate_commit", ""))["manifest"]
    release = prove(home, source, manifest)
    outcome = _latest_outcome(CardStore(home), source.id)
    view = _before_release(home, source, release)
    original = view.fold(source.id)
    binding = source_binding(original)
    if any(
        manifest.get(k) != binding[k] for k in ("repository", "base_revision")
    ) or not retains_source_custody(
        home,
        source.id,
        manifest["owner"],
        manifest["claim_revision"],
        fleet_paths=FleetPaths(home / "fleet"),
        store=view,
    ):
        raise ValueError("released direct launch custody differs")
    terminal = _direct_seat_terminal(
        home, original, manifest, manifest["owner"], manifest["claim_revision"]
    )
    receipt = json.loads(bundle._read(Path(terminal["receipt_path"]), bundle.MAX_EVIDENCE))
    launches = [
        e
        for e in view._read_events(source.id)
        if e.get("action") == "production_assignment_launch"
        and e.get("schema") == "skfleet.production-assignment-launch/v1"
        and e.get("writer") == manifest["owner"]
        and e.get("worker") == manifest["owner"]
        and e.get("claim_revision") == manifest["claim_revision"]
        and e.get("launched") is True
        and e.get("source_binding") == binding
    ]
    if (
        len(launches) != 1
        or receipt.get("schema") != "skfleet.direct-seat/v1"
        or type(receipt.get("exit_code")) is not int
        or datetime.fromisoformat(launches[0]["ts"]) > datetime.fromisoformat(outcome["ts"])
        or any(
            receipt.get(k) != launches[0].get("route_identity", {}).get(k)
            for k in ("provider", "logical_route", "capacity_domains", "model_or_bucket")
        )
    ):
        raise ValueError("released direct terminal route differs")
    unit_terminal(terminal["unit"], terminal["invocation"], host=terminal["host"])
    return view, manifest, release


def review_lineage(home, source, manifest):
    """Resolve the same authorized attempt from the proven original generation."""
    from ..review_replacement import _record_binding
    from ..review_work_identity import card_generation, review_card_id

    predecessor = review_card_id(
        source.id, manifest["head"], card_generation(source), manifest["evidence_sha256"]
    )
    return _record_binding(Path(home), predecessor, live_source=False)
