"""Governed CardStore labels must reach the live fleet selector."""

from __future__ import annotations

import ast
from pathlib import Path

from skcapstone.card_store import CardCore, CardStore
from skcapstone.fleet import builder_dispatch

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _load_folded_labels(home: Path, evidence_labels: dict[str, list[dict]]):
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "folded_labels"
    )
    namespace = {
        "CardStore": CardStore,
        "Path": Path,
        "HOME": str(home),
        "_load_label_events": lambda: evidence_labels,
        "_label_value": lambda event: event.get("label"),
        "_cardstore_label_cache": {},
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace["folded_labels"]


def test_folded_labels_uses_governed_cardstore_and_keeps_legacy_evidence_labels(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    (home / ".skcapstone").mkdir(mode=0o700, parents=True)
    store = CardStore(home / ".skcapstone")
    store.create(
        CardCore(
            id="a1b2c3d4",
            title="synthetic builder card",
            initial_labels=["source-only", "sk-m", "codex-only"],
        )
    )
    store.append_event("a1b2c3d4", "add_label", "jarvis", label="glm-only")
    store.append_event("a1b2c3d4", "add_label", "jarvis", label="host-pin:chiap01")
    store.append_event("a1b2c3d4", "remove_label", "jarvis", label="codex-only")

    evidence_labels = {
        "a1b2c3d4": [
            {"action": "add_label", "label": "needs-stronger-model"},
        ]
    }
    fold = _load_folded_labels(home, evidence_labels)
    labels = fold(
        "a1b2c3d4",
        {"initial_labels": ["source-only", "sk-m", "codex-only"]},
    )

    assert labels == [
        "source-only",
        "sk-m",
        "glm-only",
        "host-pin:chiap01",
        "needs-stronger-model",
    ]
    monkeypatch.setenv("SKFLEET_PRODUCTION_POLICY", "/policy")
    assert builder_dispatch.logical_route(labels) == "sk-m"
    assert builder_dispatch.eligible({"id": "a1b2c3d4"}, labels)
