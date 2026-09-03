"""Regression tests for GLM level routing and the review replay fence.

Covers the 2026-09-03 stabilization repairs:

1. GLM level routing selects the z.ai model by the card size marker in the
   title, so one shared connection is spent deliberately across levels.
2. A review launch receipt consumes its deterministic recommendation only
   while its exact claim generation is still live, so a worker that launched,
   died, and released its claim can be redispatched instead of fencing the
   review lane on that card forever.
3. The coordination digest skips bare JSON string rows in card event files
   instead of crashing its sort key every cycle.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"
DIGEST = ROOT / "scripts" / "fleet" / "skworld-digest.py"


def _rotate_namespace(nodes: set[str]) -> dict[str, object]:
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    body = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id in nodes for target in node.targets)
        )
        or (isinstance(node, ast.FunctionDef) and node.name in nodes)
    ]
    namespace: dict[str, object] = {
        "re": __import__("re"),
        "os": __import__("os"),
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(ROTATE), "exec"), namespace)
    assert nodes <= namespace.keys()
    return namespace


def _load_glm_helpers() -> dict[str, object]:
    return _rotate_namespace(
        {"_GLM_LEVEL_DEFAULTS", "_GLM_LEVELS", "_GLM_SIZE_RE", "_glm_model_for"}
    )


class _BoundaryError(Exception):
    """Test double for the skcapstone boundary error."""


def _load_review_assignment(
    events: list[dict],
    live_claim_revision: str | None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Exec _review_assignment with stubbed collaborators and capture the fence."""
    captured: dict[str, object] = {}

    def _event_rows(cid):
        return list(events)

    def _current_claim_identity_fresh(cid):
        return ("owner" if live_claim_revision else None, None, live_claim_revision)

    def _card_process_snapshot(cid):
        return {"sessions": []}

    def recommend_reviewer(home, **kwargs):
        return SimpleNamespace(
            card_id=kwargs["card_id"],
            recommendation_id=kwargs["recommendation_id"],
            reviewer=kwargs["candidates"][0],
        )

    def authorize_review_launch(
        home, recommendation, *, actor, current_process, used_recommendation_ids
    ):
        captured["used"] = set(used_recommendation_ids)
        return SimpleNamespace(
            card_id=recommendation.card_id,
            reviewer=recommendation.reviewer,
            recommendation_id=recommendation.recommendation_id,
            state_revision="rev",
        )

    namespace: dict[str, object] = {
        "re": __import__("re"),
        "os": __import__("os"),
        "Path": Path,
        "HOME": "/home/test",
        "hashlib": __import__("hashlib"),
        "BoundaryError": _BoundaryError,
        "event_rows": _event_rows,
        "_current_claim_identity_fresh": _current_claim_identity_fresh,
        "_card_process_snapshot": _card_process_snapshot,
        "recommend_reviewer": recommend_reviewer,
        "authorize_review_launch": authorize_review_launch,
    }
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    body = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_review_assignment"
    ]
    exec(compile(ast.Module(body=body, type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace, captured


def _review_card(core_title="Fix the widget"):
    return {
        "id": "card1",
        "kind": "task",
        "title": core_title,
        "description": "Producer identity: producer-a. sha256="
        + "a" * 64
        + ". Candidate commit: c.",
        "links": {},
    }


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("[GBH-S4-01][XL] Run ten pilots", "glm-5.3"),
        ("[SKGW-COMPAT-R2-07][L] Qualify clients", "glm-4.7"),
        ("[SKLEGAL][S1-05B][L] Provision audit", "glm-4.7"),
        ("[FLEET-GLM-CAP-09][S][DEPLOY] Enforce ceiling", "glm-4.6"),
        ("[SKCOORD-VOLATILE-CI-R1][S] Stabilize identity", "glm-4.6"),
        ("[MERO-01][M] Pin the census clock", "glm-4.6"),
        ("[SKDASH][LIVE-UI-DATA-R1] Repair icons", None),
        ("[SK CONTROL PLANE][SPRINT 1] Pulse", None),
    ],
)
def test_glm_level_selected_from_title_size_marker(title: str, expected) -> None:
    namespace = _load_glm_helpers()
    assert namespace["_glm_model_for"]({"title": title}) == expected


def test_glm_levels_default_table_is_exact() -> None:
    namespace = _load_glm_helpers()
    assert namespace["_GLM_LEVELS"] == {
        "S": "glm-4.6",
        "M": "glm-4.6",
        "L": "glm-4.7",
        "XL": "glm-5.3",
    }


def test_launch_receipt_fences_only_its_live_claim_generation() -> None:
    receipt = {
        "action": "review_assignment_launch",
        "recommendation_id": "link-review-deadbeef",
        "claim_revision": "rev-old",
        "launched": True,
    }
    # The worker died and released its claim: the receipt must not fence.
    namespace, captured = _load_review_assignment([receipt], None)
    namespace["_review_assignment"]("card1", _review_card(), ["review"], "pi-glm-chiap02-1")
    assert captured["used"] == set()

    # The same claim generation is still live: the receipt must fence.
    namespace, captured = _load_review_assignment([receipt], "rev-old")
    namespace["_review_assignment"]("card1", _review_card(), ["review"], "pi-glm-chiap02-1")
    assert captured["used"] == {"link-review-deadbeef"}

    # A newer claim generation exists: the stale receipt must not fence.
    namespace, captured = _load_review_assignment([receipt], "rev-new")
    namespace["_review_assignment"]("card1", _review_card(), ["review"], "pi-glm-chiap02-1")
    assert captured["used"] == set()


def test_failed_launch_receipt_never_fences() -> None:
    failed = {
        "action": "review_assignment_launch",
        "recommendation_id": "link-review-failed",
        "claim_revision": "rev-live",
        "launched": False,
    }
    namespace, captured = _load_review_assignment([failed], "rev-live")
    namespace["_review_assignment"]("card1", _review_card(), ["review"], "pi-glm-chiap02-1")
    assert captured["used"] == set()


def test_digest_collect_board_skips_string_rows(tmp_path: Path) -> None:
    """A bare JSON string row must not crash the board collection sort."""
    events = tmp_path / "card1" / "events"
    events.mkdir(parents=True)
    rows = [
        json.dumps({"action": "claim", "ts": "2026-09-03T10:00:00+00:00", "writer": "worker-a"}),
        json.dumps("a bare json string row from a legacy writer"),
        json.dumps(
            {"action": "release_claim", "ts": "2026-09-03T10:01:00+00:00", "writer": "worker-a"}
        ),
    ]
    (events / "0001.jsonl").write_text("\n".join(rows) + "\n", encoding="utf-8")

    source = ast.parse(DIGEST.read_text(encoding="utf-8"))
    body = [
        node
        for node in source.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        or (isinstance(node, ast.FunctionDef) and node.name == "collect_board")
        or (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "CARDS" for t in node.targets)
        )
    ]
    namespace: dict[str, object] = {}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(DIGEST), "exec"), namespace)
    namespace["CARDS"] = str(tmp_path)
    result = namespace["collect_board"]()
    assert result.get("available") is True
