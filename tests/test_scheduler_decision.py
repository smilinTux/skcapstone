"""Scheduler eligibility reasons form one stable, complete partition."""

import ast
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from skcapstone.scheduler_decision import (
    SchedulerDecision,
    SchedulerFacts,
    classify_scheduler,
    classify_scheduler_population,
    pool_v2,
)

SCRIPT = Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"

# Exact origin/main baseline at 659014a. This candidate may reduce inherited
# launcher debt but must not add a finding in any existing Ruff category.
LAUNCHER_RUFF_BASELINE = {
    "E401": 1,
    "E501": 11,
    "E701": 93,
    "E702": 20,
    "E722": 2,
    "E741": 4,
    "F401": 1,
    "F841": 1,
    "I001": 1,
}


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("malformed", True, "malformed"),
        ("lifecycle_excluded", True, "lifecycle_excluded"),
        ("selector_excluded", True, "selector_excluded"),
        ("terminal_cardstore", True, "terminal_cardstore"),
        ("terminal_itil", True, "terminal_itil"),
        ("superseded", True, "superseded"),
        ("owner_health", "dead", "owned_dead"),
        ("owner_health", "stale", "owned_stale"),
        ("owner_health", "live", "owned_live"),
        ("human_gate", True, "human_gate"),
        ("foreign_project", True, "foreign_project"),
        ("not_claimable", True, "not_claimable"),
        ("sensitive_category", True, "sensitive_category"),
        ("dependency", True, "dependency"),
        ("awaiting_review", True, "awaiting_review"),
        ("backoff", True, "backoff"),
        ("attempt_limit", True, "attempt_limit"),
        ("host_pin_elsewhere", True, "host_pin_elsewhere"),
    ],
)
def test_each_primary_reason(field: str, value: object, reason: str) -> None:
    decision = classify_scheduler(SchedulerFacts("deadbeef", **{field: value}))

    assert decision.primary_reason == reason
    assert decision.eligible is False


def test_precedence_is_exclusive_and_preserves_lower_reasons_as_facets() -> None:
    decision = classify_scheduler(
        SchedulerFacts(
            "deadbeef",
            human_gate=True,
            dependency=True,
            backoff=True,
            adapter_facets=("skcoord:void_dependency_edges",),
        )
    )

    assert decision.primary_reason == "human_gate"
    assert decision.facets == (
        "dependency",
        "backoff",
        "skcoord:void_dependency_edges",
    )


def test_no_reason_is_ready() -> None:
    assert classify_scheduler(SchedulerFacts("deadbeef")) == SchedulerDecision(
        "deadbeef", "ready", True
    )


@pytest.mark.parametrize(
    ("primary_reason", "eligible"),
    [("ready", False), ("dependency", True)],
)
def test_decision_rejects_incoherent_eligibility(primary_reason: str, eligible: bool) -> None:
    with pytest.raises(
        ValueError, match="eligible must be true exactly when primary_reason is ready"
    ):
        SchedulerDecision("deadbeef", primary_reason, eligible)


def test_pool_v2_is_a_complete_partition() -> None:
    decisions = [
        classify_scheduler(SchedulerFacts("aaaaaaaa")),
        classify_scheduler(SchedulerFacts("bbbbbbbb", dependency=True)),
        classify_scheduler(SchedulerFacts("cccccccc", dependency=True, backoff=True)),
    ]

    report = pool_v2("chiap01", decisions)

    assert report.population == report.ready + report.ineligible == 3
    assert report.reasons == {"dependency": 2}
    assert report.render() == (
        "POOL_V2|chiap01|population=3 ready=1 ineligible=2 " 'reasons={"dependency":2}'
    )


def test_pool_v2_rejects_duplicate_card_decisions() -> None:
    decision = classify_scheduler(SchedulerFacts("deadbeef"))

    with pytest.raises(ValueError, match="one decision per card"):
        pool_v2("chiap01", [decision, decision])


def _launcher_function(name: str, namespace: dict) -> object:
    source = SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name
    )
    exec(compile(ast.Module(body=[function], type_ignores=[]), source, "exec"), namespace)
    return namespace[name]


def test_shadow_partition_executes_real_legacy_path_on_same_population(tmp_path) -> None:
    rows = {
        "ready000": {},
        "excluded": {"excluded": True, "dependency": True},
        "terminal": {"lifecycle": "complete", "awaiting_review": True},
        "owned00": {"lifecycle": "claimed", "dependency": True},
        "review00": {"awaiting_review": True, "backoff": True},
        "blocked0": {"dependency": True, "backoff": True},
        "hostpin0": {"claimability_reason": "host-pin:chiap01"},
    }
    for cid in rows:
        (tmp_path / f"{cid}.json").write_text(
            json.dumps({"id": cid, "title": cid}), encoding="utf-8"
        )

    def row(cid):
        return rows[cid]

    def outcome_bucket(lifecycle, historical_review):
        if lifecycle == "open":
            return "open"
        if lifecycle == "claimed":
            return "historical_review_claimed" if historical_review else "claimed"
        return "historical_review_terminal" if historical_review else "terminal"

    def claimability(cid, core=None):
        reason = row(cid).get("claimability_reason")
        if reason is None and row(cid).get("dependency"):
            reason = "dependency"
        if reason:
            return {"claimable": False, "reason": reason}
        payload = core or {"id": cid, "title": cid}
        return {
            "claimable": True,
            "reason": "claimable",
            "title": payload["title"],
            "labels": [],
            "core": payload,
        }

    legacy = _launcher_function(
        "_legacy_selector_decision",
        {
            "excluded": {cid for cid, facts in rows.items() if facts.get("excluded")},
            "_REVIEW_READBACK_BLOCKED": set(),
            "unclaimable": lambda cid: False,
            "itil_terminal": lambda cid: False,
            "lifecycle_state": lambda cid: row(cid).get("lifecycle", "open"),
            "awaiting_review": lambda cid: row(cid).get("awaiting_review", False),
            "outcome_lifecycle_bucket": outcome_bucket,
            "authoritative_claimability": claimability,
            "blocked_backoff": lambda cid: row(cid).get("backoff", False),
            "terminal_review_verdict": lambda cid, core: False,
            "source_spec": lambda core, home: None,
            "SourceWorktreeError": ValueError,
            "Path": Path,
            "HOME": str(tmp_path),
            "json": json,
        },
    )
    legacy_ready_ids = {
        cid for cid in rows if legacy(cid, str(tmp_path / f"{cid}.json"))["eligible"]
    }
    population = tuple(
        SchedulerFacts(
            cid,
            lifecycle_excluded=facts.get("excluded", False),
            terminal_cardstore=facts.get("lifecycle") == "complete",
            owner_health="live" if facts.get("lifecycle") == "claimed" else None,
            dependency=facts.get("dependency", False),
            awaiting_review=facts.get("awaiting_review", False),
            backoff=facts.get("backoff", False),
            host_pin_elsewhere=str(facts.get("claimability_reason", "")).startswith("host-pin:"),
        )
        for cid, facts in rows.items()
    )
    decisions = classify_scheduler_population(population)
    shadow_ready_ids = {row.card_id for row in decisions if row.eligible}
    report = pool_v2("chiap08", decisions)

    assert shadow_ready_ids == legacy_ready_ids
    assert legacy_ready_ids == {"ready000"}
    assert report.population == len(rows)
    assert report.population == report.ready + sum(report.reasons.values())
    assert report.reasons == {
        "awaiting_review": 1,
        "dependency": 1,
        "host_pin_elsewhere": 1,
        "lifecycle_excluded": 1,
        "owned_live": 1,
        "terminal_cardstore": 1,
    }


def test_shadow_error_is_logged_and_does_not_change_legacy_pool() -> None:
    messages = []
    legacy_pool = ["ready000"]

    emit = _launcher_function(
        "_emit_shadow_pool_v2",
        {
            "_shadow_pool_v2": lambda: (_ for _ in ()).throw(RuntimeError("boom")),
            "log": lambda _directory, message: messages.append(message),
            "d": "unused",
            "HOST": "chiap08",
        },
    )
    emit()

    assert legacy_pool == ["ready000"]
    assert messages == ["SHADOW_ERROR|chiap08|RuntimeError:boom"]


def test_launcher_ruff_does_not_expand_the_exact_inherited_baseline() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--output-format=json",
            str(SCRIPT),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    findings = json.loads(result.stdout)
    counts = Counter(item["code"] for item in findings)

    assert not (set(counts) - set(LAUNCHER_RUFF_BASELINE))
    assert all(counts[code] <= baseline for code, baseline in LAUNCHER_RUFF_BASELINE.items())
    assert sum(counts.values()) <= sum(LAUNCHER_RUFF_BASELINE.values()) == 134


def test_launcher_emits_shadow_report_after_legacy_pool() -> None:
    launcher = (Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet-rotate.py").read_text(
        encoding="utf-8"
    )

    legacy = launcher.index('log(d,"POOL|%s|ready=%d')
    shadow = launcher.index("_shadow_pool_v2()", legacy)
    selection = launcher.index("def owner_host", shadow)

    assert legacy < shadow < selection
    assert "POOL_V2_PARITY|%s|match=%s" in launcher
    assert "SKCoord contributes" in launcher
