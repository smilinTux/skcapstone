"""Exact edge-triggered wake tests for the fleet BLOCKED pool."""

from __future__ import annotations

import ast
import collections
import datetime
import glob
import json
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"

FUNCTIONS = {
    "_fold_key",
    "_load_evidence_events",
    "_native_outcome_value",
    "_load_outcomes",
    "_ts_epoch",
    "_label_value",
    "_blocked_reason",
    "_latest_blocked_reason",
    "_completion_epoch",
    "_material_label",
    "_authored_change_epoch",
    "_human_gate",
    "_human_resolution_epoch",
    "_blocker_change_epoch",
    "_wake_retry_available",
    "blocked_backoff",
    "_material_change_since",
    "outcome_workflow_class",
    "awaiting_review",
    "needs_escalation",
}
CONSTANTS = {
    "_OUTCOME_KEYS",
    "_OUTCOME_VALUE_RE",
    "_PIPE_OUTCOME_RE",
    "_INVALID_NATIVE_OUTCOME",
    "_BLOCKED_CATEGORIES",
    "_BLOCKED_ON_RE",
    "_BLOCKED_CAT_RE",
    "_PIPE_VERDICT_RE",
    "_REFERENT_RE",
    "_CARD_REFERENT_RE",
    "_AC_REFERENT_RE",
    "_AC_MENTION_RE",
    "_WAKE_RETRY_LIMIT",
    "_PASS_RE",
    "_PROVISIONAL_PASS_RE",
    "_REVIEW_TITLE_RE",
    "_ESCALATE_LABEL",
    "_CAPABILITY_VERDICT_RE",
}


def _load_backoff_namespace() -> dict[str, object]:
    """Load the scheduler's pure backoff seam without running the launcher."""
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
            nodes.append(node)
        elif isinstance(node, ast.Assign):
            names = {target.id for target in node.targets if isinstance(target, ast.Name)}
            if names & CONSTANTS:
                nodes.append(node)
    namespace = {
        "collections": collections,
        "datetime": datetime,
        "glob": glob,
        "json": json,
        "os": os,
        "re": re,
    }
    exec(compile(ast.Module(nodes, type_ignores=[]), str(ROTATE), "exec"), namespace)
    assert FUNCTIONS <= namespace.keys()
    return namespace


class BackoffHarness:
    """Small mutable board model used by the source-extracted scheduler."""

    def __init__(self, tmp_path: Path) -> None:
        self.ns = _load_backoff_namespace()
        self.events: dict[str, list[dict]] = {}
        self.evidence: dict[str, list[dict]] = {}
        self.labels: dict[str, list[dict]] = {}
        self.outcomes: dict[str, tuple[str, str]] = {}
        self.dependencies: dict[str, list[str]] = {}
        self.states: dict[str, str] = {}
        self.satisfied: dict[str, bool] = {}
        self.attempts: dict[str, int] = {}
        self.cards = tmp_path / "cards"
        self.cards.mkdir()
        self.ns.update(
            {
                "CARDS": str(self.cards),
                "_wake_launch_times": collections.defaultdict(list),
                "_strong_launched_at": {},
                "_launched_at": {},
                "event_rows": lambda cid: self.events.get(cid, []),
                "_load_evidence_events": lambda: self.evidence,
                "_load_label_events": lambda: self.labels,
                "_load_outcomes": lambda: self.outcomes,
                "folded_dependencies": lambda cid, core=None: self.dependencies.get(cid, []),
                "_dependency_value": lambda event: event.get("dependency"),
                "lifecycle_state": lambda cid: self.states.get(cid, "open"),
                "_dep_satisfied": lambda cid: self.satisfied.get(cid, False),
                "folded_labels": lambda cid, core: core.get("initial_labels", []),
                "launch_attempts": lambda cid: self.attempts.get(cid, 0),
            }
        )

    def core(self, cid: str, labels: tuple[str, ...] = (), title: str = "test") -> None:
        path = self.cards / cid
        path.mkdir(exist_ok=True)
        (path / "core.json").write_text(
            json.dumps({"title": title, "initial_labels": list(labels)}), encoding="utf-8"
        )

    def outcome(self, cid: str, stamp: str, value: str) -> None:
        self.outcomes[cid] = (stamp, value)

    def event(self, cid: str, stamp: str, action: str, **fields: object) -> None:
        self.events.setdefault(cid, []).append({"ts": stamp, "action": action, **fields})

    def evidence_event(self, cid: str, stamp: str, action: str, **fields: object) -> None:
        event = {"ts": stamp, "action": action, "card_id": cid, **fields}
        self.evidence.setdefault(cid, []).append(event)
        if action in ("add_label", "remove_label"):
            self.labels.setdefault(cid, []).append(event)

    def blocked(self, cid: str) -> bool:
        return bool(self.ns["blocked_backoff"](cid))

    def epoch(self, stamp: str) -> float:
        return float(self.ns["_ts_epoch"](stamp))


@pytest.fixture
def board(tmp_path: Path) -> BackoffHarness:
    return BackoffHarness(tmp_path)


def _native_namespace(
    tmp_path: Path,
    native: dict[str, list[dict]],
    legacy: list[dict] | None = None,
) -> dict[str, object]:
    events = tmp_path / "coordination-events"
    cards = tmp_path / "cards"
    events.mkdir()
    cards.mkdir()
    for cid in native:
        (cards / cid).mkdir()
    if legacy:
        (events / "writer.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in legacy), encoding="utf-8"
        )
    namespace = _load_backoff_namespace()
    namespace.update(
        {
            "CARDS": str(cards),
            "_EVID_DIR": str(events),
            "_evidence_events": None,
            "_outcomes": None,
            "event_rows": lambda cid: native.get(cid, []),
            "folded_labels": lambda cid, core: core.get("initial_labels", []),
            "_load_label_events": lambda: {},
            "_strong_launched_at": {},
            "_wake_launch_times": collections.defaultdict(list),
            "_launched_at": {},
            "folded_dependencies": lambda cid, core=None: [],
            "launch_attempts": lambda cid: 0,
            "lifecycle_state": lambda cid: "open",
        }
    )
    return namespace


def _native_file_namespace(
    tmp_path: Path, card: str, files: tuple[tuple[str, list[dict]], ...]
) -> dict[str, object]:
    namespace = _native_namespace(tmp_path, {card: []})
    event_dir = Path(namespace["CARDS"]) / card / "events"
    event_dir.mkdir()
    for name, rows in files:
        (event_dir / name).write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )

    def read_rows(cid: str) -> list[dict]:
        rows = []
        for path in sorted((Path(namespace["CARDS"]) / cid / "events").glob("*.jsonl")):
            rows.extend(json.loads(line) for line in path.read_text().splitlines())
        return rows

    namespace["event_rows"] = read_rows
    return namespace


def test_regression_c4ef2a90_native_blocked_event_is_authoritative(
    tmp_path: Path,
) -> None:
    card = "c4ef2a90"
    event = {
        "event_id": "b9c2e4d1a3f70b2c8e6d4a1f5c3b0d9e",
        "ts": "2026-08-30T17:05:00+00:00",
        "writer": "pi-qwen-chiap03-c4ef2a90",
        "action": "blocked",
        "blocked_on": {
            "verdict": "BLOCKED",
            "blocked_on": "dependency",
            "referent": "card:8137c2f5",
            "evidence": "/evidence/work/c4ef2a90/blocked-verdict.json",
            "evidence_sha256": "1" * 64,
        },
    }
    namespace = _native_namespace(tmp_path, {card: [event]})
    namespace.update(
        {
            "_dep_satisfied": lambda cid: False,
            "folded_dependencies": lambda cid, core=None: ["8137c2f5"],
        }
    )
    assert namespace["_load_outcomes"]()[card][1] == (
        "BLOCKED blocked_on=dependency referent=card:8137c2f5"
    )
    assert namespace["blocked_backoff"](card) is True


def test_regression_cd4f9506_split_blocked_evidence_and_reopen(tmp_path: Path) -> None:
    card = "cd4f9506"
    links = [
        ("blocked_on", "dependency"),
        ("referent", "card:2e9a5909"),
        ("evidence", "evidence/cd4f9506-VERDICT-BLOCKED.md"),
        ("evidence_sha256", "e" * 64),
    ]
    legacy = [
        {
            "card_id": card,
            "action": "link",
            "writer": "pi-qwen-chiap01-cd4f9506",
            "ts": f"2026-08-30T16:39:4{index}+00:00",
            "link_key": key,
            "link_value": value,
        }
        for index, (key, value) in enumerate(links)
    ]
    native = {card: []}
    namespace = _native_namespace(tmp_path, native, legacy)
    namespace.update(
        {
            "_dep_satisfied": lambda cid: False,
            "folded_dependencies": lambda cid, core=None: ["2e9a5909"],
        }
    )
    assert namespace["blocked_backoff"](card) is True
    native[card].append(
        {"action": "reopen", "ts": "2026-08-30T17:00:00+00:00", "writer": "jarvis"}
    )
    assert namespace["blocked_backoff"](card) is False


def test_later_pass_does_not_complete_stale_blocked_fragments(tmp_path: Path) -> None:
    card = "abcd1234"
    for pass_writer in ("blocked-worker", "pass-worker"):
        case = tmp_path / pass_writer
        case.mkdir()
        legacy = [
            {
                "card_id": card,
                "action": "link",
                "writer": "blocked-worker",
                "ts": "2026-08-30T01:00:00+00:00",
                "link_key": "blocked_on",
                "link_value": "dependency",
            },
            {
                "card_id": card,
                "action": "link",
                "writer": "blocked-worker",
                "ts": "2026-08-30T01:00:01+00:00",
                "link_key": "referent",
                "link_value": "card:deadbeef",
            },
            {
                "card_id": card,
                "action": "link",
                "writer": pass_writer,
                "ts": "2026-08-30T02:00:00+00:00",
                "link_key": "verdict",
                "link_value": "PASS_FOR_REVIEW",
            },
            {
                "card_id": card,
                "action": "link",
                "writer": pass_writer,
                "ts": "2026-08-30T02:00:01+00:00",
                "link_key": "evidence",
                "link_value": "evidence/new-pass.json",
            },
            {
                "card_id": card,
                "action": "link",
                "writer": pass_writer,
                "ts": "2026-08-30T02:00:02+00:00",
                "link_key": "evidence_sha256",
                "link_value": "a" * 64,
            },
        ]
        namespace = _native_namespace(case, {card: []}, legacy)
        assert namespace["_load_outcomes"]()[card][1] == "PASS_FOR_REVIEW"
        assert namespace["awaiting_review"](card) is True


def test_actionable_reason_requires_one_category_and_exact_referents(
    board: BackoffHarness,
) -> None:
    parse = board.ns["_blocked_reason"]
    assert parse("BLOCKED|card|card:482cc241") == ("card", ("card:482cc241",))
    assert parse("BLOCKED blocked_on=card referent=card:482cc241 and referent=card:2076c423") == (
        "card",
        ("card:482cc241", "card:2076c423"),
    )
    assert parse("BLOCKED blocked_on=card referent=ac:2") == ("card", ("ac:2",))
    assert parse("BLOCKED blocked_on=card") is None
    assert (
        parse("BLOCKED blocked_on=card referent=ac:1 blocked_on=human referent=approval:x") is None
    )


def test_split_blocked_on_and_referent_links_fold(board: BackoffHarness) -> None:
    board.evidence_event(
        "aaaaaaaa", "2026-08-28T00:00:01Z", "link", link_key="blocked_on", link_value="card"
    )
    board.evidence_event(
        "aaaaaaaa", "2026-08-28T00:00:02Z", "link", link_key="referent", link_value="ac:3"
    )
    reason = board.ns["_latest_blocked_reason"]("aaaaaaaa", "2026-08-28T00:00:03Z", "BLOCKED")
    assert reason == ("card", ("ac:3",))


def test_artifact_link_does_not_shadow_latest_real_outcome(tmp_path: Path) -> None:
    events = tmp_path / "events"
    events.mkdir()
    rows = [
        {
            "card_id": "aaaaaaaa",
            "action": "link",
            "ts": "2026-08-28T01:00:00Z",
            "link_key": "verdict",
            "link_value": "BLOCKED|card|ac:1",
        },
        {
            "card_id": "aaaaaaaa",
            "action": "link",
            "ts": "2026-08-28T02:00:00Z",
            "link_key": "verdict_artifact",
            "link_value": "/tmp/BLOCKED.json",
        },
        {
            "card_id": "bbbbbbbb",
            "action": "link",
            "ts": "2026-08-28T03:00:00Z",
            "link_key": "verdict",
            "link_value": "/tmp/verdict.json|sha256=abc|PASS_FOR_REVIEW",
        },
    ]
    (events / "writer.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    namespace = _load_backoff_namespace()
    cards = tmp_path / "cards"
    cards.mkdir()
    namespace.update(
        {
            "CARDS": str(cards),
            "_EVID_DIR": str(events),
            "_evidence_events": None,
            "_outcomes": None,
            "event_rows": lambda cid: [],
        }
    )
    assert namespace["_load_outcomes"]() == {
        "aaaaaaaa": ("2026-08-28T01:00:00Z", "BLOCKED|card|ac:1"),
        "bbbbbbbb": ("2026-08-28T03:00:00Z", "PASS_FOR_REVIEW"),
    }


def test_native_82dec6a7_capability_verdict_routes_to_escalation(tmp_path: Path) -> None:
    card = "82dec6a7"
    stamp = "2026-08-29T01:21:53.640715+00:00"
    native = {
        card: [
            {
                "event_id": "e70e5dfcc09c487c8a97f458d20caea0",
                "ts": stamp,
                "writer": "pi-glm-chiap01-82dec6a7",
                "action": "verdict",
                "verdict": "BLOCKED",
                "blocked_on": "capability",
                "referent": "ac:1",
            }
        ]
    }
    namespace = _native_namespace(tmp_path, native)
    assert namespace["_load_outcomes"]()[card] == (
        stamp,
        "BLOCKED blocked_on=capability referent=ac:1",
    )
    assert namespace["needs_escalation"](card, {}) is True
    assert namespace["blocked_backoff"](card) is False

    namespace["_strong_launched_at"][card] = namespace["_ts_epoch"]("2026-08-29T01:30:00+00:00")
    assert namespace["blocked_backoff"](card) is True


@pytest.mark.parametrize(
    ("card", "blocked_on", "referent"),
    [
        ("aaaaaaaa", "dependency", "card:bbbbbbbb"),
        ("cccccccc", "human", "approval:release"),
    ],
)
def test_native_unresolved_holds_remain_parked(
    tmp_path: Path, card: str, blocked_on: str, referent: str
) -> None:
    native = {
        card: [
            {
                "ts": "2026-08-29T01:00:00+00:00",
                "action": "verdict",
                "verdict": "BLOCKED",
                "blocked_on": blocked_on,
                "referent": referent,
            }
        ]
    }
    namespace = _native_namespace(tmp_path, native)
    assert namespace["blocked_backoff"](card) is True
    assert namespace["needs_escalation"](card, {}) is False


def test_native_provisional_pass_remains_awaiting_review(tmp_path: Path) -> None:
    card = "dddddddd"
    native = {
        card: [
            {
                "ts": "2026-08-29T01:00:00+00:00",
                "action": "verdict",
                "payload": {"verdict": "PASS_FOR_REVIEW"},
            }
        ]
    }
    namespace = _native_namespace(tmp_path, native)
    assert namespace["awaiting_review"](card) is True
    assert namespace["blocked_backoff"](card) is True


def test_native_pass_supersedes_older_legacy_block(tmp_path: Path) -> None:
    """The 567e6b09 shape is reviewable, not a stale BLOCKED retry."""
    card = "567e6b09"
    legacy = [
        {
            "card_id": card,
            "ts": "2026-08-28T11:11:59.211280+00:00",
            "action": "link",
            "link_key": "verdict",
            "link_value": "BLOCKED blocked_on=card referent=card:482cc241",
        }
    ]
    native = {
        card: [
            {
                "event_id": "3b955381055a47fabbd76c0ecf251dd4",
                "ts": "2026-08-29T11:30:02.389597+00:00",
                "writer": "pi-codex-chiap03-567e6b09",
                "action": "verdict",
                "verdict": "PASS",
            }
        ]
    }
    namespace = _native_namespace(tmp_path, native, legacy)

    assert namespace["_load_outcomes"]()[card] == (
        native[card][0]["ts"],
        "PASS",
    )
    assert namespace["awaiting_review"](card) is False
    assert namespace["blocked_backoff"](card) is True


@pytest.mark.parametrize(
    "event",
    [
        {"verdict": "BLOCKED", "blocked_on": "capability"},
        {
            "verdict": "BLOCKED blocked_on=human referent=approval:x",
            "blocked_on": "capability",
            "referent": "ac:1",
        },
        {"verdict": "PASS", "blocked_on": "capability", "referent": "ac:1"},
        {"payload": {"verdict": "maybe"}},
    ],
)
def test_malformed_or_ambiguous_native_verdict_fails_closed(tmp_path: Path, event: dict) -> None:
    card = "eeeeeeee"
    row = {"ts": "2026-08-29T01:00:00+00:00", "action": "verdict", **event}
    namespace = _native_namespace(tmp_path, {card: [row]})
    assert namespace["_load_outcomes"]()[card][1] == "BLOCKED native_outcome_invalid=true"
    assert namespace["blocked_backoff"](card) is True
    assert namespace["needs_escalation"](card, {}) is False


def test_native_and_legacy_outcomes_have_deterministic_order(tmp_path: Path) -> None:
    card = "ffffffff"
    common = {
        "card_id": card,
        "ts": "2026-08-29T02:00:00+00:00",
        "writer": "same-writer",
        "event_id": "same-event",
    }
    legacy = [
        {
            **common,
            "action": "link",
            "link_key": "verdict",
            "link_value": "PASS_FOR_REVIEW",
        },
        {
            "card_id": card,
            "ts": "2026-08-29T03:00:00+00:00",
            "action": "link",
            "link_key": "verdict_artifact",
            "link_value": "/tmp/BLOCKED.json",
        },
    ]
    native = {
        card: [
            {
                **common,
                "action": "verdict",
                "verdict": "BLOCKED",
                "blocked_on": "capability",
                "referent": "ac:1",
            }
        ]
    }
    namespace = _native_namespace(tmp_path, native, legacy)
    assert namespace["_load_outcomes"]()[card] == (
        common["ts"],
        "BLOCKED blocked_on=capability referent=ac:1",
    )


def test_exact_native_duplicate_identity_collapses(tmp_path: Path) -> None:
    card = "abababab"
    row = {
        "event_id": "same-event",
        "ts": "2026-08-29T03:00:00+00:00",
        "writer": "same-writer",
        "action": "verdict",
        "verdict": "BLOCKED",
        "blocked_on": "capability",
        "referent": "ac:1",
    }
    namespace = _native_namespace(tmp_path, {card: [row, dict(row)]})
    assert namespace["_load_outcomes"]()[card] == (
        row["ts"],
        "BLOCKED blocked_on=capability referent=ac:1",
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_native_duplicate_identity_fails_closed_in_any_order(
    tmp_path: Path, reverse: bool
) -> None:
    card = "bcbcbcbc"
    common = {
        "event_id": "same-event",
        "ts": "2026-08-29T03:00:00+00:00",
        "writer": "same-writer",
        "action": "verdict",
    }
    rows = [
        {**common, "verdict": "PASS_FOR_REVIEW"},
        {
            **common,
            "verdict": "BLOCKED",
            "blocked_on": "capability",
            "referent": "ac:1",
        },
    ]
    namespace = _native_namespace(tmp_path, {card: list(reversed(rows)) if reverse else rows})
    assert namespace["_load_outcomes"]()[card] == (
        common["ts"],
        "BLOCKED native_outcome_invalid=true",
    )


def test_conflicting_native_duplicate_identity_across_files_fails_closed(
    tmp_path: Path,
) -> None:
    card = "cdcdcdcd"
    common = {
        "event_id": "same-event",
        "ts": "2026-08-29T03:00:00+00:00",
        "writer": "same-writer",
    }
    namespace = _native_file_namespace(
        tmp_path,
        card,
        (
            ("a.jsonl", [{**common, "action": "verdict", "verdict": "PASS"}]),
            ("b.jsonl", [{**common, "action": "move", "column": "done"}]),
        ),
    )
    assert namespace["_load_outcomes"]()[card] == (
        common["ts"],
        "BLOCKED native_outcome_invalid=true",
    )


@pytest.mark.parametrize(
    ("card", "event_id", "rows"),
    [
        (
            "5185714f",
            "$(uuidgen 2>/dev/null || echo 'manual-'$(date +%s))",
            (
                {"action": "evidence", "verdict": "BLOCKED", "blocked_on": "human"},
                {"action": "evidence_link", "referent": "approval:tailscale-admin-delete"},
                {"action": "verdict", "verdict": "BLOCKED", "blocked_on": "human"},
            ),
        ),
        (
            "79bb2305",
            "h0i1j2k3l4m5n6o7p8q9r0s1t2u3v4w5",
            (
                {"action": "verdict", "verdict": "PASS"},
                {"action": "verdict", "verdict": "BLOCKED", "blocked_on": "card"},
            ),
        ),
    ],
)
def test_current_live_duplicate_id_fixtures_fail_closed(
    tmp_path: Path, card: str, event_id: str, rows: tuple[dict, ...]
) -> None:
    common = {
        "event_id": event_id,
        "ts": "2026-08-28T03:00:06.000000+00:00",
        "writer": "fixture-writer",
    }
    namespace = _native_namespace(tmp_path, {card: [{**common, **row} for row in rows]})
    assert namespace["_load_outcomes"]()[card] == (
        common["ts"],
        "BLOCKED native_outcome_invalid=true",
    )


def test_exact_567_timeline_wakes_then_exhausts_one_generation(board: BackoffHarness) -> None:
    card = "567e6b09"
    referent = "482cc241"
    verdict = "2026-08-28T11:11:59.211280+00:00"
    resolved = "2026-08-28T11:26:03.352672+00:00"
    board.core(referent)
    board.states[referent] = "complete"
    board.satisfied[referent] = True
    board.event(referent, resolved, "complete")
    board.outcome(card, verdict, "BLOCKED blocked_on=card referent=card:482cc241")

    # The historical 20:02 launch had no claim revision, so it cannot consume
    # the exact edge. The repaired launcher records a strict launch generation.
    board.ns["_launched_at"][card] = board.epoch("2026-08-28T20:02:00Z")
    assert board.blocked(card) is False
    assert board.blocked(card) is False

    board.ns["_wake_launch_times"][card] = [board.epoch("2026-08-28T20:30:00Z")]
    assert board.blocked(card) is True

    board.outcome(card, "2026-08-28T20:31:00Z", "BLOCKED|card|card:482cc241")
    assert board.blocked(card) is True


def test_dependency_requires_exact_edge_and_satisfied_referent(board: BackoffHarness) -> None:
    card = "83e04cf1"
    dep = "2076c423"
    board.core(dep)
    board.states[dep] = "complete"
    board.satisfied[dep] = True
    board.event(dep, "2026-08-28T02:00:00Z", "complete")
    board.outcome(card, "2026-08-28T01:00:00Z", f"BLOCKED|dependency|card:{dep}")
    assert board.blocked(card) is True

    board.dependencies[card] = [dep]
    board.event(card, "2026-08-28T01:30:00Z", "add_dependency", dependency=dep)
    assert board.blocked(card) is False

    board.satisfied[dep] = False
    assert board.blocked(card) is True


def test_exact_dependency_removal_is_a_material_change(board: BackoffHarness) -> None:
    card = "bbbbbbbb"
    dep = "cccccccc"
    board.outcome(card, "2026-08-28T01:00:00Z", f"BLOCKED|dependency|card:{dep}")
    board.event(card, "2026-08-28T02:00:00Z", "remove_dependency", dependency=dep)
    assert board.blocked(card) is False


@pytest.mark.parametrize("action", ["describe", "amend_criteria"])
def test_card_criterion_wakes_only_after_authored_contract_change(
    board: BackoffHarness, action: str
) -> None:
    card = "dddddddd"
    board.outcome(card, "2026-08-28T01:00:00Z", "BLOCKED|card|ac:2")
    board.event("eeeeeeee", "2026-08-28T02:00:00Z", action)
    assert board.blocked(card) is True

    board.event(card, "2026-08-28T03:00:00Z", action)
    assert board.blocked(card) is False
    board.ns["_wake_launch_times"][card] = [board.epoch("2026-08-28T04:00:00Z")]
    assert board.blocked(card) is True


def test_human_wake_requires_exact_explicit_approval_or_void(board: BackoffHarness) -> None:
    card = "11111111"
    referent = "approval:tailscale-admin-delete"
    board.outcome(card, "2026-08-28T01:00:00Z", f"BLOCKED|human|{referent}")
    board.evidence_event(
        card,
        "2026-08-28T02:00:00Z",
        "link",
        link_key="successor-review",
        link_value="PASS",
    )
    assert board.blocked(card) is True

    board.evidence_event(
        card,
        "2026-08-28T03:00:00Z",
        "link",
        link_key="human-decision",
        link_value=f"APPROVE {referent} by Chef",
        writer="Chef",
    )
    assert board.blocked(card) is False

    gate = "22222222"
    board.core(gate, ("human-gate",), "[HUMAN] exact gate")
    board.outcome(card, "2026-08-28T04:00:00Z", f"BLOCKED|human|card:{gate}")
    board.event(gate, "2026-08-28T05:00:00Z", "void", writer="chef")
    assert board.blocked(card) is False


def test_capability_uses_one_stronger_route_generation(board: BackoffHarness) -> None:
    card = "33333333"
    verdict = "2026-08-28T01:00:00Z"
    board.outcome(card, verdict, "BLOCKED|capability|ac:4")
    assert board.blocked(card) is False

    board.ns["_strong_launched_at"][card] = board.epoch("2026-08-28T02:00:00Z")
    assert board.blocked(card) is True

    board.evidence_event(
        card,
        "2026-08-28T03:00:00Z",
        "add_label",
        label="needs-stronger-model",
    )
    assert board.blocked(card) is False
    board.ns["_strong_launched_at"][card] = board.epoch("2026-08-28T04:00:00Z")
    assert board.blocked(card) is True


@pytest.mark.parametrize("value", ["PASS_FOR_REVIEW", "PASS_FOR_REREVIEW"])
def test_pass_outcomes_remain_awaiting_review(board: BackoffHarness, value: str) -> None:
    card = "44444444"
    board.outcome(card, "2026-08-28T01:00:00Z", value)
    board.event(card, "2026-08-28T02:00:00Z", "amend_criteria")
    assert board.blocked(card) is True
    assert board.ns["awaiting_review"](card) is True


def test_plain_pass_requires_workflow_closure_not_review(board: BackoffHarness) -> None:
    card = "44444444"
    board.outcome(card, "2026-08-28T01:00:00Z", "PASS")
    assert board.ns["awaiting_review"](card) is False
    assert board.ns["outcome_workflow_class"](card, {}) == "workflow_closure_required"


def test_no_change_and_unrelated_traffic_do_not_wake(board: BackoffHarness) -> None:
    card = "55555555"
    board.outcome(card, "2026-08-28T01:00:00Z", "BLOCKED|card|ac:1")
    board.event("66666666", "2026-08-28T02:00:00Z", "amend_criteria")
    board.evidence_event(
        "66666666", "2026-08-28T03:00:00Z", "add_label", label="needs-stronger-model"
    )
    assert board.blocked(card) is True
