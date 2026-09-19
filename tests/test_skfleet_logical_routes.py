"""Fleet workers preserve job size as provider-neutral SKGateway routes."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _helpers() -> dict[str, object]:
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    wanted = {"_GLM_SIZE_RE", "_LOGICAL_ROUTES"}
    body = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id in wanted for target in node.targets
            )
        )
        or (
            isinstance(node, ast.FunctionDef)
            and node.name in {"_size_class_for", "_logical_route_for"}
        )
    ]
    namespace: dict[str, object] = {"re": re}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace


@pytest.mark.parametrize(
    ("size", "route"),
    [("S", "sk-s"), ("M", "sk-m"), ("L", "sk-l"), ("XL", "sk-xl")],
)
def test_tshirt_size_maps_only_to_logical_gateway_route(size: str, route: str) -> None:
    helper = _helpers()["_logical_route_for"]
    assert helper({"title": f"[CARD][{size}] Work"}) == route


@pytest.mark.parametrize(
    "title",
    ["[CARD] Missing size", "[CARD][S][M] Ambiguous size", ""],
)
def test_missing_or_ambiguous_size_fails_closed(title: str) -> None:
    helper = _helpers()["_logical_route_for"]
    assert helper({"title": title}) is None


def test_empty_title_uses_one_canonical_folded_size_label() -> None:
    helper = _helpers()["_logical_route_for"]
    assert helper({"title": ""}, ["review", "sk-l"]) == "sk-l"
    assert helper({"title": ""}, ["sk-s", "sk-m"]) is None
    assert helper({"title": ""}, ["review"]) is None


def test_launch_never_replaces_logical_route_with_selected_member() -> None:
    """The route identity names the SIZE, never a concrete pool member.

    The identity is what preflight, health and evidence are keyed on, so if it
    ever carries a member model instead of the size, those three start naming a
    backend and the card's size is lost from the record.
    """
    source = ROTATE.read_text(encoding="utf-8")
    assert 'model=str(_selected_route["model_or_bucket"])' not in source
    assert '"provider":"skgateway"' in source
    assert '"logical_route":_bucket' in source
    # `model` is now the LANE's resolution of the bucket (sk-codex-mid, sk-glm-m,
    # kimi-for-coding). It is the right thing to SEND and the wrong thing to
    # record as the identity, so it must never be assigned back to it.
    assert '"logical_route":model' not in source


def test_corrupted_title_still_routes_from_the_canonical_size_label() -> None:
    """A describe that ate the title must not un-route an already-sized card.

    Measured on chi 2026-09-19: an ``mcp`` writer appended ``describe`` events
    carrying literal argv fragments as the title (``x``, ``--description``) to
    live cards, ~80 times since 2026-09-08. CardStore folds the latest describe,
    so ``[SKLEGAL-R33-ACTIVITY][S] ...`` folded to ``x`` while the card still
    carried its canonical ``sk-s`` label. The title lost the size marker, this
    helper returned None, and the candidate scan silently dropped the card. Every
    chi host went to ``owned_ready=0`` with free seats and a non-empty pool.

    The size LABEL is the same canonical route id the title marker resolves to,
    so a non-empty-but-unmarked title must fall through to it exactly as an
    empty one already did. A title the card no longer has is not evidence that
    the card has no size.
    """
    helper = _helpers()["_logical_route_for"]
    assert helper({"title": "x"}, ["sklegal", "source-only", "sk-s"]) == "sk-s"
    assert helper({"title": "--description"}, ["sk-s", "size-s"]) == "sk-s"
    assert (
        helper(
            {"title": "Authority contradiction retention audit"},
            ["sklegal", "sk-s", "kimi-cohort"],
        )
        == "sk-s"
    )
    # A title marker still wins outright, and still fails closed with no label.
    assert helper({"title": "[C][M] Work"}, ["sk-s"]) == "sk-m"
    assert helper({"title": "x"}, ["sklegal"]) is None
    assert helper({"title": "x"}, ["sk-s", "sk-m"]) is None
