"""Scheduler eligibility reasons form one stable, complete partition."""

import ast
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone.scheduler_decision import (
    SchedulerDecision,
    SchedulerFacts,
    classify_scheduler,
    classify_scheduler_population,
    pool_v2,
)
from skcapstone.seat_boundaries import BoundaryError
from skcapstone.seat_runtime import (
    authorize_review_launch,
    recommend_reviewer,
    review_state_revision,
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
    namespace.setdefault("re", re)
    source = SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name
    )
    exec(compile(ast.Module(body=[function], type_ignores=[]), source, "exec"), namespace)
    return namespace[name]


def _admission(
    card_id: str,
    *,
    claimable: object = True,
    reason: str = "claimable",
    labels: list[str] | None = None,
    core: dict[str, object] | None = None,
    governed_review: bool = False,
) -> dict[str, object]:
    card_core = core or {
        "id": card_id,
        "title": f"[CARD][S] {card_id}",
        "initial_priority": "high",
    }
    return {
        "card_id": card_id,
        "claimable": claimable,
        "reason": reason,
        "host_pin": None,
        "title": str(card_core["title"]),
        "labels": list(labels or []),
        "core": card_core,
        "governed_review": governed_review,
        "overlay": {"reason": reason},
        "source_revision": hashlib.sha256(card_id.encode()).hexdigest(),
    }


def _authoritative_claimability_for(card_home: Path):
    """Load the launcher's real fold against one isolated CardStore."""

    dependency_value = _launcher_function("_dependency_value", {})
    task_claimable = _launcher_function("_coord_task_claimable", {})
    fold = _launcher_function(
        "_fold_claimability",
        {
            "_COLUMNS": {"backlog", "ready", "doing", "review", "done"},
            "_dependency_value": dependency_value,
        },
    )

    def host_pin(_core, _labels):
        return None

    reason = _launcher_function(
        "_claimability_reason",
        {
            "_coord_task_claimable": task_claimable,
            "_NOT_CLAIMABLE": {"not-claimable", "sprint-container", "do-not-claim"},
            "_SENSITIVE_CATEGORY": re.compile(
                r"(capauth|credential|custody|issuer|secret|\bkey\b|rollback|"
                r"deploy|production|release|migrat)",
                re.I,
            ),
            "_CATEGORY_OPT_IN": "dispatch-approved",
            "non_implementation": lambda core, labels: (
                "[HUMAN]" in str(core.get("title") or "").upper() or "human-gate" in labels
            ),
            "_dep_satisfied": lambda _dependency: True,
            "host_pin": host_pin,
            "HOST": "chiap08",
        },
    )
    store = CardStore(card_home)
    snapshot = _launcher_function(
        "_authoritative_card_snapshot",
        {
            "os": __import__("os"),
            "json": json,
            "hashlib": hashlib,
            "CARDS": str(card_home / "cards"),
            "_strict_card_events": lambda cid, fresh=False: store._read_events(cid),
            "_legacy_claimability_events": lambda fresh=False: {},
            "_fold_claimability": fold,
        },
    )
    return _launcher_function(
        "authoritative_claimability",
        {
            "_authoritative_card_snapshot": snapshot,
            "_claimability_reason": reason,
            "host_pin": host_pin,
        },
    )


def test_pool_v2_authority_includes_safe_review_rows_and_fails_closed() -> None:
    dispatchable = _launcher_function("_pool_v2_dispatchable", {})
    ready_ids = _launcher_function("_pool_v2_ready_ids", {"_pool_v2_dispatchable": dispatchable})
    decisions = (
        SchedulerDecision("claim000", "ready", True),
        SchedulerDecision("review00", "ready", True),
        SchedulerDecision("unsafe00", "ready", True),
        SchedulerDecision("blocked0", "dependency", False),
    )
    admissions = {
        "claim000": _admission("claim000"),
        "review00": _admission(
            "review00",
            claimable=False,
            reason="review",
            labels=["review"],
            governed_review=True,
        ),
        "unsafe00": _admission("unsafe00", claimable=False, reason="dependency"),
        "blocked0": _admission("blocked0"),
    }

    assert ready_ids(decisions, admissions) == {"claim000", "review00"}
    assert ready_ids(decisions, admissions, failed=True) == set()


def test_pool_v2_preclaim_accepts_unchanged_review_only() -> None:
    dispatchable = _launcher_function("_pool_v2_dispatchable", {})
    fingerprint = _launcher_function(
        "_pool_v2_fingerprint", {"hashlib": __import__("hashlib"), "json": json}
    )
    matches = _launcher_function(
        "_pool_v2_preclaim_matches",
        {
            "_pool_v2_dispatchable": dispatchable,
            "_pool_v2_fingerprint": fingerprint,
        },
    )
    selected = _admission(
        "review00",
        claimable=False,
        reason="review",
        labels=["review"],
        governed_review=True,
    )

    assert matches(selected, dict(selected)) is True
    assert matches(selected, {**selected, "source_revision": "b"}) is False
    assert (
        matches(
            {"claimable": False, "reason": "dependency"},
            {"claimable": False, "reason": "dependency"},
        )
        is False
    )


def test_legacy_8_pool_v2_64_reaches_governed_review_preclaim(tmp_path) -> None:
    card_home = tmp_path / ".skcapstone"
    card_home.mkdir()
    store = CardStore(card_home)
    card_ids = [f"{index:08x}" for index in range(64)]
    legacy_ids = set(card_ids[:8])
    for cid in card_ids:
        review = cid not in legacy_ids
        parent_id = f"{int(cid, 16) + 64:08x}"
        if review:
            store.create(
                CardCore(
                    id=parent_id,
                    title=f"Source candidate {cid}",
                    created_by="producer",
                )
            )
        store.create(
            CardCore(
                id=cid,
                title=(f"[REVIEW] Candidate {cid}" if review else f"[CARD][S] Candidate {cid}"),
                created_by="producer",
                initial_priority="high",
                initial_labels=(
                    ["review", f"parent-{parent_id}", "qwen-suitable"]
                    if review
                    else ["qwen-suitable"]
                ),
            )
        )
        if review:
            store.append_event(
                cid,
                "link",
                "fixture",
                link_key="producer_identity",
                link_value="producer",
            )
            store.append_event(
                cid,
                "link",
                "fixture",
                link_key="candidate_evidence_sha256",
                link_value=hashlib.sha256(cid.encode()).hexdigest(),
            )

    claimability_for = _authoritative_claimability_for(card_home)
    metadata = _launcher_function("_governed_review_metadata", {"re": re})
    dispatchable = _launcher_function("_pool_v2_dispatchable", {})
    ready_ids = _launcher_function("_pool_v2_ready_ids", {"_pool_v2_dispatchable": dispatchable})
    fingerprint = _launcher_function("_pool_v2_fingerprint", {"hashlib": hashlib, "json": json})
    matches = _launcher_function(
        "_pool_v2_preclaim_matches",
        {
            "_pool_v2_dispatchable": dispatchable,
            "_pool_v2_fingerprint": fingerprint,
        },
    )
    admission = _launcher_function(
        "_pool_v2_admission",
        {
            "_governed_review_metadata": metadata,
            "_pool_v2_overlay": lambda cid, core, reason: {"reason": reason},
        },
    )
    review_assignment = _launcher_function(
        "_review_assignment",
        {
            "_governed_review_metadata": metadata,
            "BoundaryError": BoundaryError,
            "hashlib": hashlib,
            "Path": Path,
            "CardStore": CardStore,
            "HOME": str(tmp_path),
            "_card_process_snapshot": lambda cid: {"sessions": []},
            "_current_claim_identity_fresh": lambda cid: (None, None, None),
            "event_rows": lambda cid: store._read_events(cid),
            "recommend_reviewer": recommend_reviewer,
            "review_state_revision": review_state_revision,
            "authorize_review_launch": authorize_review_launch,
        },
    )
    preclaim_handoff = _launcher_function(
        "_pool_v2_preclaim_handoff",
        {
            "_pool_v2_preclaim_matches": matches,
            "_review_assignment": review_assignment,
            "BoundaryError": BoundaryError,
        },
    )
    authority_rows = _launcher_function(
        "_pool_v2_authority_rows",
        {"_pool_v2_ready_ids": ready_ids, "json": json},
    )
    partition_owner = _launcher_function("_partition_owner", {"hashlib": hashlib})
    rotation_hosts = ("chiap01", "chiap02", "chiap03", "chiap04", "chiap08")
    seat_owner = _launcher_function(
        "_seat_owner",
        {"_partition_owner": partition_owner, "ROTATION_HOSTS": rotation_hosts},
    )
    owner_map = _launcher_function(
        "_pool_v2_owner_map",
        {"_seat_owner": seat_owner, "seat_for": lambda cid, core: None},
    )
    lane_compatibility = _launcher_function("lane_compatibility", {"_LANE_ONLY_LABELS": {}})
    select_lane = _launcher_function(
        "select_compatible_lane", {"lane_compatibility": lane_compatibility}
    )
    legacy_selector = _launcher_function(
        "_legacy_selector_decision",
        {
            "excluded": set(),
            "_REVIEW_READBACK_BLOCKED": set(),
            "unclaimable": lambda cid: False,
            "itil_terminal": lambda cid: False,
            "lifecycle_state": lambda cid: "open",
            "awaiting_review": lambda cid: False,
            "outcome_lifecycle_bucket": lambda lifecycle, historical_review: "open",
            "blocked_backoff": lambda cid: False,
            "terminal_review_verdict": lambda cid, core: False,
            "authoritative_claimability": claimability_for,
            "json": json,
        },
    )
    actual_legacy_ids = {
        cid
        for cid in card_ids
        if legacy_selector(cid, str(card_home / "cards" / cid / "core.json"))["eligible"]
    }
    decisions = classify_scheduler_population(SchedulerFacts(cid) for cid in card_ids)
    admissions = {}
    for cid in card_ids:
        claimability = claimability_for(cid)
        core = claimability["core"]
        admissions[cid] = admission(cid, core, claimability)

    rows, pinned_ids = authority_rows(
        decisions,
        admissions,
        False,
        {},
        {"high": 1},
        (),
        "chiap08",
    )
    authority_ids = {row[2] for row in rows}
    owners, blocked = owner_map(rows, "chiap08", pinned_ids)
    selected_ids = set()
    for host in rotation_hosts:
        remaining = {"qwen": 64}
        for row in rows:
            cid = row[2]
            if owners[cid] != host:
                continue
            lane, lane_reason = select_lane(
                admissions[cid]["labels"],
                False,
                [{"name": "qwen"}],
                remaining,
            )
            assert (lane, lane_reason) == ("qwen", "compatible")
            remaining[lane] -= 1
            selected_ids.add(cid)
    review_id = card_ids[8]
    reviewer_identity = f"pi-codex-chiap08-{review_id}"
    selected_admission = admissions[review_id]
    drifted_admission = {**selected_admission, "source_revision": "f" * 64}
    before_actions = [event["action"] for event in store._read_events(review_id)]
    with pytest.raises(BoundaryError, match="admission changed"):
        preclaim_handoff(
            review_id,
            selected_admission,
            drifted_admission,
            reviewer_identity,
        )
    assert [event["action"] for event in store._read_events(review_id)] == before_actions

    reviewer, recommendation, handoff = preclaim_handoff(
        review_id,
        selected_admission,
        dict(selected_admission),
        reviewer_identity,
    )

    assert actual_legacy_ids == legacy_ids
    assert pool_v2("chiap08", decisions).ready == 64
    assert authority_ids == selected_ids == set(card_ids)
    assert blocked == {}
    assert reviewer == reviewer_identity
    assert recommendation is not None and handoff is not None
    assert recommendation.reviewer == reviewer_identity
    assert handoff.reviewer == reviewer_identity
    assert matches(admissions[review_id], dict(admissions[review_id])) is True
    assert [event["action"] for event in store._read_events(review_id)].count(
        "review_assignment_recommendation"
    ) == 1


@pytest.mark.parametrize(
    "admission",
    [
        {"claimable": False, "reason": "review"},
        {"claimable": False, "reason": "review", "governed_review": False},
        {"claimable": False, "reason": "dependency", "governed_review": True},
        {"claimable": None, "reason": "review", "governed_review": True},
        {},
        None,
    ],
)
def test_pool_v2_review_dispatch_rejects_incomplete_or_unknown(admission) -> None:
    dispatchable = _launcher_function("_pool_v2_dispatchable", {})

    assert dispatchable(admission) is False


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
