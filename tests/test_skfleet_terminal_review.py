"""Terminal independent-review verdicts must not be dispatched again."""

import ast
import re
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"


def _load(labels, outcomes, title=""):
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"outcome_workflow_class", "terminal_review_verdict"}
    ]
    namespace = {
        "re": re,
        "_PROVISIONAL_PASS_RE": re.compile(r"^\s*(PASS_FOR_[A-Z_]+|PASS_READY_[A-Z_]+)\b", re.I),
        "_REVIEW_TITLE_RE": re.compile(r"\[(?:REVIEW|REREVIEW)\]", re.I),
        "folded_labels": lambda cid, core: labels,
        "_load_outcomes": lambda: {"card": outcomes},
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(SCRIPT), "exec"), namespace)
    return namespace["terminal_review_verdict"]("card", {"title": title})


def _classify(card_id, title, outcome):
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "outcome_workflow_class"
    )
    namespace = {
        "re": re,
        "_PROVISIONAL_PASS_RE": re.compile(r"^\s*(PASS_FOR_[A-Z_]+|PASS_READY_[A-Z_]+)\b", re.I),
        "_REVIEW_TITLE_RE": re.compile(r"\[(?:REVIEW|REREVIEW)\]", re.I),
        "folded_labels": lambda cid, core: [],
        "_load_outcomes": lambda: {card_id: ("2026-09-03T18:00:00Z", outcome)},
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SCRIPT), "exec"), namespace)
    return namespace["outcome_workflow_class"](card_id, {"title": title})


def test_review_fail_is_terminal():
    assert _load(["review"], ("2026-09-01T14:28:05Z", "FAIL: evidence mismatch"))


def test_review_pass_is_terminal():
    assert _load(["review"], ("2026-09-01T14:28:05Z", "PASS"))


def test_legacy_review_title_is_terminal_without_review_label():
    assert _load([], ("2026-09-01T14:28:05Z", "PASS"), "[X][REVIEW] Verify")


def test_source_pass_for_review_is_not_terminal():
    assert not _load(["repair"], ("2026-09-01T14:28:05Z", "PASS_FOR_REVIEW"))


def test_blocked_review_can_be_retried_after_change():
    assert not _load(["review"], ("2026-09-01T14:28:05Z", "BLOCKED: dependency"))


def test_seven_row_plain_pass_census_has_exact_workflow_classes():
    rows = {
        "16e8ba34": ("[INTEGRATION-PREP] Refresh reviewed quorum repair", "PASS"),
        "3e3c32d6": ("[SKL-S5-04B] Load and saturation qualification", "PASS"),
        "567e6b09": ("[FLEET][REVIEW] Independently review partition 11", "PASS"),
        "881c0a32": ("[SKL-S1-03A-R2A] Implement replay reservation", "PASS"),
        "8a3f57c4": ("[SKDASH-ECONOMY-01] Compose governed provider", "PASS"),
        "ac4e04fd": ("[FLEET-SELECTOR] Enforce Qwen-first", "PASS"),
        "b70f118c": ("[SKL-S0-04] Scaffold monorepo", "PASS: SBOM complete"),
    }

    assert {
        card_id: _classify(card_id, title, outcome) for card_id, (title, outcome) in rows.items()
    } == {
        "16e8ba34": "workflow_closure_required",
        "3e3c32d6": "workflow_closure_required",
        "567e6b09": "terminal_review",
        "881c0a32": "workflow_closure_required",
        "8a3f57c4": "workflow_closure_required",
        "ac4e04fd": "workflow_closure_required",
        "b70f118c": "workflow_closure_required",
    }
