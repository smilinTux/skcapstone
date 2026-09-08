"""CardStore-bound fleet worker completion tests."""

from __future__ import annotations

import argparse
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from click.testing import CliRunner

from skcapstone.card_store import CardCore, CardStore
from skcapstone.cli import main

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts" / "fleet" / "skfleet-worker-wrapper.py"


def wrapper():
    """Load the worker wrapper as an isolated module."""
    spec = importlib.util.spec_from_file_location("cardstore_wrapper", WRAPPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def args() -> argparse.Namespace:
    """Return one exact provider-neutral launch identity."""
    return argparse.Namespace(
        card="feedbeef",
        owner="worker",
        claim_revision="revision-1",
        session="worker-auto-feedbeef",
        attempt_id="attempt-1",
    )


def cardstore(
    home: Path,
    verdict: str = "PASS_FOR_REVIEW",
    complete: bool = False,
    *,
    attempt_id: str = "attempt-1",
    session_id: str = "worker-auto-feedbeef",
) -> None:
    """Write one authoritative card lifecycle through CardStore APIs."""
    (home / ".skcapstone").mkdir()
    store = CardStore(home / ".skcapstone")
    store.create(CardCore(id="feedbeef", title="synthetic"))
    store.append_event("feedbeef", "claim", "worker", owner="worker", claim_revision="revision-1")
    identity = {
        "claim_revision": "revision-1",
        "attempt_id": attempt_id,
        "session_id": session_id,
    }
    store.append_event(
        "feedbeef", "link", "worker", link_key="evidence", link_value="evidence.md", **identity
    )
    store.append_event(
        "feedbeef", "link", "worker", link_key="verdict", link_value=verdict, **identity
    )
    if complete:
        store.append_event("feedbeef", "complete", "worker")


def test_coord_link_records_exact_fleet_outcome_identity(tmp_path: Path) -> None:
    store = CardStore(tmp_path)
    store.create(CardCore(id="feedbeef", title="synthetic"))
    environment = {
        "SKAGENT": "worker",
        "SKFLEET_CARD_ID": "feedbeef",
        "SKFLEET_CLAIM_REVISION": "revision-1",
        "SKFLEET_ATTEMPT_ID": "attempt-1",
        "SKFLEET_SESSION_ID": "session-1",
    }
    result = CliRunner().invoke(
        main,
        [
            "coord",
            "link",
            "feedbeef",
            "verdict",
            "PASS_FOR_REVIEW",
            "--home",
            str(tmp_path),
            "--agent",
            "worker",
        ],
        env=environment,
    )
    assert result.exit_code == 0, result.output
    (event,) = store._read_events("feedbeef")
    assert {key: event[key] for key in ("claim_revision", "attempt_id", "session_id")} == {
        "claim_revision": "revision-1",
        "attempt_id": "attempt-1",
        "session_id": "session-1",
    }


def test_coord_link_rejects_cross_attempt_card(tmp_path: Path) -> None:
    store = CardStore(tmp_path)
    store.create(CardCore(id="feedbeef", title="synthetic"))
    result = CliRunner().invoke(
        main,
        [
            "coord",
            "link",
            "feedbeef",
            "verdict",
            "PASS",
            "--home",
            str(tmp_path),
            "--agent",
            "worker",
        ],
        env={
            "SKAGENT": "worker",
            "SKFLEET_CARD_ID": "other-card",
            "SKFLEET_CLAIM_REVISION": "revision-1",
            "SKFLEET_ATTEMPT_ID": "attempt-1",
            "SKFLEET_SESSION_ID": "session-1",
        },
    )
    assert result.exit_code != 0
    assert store._read_events("feedbeef") == []


@pytest.mark.parametrize("complete", [False, True])
def test_current_or_just_completed_exact_claim_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, complete: bool
) -> None:
    module = wrapper()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cardstore(tmp_path, complete=complete)
    assert module.validate_cardstore_completion(args()) == (
        True,
        "valid_cardstore_completion",
        "PASS_FOR_REVIEW",
    )


@pytest.mark.parametrize("verdict", ["BLOCKED", "FAIL_CLOSED"])
def test_blocked_outcomes_never_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdict: str
) -> None:
    module = wrapper()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cardstore(tmp_path, verdict=verdict)
    assert module.validate_cardstore_completion(args()) == (
        False,
        "blocked_outcome",
        verdict,
    )


def test_stale_claim_never_completes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = wrapper()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cardstore(tmp_path)
    store = CardStore(tmp_path / ".skcapstone")
    store.append_event(
        "feedbeef",
        "release_claim",
        "worker",
        released_owner="worker",
        expected_claim_revision="revision-1",
    )
    store.append_event(
        "feedbeef", "claim", "new-worker", owner="new-worker", claim_revision="revision-2"
    )
    assert module.validate_cardstore_completion(args())[:2] == (False, "stale_claim")


def test_exact_release_readback_rejects_released_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = wrapper()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cardstore(tmp_path)
    store = CardStore(tmp_path / ".skcapstone")
    store.append_event(
        "feedbeef",
        "release_claim",
        "worker",
        released_owner="worker",
        expected_claim_revision="revision-1",
    )
    assert store.fold("feedbeef").owner is None
    assert module.validate_cardstore_completion(args())[:2] == (False, "stale_claim")


def test_same_claim_concurrent_attempts_only_bound_attempt_consumes_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = wrapper()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cardstore(tmp_path)
    current = args()
    stale = args()
    stale.attempt_id = "attempt-2"
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(module.validate_cardstore_completion, [current, stale]))
    assert results[0][0] is True
    assert results[1][0] is False


def test_same_claim_cross_session_cannot_consume_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = wrapper()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cardstore(tmp_path)
    identity = args()
    identity.session = "other-session"
    assert module.validate_cardstore_completion(identity)[:2] == (
        False,
        "cross_session_outcome",
    )


def test_missing_bound_evidence_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = wrapper()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".skcapstone").mkdir()
    store = CardStore(tmp_path / ".skcapstone")
    store.create(CardCore(id="feedbeef", title="synthetic"))
    store.append_event("feedbeef", "claim", "worker", owner="worker", claim_revision="revision-1")
    store.append_event(
        "feedbeef",
        "link",
        "worker",
        link_key="verdict",
        link_value="PASS_FOR_REVIEW",
        claim_revision="revision-1",
        attempt_id="attempt-1",
        session_id="worker-auto-feedbeef",
    )
    assert module.validate_cardstore_completion(args())[:2] == (False, "missing_evidence")


def test_duplicate_bound_verdict_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = wrapper()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cardstore(tmp_path)
    store = CardStore(tmp_path / ".skcapstone")
    store.append_event(
        "feedbeef",
        "link",
        "worker",
        link_key="verdict",
        link_value="PASS_FOR_REVIEW",
        claim_revision="revision-1",
        attempt_id="attempt-1",
        session_id="worker-auto-feedbeef",
    )
    assert module.validate_cardstore_completion(args())[:2] == (
        False,
        "duplicate_terminal_outcome",
    )


def test_legacy_unbound_outcome_fails_closed_as_cross_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = wrapper()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".skcapstone").mkdir()
    store = CardStore(tmp_path / ".skcapstone")
    store.create(CardCore(id="feedbeef", title="synthetic"))
    store.append_event("feedbeef", "claim", "worker", owner="worker", claim_revision="revision-1")
    store.append_event("feedbeef", "link", "worker", link_key="evidence", link_value="evidence.md")
    store.append_event(
        "feedbeef", "link", "worker", link_key="verdict", link_value="PASS_FOR_REVIEW"
    )
    assert module.validate_cardstore_completion(args())[0] is False


def test_superseded_claim_outcome_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = wrapper()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cardstore(tmp_path)
    store = CardStore(tmp_path / ".skcapstone")
    store.append_event(
        "feedbeef",
        "release_claim",
        "worker",
        released_owner="worker",
        expected_claim_revision="revision-1",
    )
    store.append_event("feedbeef", "claim", "worker", owner="worker", claim_revision="revision-2")
    assert module.validate_cardstore_completion(args())[:2] == (False, "stale_claim")


def test_exact_outcome_can_only_be_consumed_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = wrapper()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cardstore(tmp_path)
    first = module.validate_cardstore_completion(args())
    second = module.validate_cardstore_completion(args())
    assert first[0] is True
    assert second[:2] == (False, "claim_outcome_already_consumed")


def test_two_bound_attempts_in_one_claim_cannot_both_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = wrapper()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cardstore(tmp_path)
    store = CardStore(tmp_path / ".skcapstone")
    second_identity = {
        "claim_revision": "revision-1",
        "attempt_id": "attempt-2",
        "session_id": "worker-auto-feedbeef",
    }
    store.append_event(
        "feedbeef",
        "link",
        "worker",
        link_key="evidence",
        link_value="evidence-2.md",
        **second_identity,
    )
    store.append_event(
        "feedbeef",
        "link",
        "worker",
        link_key="verdict",
        link_value="PASS_FOR_REVIEW",
        **second_identity,
    )
    first = args()
    second = args()
    second.attempt_id = "attempt-2"
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(module.validate_cardstore_completion, [first, second]))
    assert sum(result[0] for result in results) == 1
    assert {result[1] for result in results} == {
        "valid_cardstore_completion",
        "claim_outcome_already_consumed",
    }


def test_failure_receipt_has_attempt_identity_without_secret(tmp_path: Path) -> None:
    module = wrapper()
    stdout = tmp_path / "worker.log"
    stdout.write_text("ordinary worker prose")
    identity = args()
    identity.host = "host-1"
    identity.lane = "future"
    identity.model = "logical-model"
    identity.stdout = stdout
    identity.evidence_dir = tmp_path / "exits"
    module.record_terminal_exit(
        identity,
        b"Authorization: Bearer secret-secret-secret",
        0,
        (False, "missing_evidence", "PASS"),
    )
    (receipt,) = identity.evidence_dir.glob("*.json")
    payload = json.loads(receipt.read_text())
    assert payload["attempt_id"] == "attempt-1"
    assert payload["session_id"] == "worker-auto-feedbeef"
    assert payload["claim_revision"] == "revision-1"
    assert "secret-secret-secret" not in receipt.read_text()


@pytest.mark.parametrize(
    ("decision", "expected_kind", "expected_rc"),
    [
        ((True, "valid_cardstore_completion", "PASS"), "work.complete", 0),
        ((False, "blocked_outcome", "BLOCKED"), "work.blocked", 75),
        ((False, "stale_claim", "PASS"), "work.blocked", 75),
        ((False, "missing_evidence", "PASS"), "work.blocked", 75),
    ],
)
def test_main_emits_only_truthful_lifecycle_mail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decision: tuple[bool, str, str | None],
    expected_kind: str,
    expected_rc: int,
) -> None:
    module = wrapper()
    identity = args()
    identity.host = "host-1"
    identity.lane = "future"
    identity.model = "logical-model"
    identity.stdout = tmp_path / "worker.log"
    identity.evidence_dir = tmp_path / "exits"
    identity.mail_recipient = "jarvis"
    identity.worker_executable = ""
    identity.startup_timeout = 1.0
    identity.command = ["/bin/true"]
    notices: list[str] = []
    monkeypatch.setattr(module, "parse_args", lambda: identity)
    monkeypatch.setattr(module, "preflight_worktree", lambda: 0)
    monkeypatch.setattr(module, "idle_owner_projection", lambda _owner: None)
    monkeypatch.setattr(module, "emit_work_mail", lambda _args, kind, _body: notices.append(kind))
    monkeypatch.setattr(module, "validate_cardstore_completion", lambda _args: decision)
    assert module.main() == expected_rc
    assert notices[-1] == expected_kind
    assert ("work.complete" in notices) is (expected_kind == "work.complete")


@pytest.mark.parametrize("lane", ["sk-s", "sk-m", "sk-l", "sk-xl", "qwen"])
def test_completion_contract_is_provider_neutral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane: str
) -> None:
    module = wrapper()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cardstore(tmp_path)
    identity = args()
    identity.lane = lane
    assert module.validate_cardstore_completion(identity)[0] is True
