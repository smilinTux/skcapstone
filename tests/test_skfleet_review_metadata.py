"""Review assignment consumes folded typed metadata with a legacy fallback."""

from __future__ import annotations

import ast
import hashlib
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"
COORD = ROOT / "src" / "skcapstone" / "cli" / "coord.py"


class BoundaryError(Exception):
    """Test boundary failure."""


def _load_assignment(
    events: list[dict[str, object]] | None = None,
    claim_revision: str | None = None,
) -> tuple[dict[str, object], list[dict[str, object]], list[dict[str, object]]]:
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "_review_assignment"
    )
    seen = []

    def recommend(_home, **kwargs):
        seen.append(kwargs)
        return SimpleNamespace(recommendation_id=kwargs["recommendation_id"])

    handoffs = []

    def authorize(*_args, **kwargs):
        handoffs.append(kwargs)
        return SimpleNamespace(reviewer="link")

    namespace = {
        "BoundaryError": BoundaryError,
        "HOME": "/tmp",
        "Path": Path,
        "authorize_review_launch": authorize,
        "event_rows": lambda _cid: events or [],
        "hashlib": hashlib,
        "re": re,
        "recommend_reviewer": recommend,
        "_card_process_snapshot": lambda _cid: {"sessions": []},
        "_current_claim_identity_fresh": lambda _cid: ("owner", 1.0, claim_revision),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace, seen, handoffs


def test_typed_metadata_wins_over_stale_description() -> None:
    namespace, seen, _handoffs = _load_assignment()
    digest = "b" * 64
    core = {
        "description": "Producer identity: stale. Candidate evidence sha256=" + "a" * 64 + ".",
        "links": {
            "producer_identity": "current-producer",
            "candidate_evidence_sha256": digest,
        },
    }

    reviewer, _recommendation, _handoff = namespace["_review_assignment"](
        "deadbeef", core, ["review"], "link"
    )

    assert reviewer == "link"
    assert seen[0]["author"] == "current-producer"
    assert seen[0]["evidence_sha256"] == digest


def test_coord_create_help_example_supplies_claimable_review_metadata() -> None:
    source = COORD.read_text(encoding="utf-8")
    assert "coord link e5f6a7b8 producer_identity pi-codex-source" in source
    assert "coord link e5f6a7b8 candidate_evidence_sha256 " in source
    namespace, seen, _handoffs = _load_assignment()
    digest = "a" * 64

    namespace["_review_assignment"](
        "e5f6a7b8",
        {
            "description": "Producer identity: pi-codex-source. Candidate evidence "
            f"sha256={digest}.",
            "links": {
                "producer_identity": "pi-codex-source",
                "candidate_evidence_sha256": digest,
            },
        },
        ["review"],
        "link",
    )

    assert seen[0]["author"] == "pi-codex-source"
    assert seen[0]["evidence_sha256"] == digest


def test_legacy_description_remains_supported() -> None:
    namespace, seen, _handoffs = _load_assignment()
    digest = "c" * 64
    core = {
        "description": "Producer identity: legacy-producer. Candidate evidence sha256="
        + digest
        + "."
    }

    namespace["_review_assignment"]("deadbeef", core, ["review"], "link")

    assert seen[0]["author"] == "legacy-producer"
    assert seen[0]["evidence_sha256"] == digest


@pytest.mark.parametrize(
    "links",
    [
        {"producer_identity": "producer-only"},
        {"candidate_evidence_sha256": "d" * 64},
        {
            "producer_identity": "producer",
            "candidate_evidence_sha256": "not-a-digest",
        },
    ],
)
def test_incomplete_or_malformed_typed_metadata_fails_closed(links) -> None:
    namespace, seen, _handoffs = _load_assignment()
    core = {
        "description": "Producer identity: valid-fallback. Candidate evidence sha256="
        + "e" * 64
        + ".",
        "links": links,
    }

    with pytest.raises(BoundaryError):
        namespace["_review_assignment"]("deadbeef", core, ["review"], "link")
    assert seen == []


def test_only_live_successful_launch_consumes_review_recommendation() -> None:
    digest = "f" * 64
    core = {
        "links": {
            "producer_identity": "producer",
            "candidate_evidence_sha256": digest,
        }
    }
    events = [
        {
            "action": "review_assignment_launch",
            "recommendation_id": "failed-current",
            "claim_revision": "current",
            "launched": False,
        },
        {
            "action": "review_assignment_launch",
            "recommendation_id": "successful-old",
            "claim_revision": "old",
            "launched": True,
        },
        {
            "action": "review_assignment_launch",
            "recommendation_id": "successful-current",
            "claim_revision": "current",
            "launched": True,
        },
    ]
    namespace, _seen, handoffs = _load_assignment(events, "current")

    namespace["_review_assignment"]("deadbeef", core, ["review"], "link")

    assert handoffs[0]["used_recommendation_ids"] == {"successful-current"}


def test_released_review_claim_does_not_consume_recommendation() -> None:
    events = [
        {
            "action": "review_assignment_launch",
            "recommendation_id": "burned",
            "claim_revision": "released",
            "launched": True,
        }
    ]
    namespace, _seen, handoffs = _load_assignment(events, None)

    namespace["_review_assignment"](
        "deadbeef",
        {
            "links": {
                "producer_identity": "producer",
                "candidate_evidence_sha256": "f" * 64,
            }
        },
        ["review"],
        "link",
    )

    assert handoffs[0]["used_recommendation_ids"] == set()


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("[CARD][S] Small", "glm-4.6"),
        ("[CARD][M] Medium", "glm-4.6"),
        ("[CARD][L] Large", "glm-4.7"),
        ("[CARD][XL] Extra large", "glm-5.3"),
        ("[CARD] Unspecified", None),
    ],
)
def test_glm_model_follows_card_size(title: str, expected: str | None) -> None:
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    names = {"_GLM_LEVEL_DEFAULTS", "_GLM_LEVELS", "_GLM_SIZE_RE"}
    nodes = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in names
        )
        or (isinstance(node, ast.FunctionDef) and node.name == "_glm_model_for")
    ]
    namespace = {"os": os, "re": re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROTATE), "exec"), namespace)
    assert namespace["_glm_model_for"]({"title": title}) == expected
