"""Regression tests for bounded POOL_V2 dispatch authority."""

from __future__ import annotations

import ast
import collections
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

from skcapstone.review_admission import governed_review_seat, qualified_reviewer_seats

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _load_helpers(*names: str) -> dict[str, object]:
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    }
    assert set(functions) == set(names)
    module = ast.Module(body=[functions[name] for name in names], type_ignores=[])
    namespace: dict[str, object] = {
        "collections": collections,
        "hashlib": hashlib,
        "json": json,
        "re": re,
        "governed_review_seat": governed_review_seat,
        "qualified_reviewer_seats": qualified_reviewer_seats,
        "blocked_backoff": lambda _cid: False,
    }
    exec(compile(module, str(ROTATE), "exec"), namespace)
    return namespace


def _admission(card_id: str, *, claimable: object = True) -> dict[str, object]:
    return {
        "card_id": card_id,
        "claimable": claimable,
        "reason": "claimable" if claimable is True else "unknown",
        "host_pin": None,
        "title": f"Synthetic {card_id}",
        "labels": [],
        "core": {"id": card_id, "initial_priority": "high"},
        "overlay": {},
        "source_revision": "a" * 64,
    }


def test_pool_v2_dispatchable_refuses_backoff_overlay() -> None:
    """Seraph review admission cannot bypass an unresolved dependency backoff."""
    dispatchable = _load_helpers("_pool_v2_dispatchable")["_pool_v2_dispatchable"]
    card_id = "4cd4dd62"
    admission = {
        "card_id": card_id,
        "claimable": False,
        "reason": "review",
        "title": "[W72][M][REVIEW] blocked dependency",
        "labels": ["review", "seat-seraph"],
        "core": {"id": card_id},
        "seraph_review_admitted": True,
        "elastic_review_admitted": False,
        "overlay": {"backoff": True, "reason": "review"},
        "source_revision": "c" * 64,
    }
    assert dispatchable(admission) is False
    admission["overlay"] = {"backoff": False, "reason": "review"}
    assert dispatchable(admission) is True


def test_seraph_admission_clears_when_blocked_backoff_holds() -> None:
    """Unresolved blockers clear Seraph/elastic bits so do-not-claim is unnecessary."""
    helpers = _load_helpers(
        "_governed_review_metadata",
        "_pool_v2_admission",
        "_pool_v2_dispatchable",
        "_pool_v2_ready_ids",
    )
    helpers.update(
        {
            "_ONLY_SEAT": "seraph",
            "_pool_v2_overlay": lambda _cid, _core, reason: {
                "reason": reason,
                "backoff": True,
            },
            "blocked_backoff": lambda _cid: True,
            "seat_for": lambda _cid, _core: "seraph",
        }
    )
    card_id = "4cd4dd62"
    core = {
        "id": card_id,
        "kind": "task",
        "title": "[W72-COMM01-R][M][REVIEW] Independently verify",
        "initial_priority": "high",
        "links": {
            "producer_identity": "codex-resume-383a7834",
            "candidate_evidence_sha256": "a" * 64,
            "blocked_on": "dependency card:383a7834",
            "evidence_sha256": "5658c6eed5fae206138d6adfc7daf45602899ee78f78e8e34ac1716059f3c2f1",
        },
        "meta": {
            "link_source_card": "383a7834",
            "link_head_revision": "b" * 40,
        },
    }
    claimability = {
        "claimable": False,
        "reason": "review",
        "host_pin": None,
        "title": core["title"],
        "labels": ["review", "seat-seraph", "parent-383a7834"],
        "core": core,
        "source_revision": "b" * 64,
    }
    admission = helpers["_pool_v2_admission"](card_id, core, claimability)
    decisions = [SimpleNamespace(card_id=card_id, eligible=True)]
    assert admission["seraph_review_admitted"] is False
    assert admission["elastic_review_admitted"] is False
    assert helpers["_pool_v2_dispatchable"](admission) is False
    assert helpers["_pool_v2_ready_ids"](decisions, {card_id: admission}) == set()


def test_pool_v2_is_authoritative_for_large_only_v2_population() -> None:
    """Forty-five V2-only cards enter even when the legacy pool has two rows."""
    ready_ids = _load_helpers("_pool_v2_dispatchable", "_pool_v2_ready_ids")["_pool_v2_ready_ids"]
    ids = [f"{index:08x}" for index in range(45)]
    decisions = [SimpleNamespace(card_id=card_id, eligible=True) for card_id in ids]
    admissions = {card_id: _admission(card_id) for card_id in ids}
    legacy_ids = set(ids[:2])

    authoritative = ready_ids(decisions, admissions)

    assert authoritative == set(ids)
    assert authoritative - legacy_ids == set(ids[2:])
    assert len(authoritative) == 45


def test_malformed_review_stale_drift_and_unknown_fail_closed() -> None:
    """Every uncertain class stays out of the authoritative candidate set."""
    ready_ids = _load_helpers("_pool_v2_dispatchable", "_pool_v2_ready_ids")["_pool_v2_ready_ids"]
    cases = {
        "malformed": False,
        "review": False,
        "stale": False,
        "drift": False,
        "unknown": True,
    }
    decisions = [
        SimpleNamespace(card_id=card_id, eligible=eligible) for card_id, eligible in cases.items()
    ]
    admissions = {
        "malformed": _admission("malformed", claimable=False),
        "review": _admission("review", claimable=False),
        "stale": _admission("stale", claimable=False),
        "drift": _admission("drift", claimable=False),
        "unknown": _admission("unknown", claimable=None),
    }

    assert ready_ids(decisions, admissions) == set()
    assert ready_ids(decisions, admissions, failed=True) == set()


def test_canonical_review_card_enters_seraph_or_elastic_codex_selector() -> None:
    """A complete canonical review reaches Seraph without becoming generic work."""
    helpers = _load_helpers(
        "_governed_review_metadata",
        "_pool_v2_admission",
        "_pool_v2_dispatchable",
        "_pool_v2_ready_ids",
    )
    helpers.update(
        {
            "_ONLY_SEAT": "seraph",
            "_pool_v2_overlay": lambda _cid, _core, reason: {"reason": reason},
            "seat_for": lambda _cid, _core: "seraph",
        }
    )
    card_id = "3ca49674"
    core = {
        "id": card_id,
        "kind": "task",
        "title": "[LINK-source-head][S][REVIEW] Review exact source head",
        "initial_priority": "medium",
        "links": {
            "producer_identity": "mero",
            "candidate_evidence_sha256": "a" * 64,
        },
        "meta": {
            "link_source_card": "source",
            "link_head_revision": "b" * 40,
        },
    }
    claimability = {
        "claimable": False,
        "reason": "review",
        "host_pin": None,
        "title": core["title"],
        "labels": ["review", "seat-seraph", "parent-source"],
        "core": core,
        "source_revision": "b" * 64,
    }

    admission = helpers["_pool_v2_admission"](card_id, core, claimability)
    decisions = [SimpleNamespace(card_id=card_id, eligible=True)]

    assert admission["governed_review"] is True
    assert admission["seraph_review_admitted"] is True
    assert helpers["_pool_v2_ready_ids"](decisions, {card_id: admission}) == {card_id}

    helpers["_ONLY_SEAT"] = ""
    generic = helpers["_pool_v2_admission"](card_id, core, claimability)
    assert generic["seraph_review_admitted"] is False
    assert generic["elastic_review_admitted"] is True
    assert helpers["_pool_v2_ready_ids"](decisions, {card_id: generic}) == {card_id}

    link_claimability = dict(claimability, labels=["review", "seat-link"])
    link_generic = helpers["_pool_v2_admission"](card_id, core, link_claimability)
    assert link_generic["elastic_review_admitted"] is True
    assert helpers["_pool_v2_ready_ids"](decisions, {card_id: link_generic}) == {card_id}

    helpers["_pool_v2_admission"].__globals__["_ONLY_SEAT"] = "link"
    link_direct = helpers["_pool_v2_admission"](card_id, core, link_claimability)
    assert link_direct["seraph_review_admitted"] is True
    assert helpers["_pool_v2_ready_ids"](decisions, {card_id: link_direct}) == {card_id}

    source = ROTATE.read_text(encoding="utf-8")
    assert "not (seraph_review_admitted or elastic_review_admitted)" in source

    incomplete = dict(core)
    incomplete["links"] = {"producer_identity": "mero"}
    incomplete_claimability = dict(claimability, core=incomplete)
    helpers["_ONLY_SEAT"] = "seraph"
    rejected = helpers["_pool_v2_admission"](card_id, incomplete, incomplete_claimability)
    assert rejected["seraph_review_admitted"] is False
    assert helpers["_pool_v2_ready_ids"](decisions, {card_id: rejected}) == set()


def test_preclaim_requires_identical_snapshot_fingerprint() -> None:
    """Any source, overlay, or claimability drift produces zero launch authority."""
    matches = _load_helpers(
        "_pool_v2_dispatchable",
        "_pool_v2_fingerprint",
        "_pool_v2_preclaim_matches",
    )["_pool_v2_preclaim_matches"]
    selected = _admission("cafefeed")
    assert matches(selected, dict(selected)) is True

    for field, value in (
        ("source_revision", "b" * 64),
        ("overlay", {"awaiting_review": True}),
        ("reason", "owned-review"),
        ("claimable", False),
    ):
        changed = dict(selected)
        changed[field] = value
        assert matches(selected, changed) is False
    assert matches(selected, None) is False


def test_seraph_preclaim_batch_rejects_duplicate_source_heads() -> None:
    """An ambiguous source/head pair cannot reach claim or launch selection."""
    unique = _load_helpers("_seraph_unique_source_heads")["_seraph_unique_source_heads"]
    unique.__globals__["_ONLY_SEAT"] = "seraph"

    def row(card_id: str, source: str, head: str) -> tuple[object, ...]:
        return (
            0,
            1,
            card_id,
            {
                "meta": {
                    "link_source_card": source,
                    "link_head_revision": head,
                }
            },
            [],
            0,
        )

    first = row("review01", "source", "a" * 40)
    duplicate = row("review02", "source", "a" * 40)
    other_head = row("review03", "source", "b" * 40)

    admitted, withheld = unique([first, duplicate, other_head])

    assert admitted == [other_head]
    assert withheld == {("source", "a" * 40)}


def test_seraph_preclaim_batch_allows_distinct_heads_concurrently() -> None:
    """Distinct source heads remain in the same bounded dispatch batch."""
    unique = _load_helpers("_seraph_unique_source_heads")["_seraph_unique_source_heads"]
    unique.__globals__["_ONLY_SEAT"] = "seraph"
    rows = [
        (
            0,
            1,
            "review01",
            {
                "meta": {
                    "link_source_card": "source",
                    "link_head_revision": head,
                }
            },
            [],
            0,
        )
        for head in ("a" * 40, "b" * 40)
    ]

    admitted, withheld = unique(rows)

    assert admitted == rows
    assert withheld == set()


def test_worker_runtime_contract_is_unchanged() -> None:
    """The authority repair does not alter Kimi, wrapper, heartbeat, or attribution."""
    source = ROTATE.read_text(encoding="utf-8")
    assert "model=_logical_route_for(core)" in source
    assert '"provider":"skgateway"' in source
    assert 'model=str(_selected_route["model_or_bucket"])' not in source
    assert 'qwen_suitable(fresh_claimability["core"]),' in source
    assert "SKFLEET_CARD_ID=%s SKFLEET_CLAIM_REVISION=%s SKFLEET_SESSION_ID=%s" in source
    assert '"--session",sess,"--worker-executable",PI,' in source
    assert "actor=name," in source
    assert "trap 'trap - HUP INT TERM; " in source


def test_role_seats_require_exact_label_and_dispatch_opt_in() -> None:
    """Tank and ATLAS cannot leak into generic or mismatched dispatch."""
    helpers = _load_helpers(
        "_role_seat_metadata",
        "_pool_v2_dispatchable",
        "_pool_v2_ready_ids",
        "_pool_v2_authority_rows",
    )
    helpers.update({"_SEAT_LABEL_PREFIX": "seat-", "_CATEGORY_OPT_IN": "dispatch-approved"})
    card_id = "a8100007"
    decision = [SimpleNamespace(card_id=card_id, eligible=True)]
    admission = _admission(card_id)
    admission["labels"] = ["seat-tank", "dispatch-approved"]
    admission["core"]["links"] = {"approved_artifact_sha256": "a" * 64}
    args = (decision, {card_id: admission}, False, {}, {"high": 1}, (), "chiap08")

    helpers["_ONLY_SEAT"] = "tank"
    assert [row[2] for row in helpers["_pool_v2_authority_rows"](*args)[0]] == [card_id]
    helpers["_ONLY_SEAT"] = "atlas"
    assert helpers["_pool_v2_authority_rows"](*args)[0] == []
    helpers["_ONLY_SEAT"] = ""
    assert helpers["_pool_v2_authority_rows"](*args)[0] == []

    admission["labels"] = ["seat-tank"]
    helpers["_ONLY_SEAT"] = "tank"
    assert helpers["_pool_v2_authority_rows"](*args)[0] == []
    admission["labels"] = ["seat-tank", "seat-atlas", "dispatch-approved"]
    assert helpers["_pool_v2_authority_rows"](*args)[0] == []


def test_role_seats_require_exact_authority_metadata() -> None:
    helpers = _load_helpers(
        "_role_seat_metadata",
        "_pool_v2_dispatchable",
        "_pool_v2_ready_ids",
        "_pool_v2_authority_rows",
    )
    helpers.update({"_SEAT_LABEL_PREFIX": "seat-", "_CATEGORY_OPT_IN": "dispatch-approved"})

    def selected(seat: str, links: dict[str, str]) -> list[object]:
        card_id = "a8100007"
        admission = _admission(card_id)
        admission["labels"] = [f"seat-{seat}", "dispatch-approved"]
        admission["core"]["links"] = links
        helpers["_ONLY_SEAT"] = seat
        return helpers["_pool_v2_authority_rows"](
            [SimpleNamespace(card_id=card_id, eligible=True)],
            {card_id: admission},
            False,
            {},
            {"high": 1},
            (),
            "chiap08",
        )[0]

    assert selected("tank", {}) == []
    assert selected("tank", {"approved_artifact_sha256": "bad"}) == []
    assert len(selected("tank", {"approved_artifact_sha256": "a" * 64})) == 1
    assert selected("atlas", {"verification_target": "prod"}) == []
    assert selected("atlas", {"verification_evidence_sha256": "b" * 64}) == []
    assert (
        len(
            selected(
                "atlas",
                {
                    "verification_target": "release-42/postconditions",
                    "verification_evidence_sha256": "b" * 64,
                },
            )
        )
        == 1
    )


def test_unsupported_worker_card_id_is_filtered_before_selection() -> None:
    """A legacy ID cannot consume a bounded launch opportunity."""
    helpers = _load_helpers(
        "_pool_v2_dispatchable", "_pool_v2_ready_ids", "_pool_v2_authority_rows"
    )
    helpers.update(
        {
            "_SEAT_LABEL_PREFIX": "seat-",
            "_CATEGORY_OPT_IN": "dispatch-approved",
            "_ONLY_SEAT": "",
        }
    )
    unsupported = "a8100007-01"
    supported = "a8100008"
    decisions = [
        SimpleNamespace(card_id=card_id, eligible=True) for card_id in (unsupported, supported)
    ]
    admissions = {card_id: _admission(card_id) for card_id in (unsupported, supported)}

    selected, _ = helpers["_pool_v2_authority_rows"](
        decisions, admissions, False, {}, {"high": 1}, (), "chiap08"
    )

    assert [row[2] for row in selected] == [supported]


def test_authority_and_preclaim_are_wired_into_launcher() -> None:
    source = ROTATE.read_text(encoding="utf-8")
    assert "pool, _PINNED_IDS = _pool_v2_authority_rows(" in source
    assert "_OWNER_BY_ID, _SEAT_BLOCKED = _pool_v2_owner_map(" in source
    assert "POOL_AUTHORITY|%s|source=POOL_V2" in source
    assert "_pool_v2_preclaim_handoff(" in source
    assert "SKIPPED_ADMISSION_DRIFT|" in source
