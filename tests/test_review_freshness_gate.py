from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "fleet" / "review-freshness-gate.py"


def load_module():
    spec = importlib.util.spec_from_file_location("review_freshness_gate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pr_fixture(state="OPEN", head="abc123", rollup=None):
    return {
        "state": state,
        "headRefOid": head,
        "baseRefName": "main",
        "statusCheckRollup": (
            rollup
            if rollup is not None
            else [
                {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"name": "unit tests (py3.11)", "status": "COMPLETED", "conclusion": "SUCCESS"},
            ]
        ),
    }


def test_fresh_and_green_passes():
    m = load_module()
    v = m.evaluate(pr_fixture(), {"status": "ahead", "behind_by": 0}, "r", 1)
    assert v["verdict"] == "FRESH_AND_GREEN" and v["exit_code"] == 0
    assert v["contains_current_main"] and v["all_checks_green_on_head"]


def test_behind_head_blocks_even_with_green_checks():
    m = load_module()
    v = m.evaluate(pr_fixture(), {"status": "diverged", "behind_by": 8}, "r", 1)
    assert v["verdict"] == "BEHIND_CURRENT_MAIN" and v["exit_code"] == 1
    assert not v["contains_current_main"]
    assert v["behind_by"] == 8


def test_failing_check_blocks():
    m = load_module()
    rollup = [
        {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"name": "unit tests (py3.12)", "status": "COMPLETED", "conclusion": "FAILURE"},
    ]
    v = m.evaluate(pr_fixture(rollup=rollup), {"status": "ahead", "behind_by": 0}, "r", 1)
    assert v["verdict"] == "CHECKS_NOT_GREEN" and v["exit_code"] == 2
    assert v["failed"] == ["unit tests (py3.12)"]


def test_pending_check_blocks():
    m = load_module()
    rollup = [{"name": "build", "status": "IN_PROGRESS", "conclusion": ""}]
    v = m.evaluate(pr_fixture(rollup=rollup), {"status": "identical", "behind_by": 0}, "r", 1)
    assert v["verdict"] == "CHECKS_NOT_GREEN" and v["exit_code"] == 2
    assert v["pending"] == ["build"]


def test_no_checks_is_not_green():
    m = load_module()
    v = m.evaluate(pr_fixture(rollup=[]), {"status": "ahead", "behind_by": 0}, "r", 1)
    assert v["verdict"] == "CHECKS_NOT_GREEN" and v["exit_code"] == 2
    assert v["check_count"] == 0


def test_skipped_and_neutral_count_as_green():
    m = load_module()
    rollup = [
        {"name": "docs", "status": "COMPLETED", "conclusion": "SKIPPED"},
        {"name": "guard", "status": "COMPLETED", "conclusion": "NEUTRAL"},
    ]
    v = m.evaluate(pr_fixture(rollup=rollup), {"status": "ahead", "behind_by": 0}, "r", 1)
    assert v["exit_code"] == 0


def test_main_routes_exit_codes(monkeypatch, capsys):
    m = load_module()
    calls = {}

    def fake_gh_json(*args):
        if args[0] == "pr":
            return calls["pr"]
        return calls["compare"]

    monkeypatch.setattr(m, "gh_json", fake_gh_json)
    calls["pr"] = pr_fixture()
    calls["compare"] = {"status": "behind", "behind_by": 3}
    assert m.main(["--repo", "r/x", "--pr", "5"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "BEHIND_CURRENT_MAIN"

    calls["pr"] = pr_fixture(state="MERGED")
    assert m.main(["--repo", "r/x", "--pr", "5"]) == 3
    assert json.loads(capsys.readouterr().out)["verdict"] == "NOT_OPEN"


def test_main_error_returns_three(monkeypatch, capsys):
    m = load_module()

    def boom(*args):
        raise RuntimeError("gh pr view failed: not found")

    monkeypatch.setattr(m, "gh_json", boom)
    assert m.main(["--repo", "r/x", "--pr", "99"]) == 3
    assert "ERROR" in capsys.readouterr().out
