"""Regression tests for bounded POOL_V2 dispatch authority."""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from types import SimpleNamespace

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
    namespace: dict[str, object] = {"hashlib": hashlib, "json": json, "re": re}
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


def test_canonical_review_card_enters_only_seraph_selector() -> None:
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
    assert helpers["_pool_v2_ready_ids"](decisions, {card_id: generic}) == set()

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


def test_worker_runtime_contract_is_unchanged() -> None:
    """The authority repair does not alter Kimi, wrapper, heartbeat, or attribution."""
    source = ROTATE.read_text(encoding="utf-8")
    assert 'if _LANE["name"]=="kimi":' in source
    assert "model=_kimi_model_for(core) or model" in source
    assert 'qwen_suitable(fresh_claimability["core"]),' in source
    assert "SKFLEET_CARD_ID=%s SKFLEET_CLAIM_REVISION=%s SKFLEET_SESSION_ID=%s" in source
    assert '"--session",sess,"--worker-executable",PI,' in source
    assert "actor=name," in source
    assert "trap 'trap - HUP INT TERM; " in source


def test_authority_and_preclaim_are_wired_into_launcher() -> None:
    source = ROTATE.read_text(encoding="utf-8")
    assert "pool, _PINNED_IDS = _pool_v2_authority_rows(" in source
    assert "_OWNER_BY_ID, _SEAT_BLOCKED = _pool_v2_owner_map(" in source
    assert "POOL_AUTHORITY|%s|source=POOL_V2" in source
    assert "_pool_v2_preclaim_handoff(" in source
    assert "SKIPPED_ADMISSION_DRIFT|" in source
