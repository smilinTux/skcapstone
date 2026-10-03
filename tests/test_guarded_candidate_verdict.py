"""The unattended relay writes only one outcome for an unchanged owned claim."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from skcapstone.card_store import CardStore
from skcapstone.provisional_verdict import candidate_evidence, record_guarded_verdict
from skcapstone.seraph_review_cardstore import LiveCardStoreGateway
from tests.test_provisional_verdict_producer import (
    COMMIT,
    REF,
    TREE,
    _candidate,
    _native_events,
    _opener,
    _run,
    _seed,
)


def setup_guard(home: Path):
    """Create an isolated claimed card and read its authoritative revision."""
    card, owner, claim = "1234abcd", "pi-guard-test", "c" * 32
    _seed(home, card)
    store = CardStore(home)
    store.append_event(card, "claim", owner, owner=owner, claim_revision=claim)
    artifact = _candidate(home, card)
    revision = LiveCardStoreGateway(home).read_card(card).revision
    args = [
        "verdict",
        card,
        "PASS_FOR_REVIEW",
        "--candidate",
        str(artifact),
        "--commit",
        COMMIT,
        "--tree",
        TREE,
        "--ref",
        REF,
        "--agent",
        owner,
        "--expected-source-revision",
        revision,
        "--expected-claim-revision",
        claim,
        "--transition-id",
        "d" * 64,
    ]
    return store, card, owner, artifact, args


def verdicts(home: Path, card: str):
    """Use the fixture's scoped native reader, never the live board."""
    return [event for event in _native_events(home, card) if event["action"] == "verdict"]


def test_guarded_cli_retries_without_duplicate_outcome(tmp_path: Path):
    store, card, owner, artifact, args = setup_guard(tmp_path)
    first = _run(tmp_path, *args)
    assert first.exit_code == 0, first.output
    second = _run(tmp_path, *args)
    assert second.exit_code == 0, second.output
    events = verdicts(tmp_path, card)
    assert len(events) == 1
    assert events[0]["writer"] == owner
    assert events[0]["candidate_commit"] == COMMIT
    assert events[0]["transition_id"] == "d" * 64
    assert store.fold(card).owner == owner
    generation = _opener(tmp_path)["_parent_review_generation"](
        card, events[0]["ts"], "PASS_FOR_REVIEW"
    )
    assert generation is not None
    assert generation[1] == owner
    assert generation[4:7] == (COMMIT, TREE, REF)


@pytest.mark.parametrize("change", ["owner", "claim", "contract", "evidence", "outcome", "done"])
def test_stale_inputs_refuse_before_outcome_write(tmp_path: Path, change: str):
    store, card, owner, artifact, args = setup_guard(tmp_path)
    if change == "owner":
        store.append_event(card, "unassign", "operator")
        store.append_event(card, "claim", "other", owner="other", claim_revision="e" * 32)
    elif change == "claim":
        store.append_event(card, "claim", owner, owner=owner, claim_revision="e" * 32)
    elif change == "contract":
        store.append_event(card, "describe", "operator", description="changed contract")
    elif change == "evidence":
        store.append_event(card, "link", owner, link_key="evidence_sha256", link_value="a" * 64)
    elif change == "outcome":
        store.append_event(card, "verdict", owner, verdict="FAIL")
    else:
        store.append_event(card, "complete", owner)
    before = verdicts(tmp_path, card)
    result = _run(tmp_path, *args)
    assert result.exit_code != 0
    assert verdicts(tmp_path, card) == before


@pytest.mark.parametrize("change", ["artifact", "head", "new-outcome", "contract", "claim"])
def test_idempotency_never_adopts_conflicting_or_superseded_outcome(tmp_path: Path, change: str):
    store, card, owner, artifact, args = setup_guard(tmp_path)
    assert _run(tmp_path, *args).exit_code == 0
    if change == "artifact":
        artifact.write_text("changed candidate\n")
    elif change == "head":
        args[args.index("--commit") + 1] = "e" * 40
    elif change == "new-outcome":
        store.append_event(card, "verdict", owner, verdict="FAIL")
    elif change == "contract":
        store.append_event(card, "describe", "operator", description="changed contract")
    else:
        store.append_event(card, "claim", owner, owner=owner, claim_revision="e" * 32)
    before = verdicts(tmp_path, card)
    result = _run(tmp_path, *args)
    assert result.exit_code != 0
    assert verdicts(tmp_path, card) == before


@pytest.mark.parametrize(
    "flag", ["--expected-source-revision", "--expected-claim-revision", "--transition-id"]
)
def test_partial_guard_is_not_silently_downgraded(tmp_path: Path, flag: str):
    store, card, owner, artifact, args = setup_guard(tmp_path)
    index = args.index(flag)
    del args[index : index + 2]
    result = _run(tmp_path, *args)
    assert result.exit_code != 0
    assert verdicts(tmp_path, card) == []


def test_claim_change_between_preparation_and_write_is_rechecked_under_lock(
    tmp_path: Path, monkeypatch
):
    store, card, owner, artifact, args = setup_guard(tmp_path)
    import skcapstone.provisional_verdict as module

    real = candidate_evidence

    def raced(*values):
        result = real(*values)
        store.append_event(card, "claim", owner, owner=owner, claim_revision="e" * 32)
        return result

    monkeypatch.setattr(module, "candidate_evidence", raced)
    result = _run(tmp_path, *args)
    assert result.exit_code != 0
    assert verdicts(tmp_path, card) == []


def test_candidate_change_after_preparation_is_refused_before_write(tmp_path: Path):
    store, card, owner, artifact, args = setup_guard(tmp_path)
    payload = candidate_evidence(artifact, COMMIT, TREE, REF)
    artifact.write_text("changed after preparation\n")
    with pytest.raises(ValueError, match="candidate changed"):
        record_guarded_verdict(
            tmp_path,
            card,
            owner,
            "PASS_FOR_REVIEW",
            payload,
            expected_source_revision=args[args.index("--expected-source-revision") + 1],
            expected_claim_revision="c" * 32,
            transition_id="d" * 64,
        )
    assert verdicts(tmp_path, card) == []


def test_write_then_error_recovers_the_same_native_event(tmp_path: Path, monkeypatch):
    store, card, owner, artifact, args = setup_guard(tmp_path)
    original = CardStore.append_event

    def lost_response(self, card_id, action, agent, **payload):
        event = original(self, card_id, action, agent, **payload)
        if action == "verdict":
            raise ValueError("synthetic lost acknowledgement")
        return event

    monkeypatch.setattr(CardStore, "append_event", lost_response)
    assert _run(tmp_path, *args).exit_code != 0
    assert len(verdicts(tmp_path, card)) == 1
    monkeypatch.setattr(CardStore, "append_event", original)
    recovered = _run(tmp_path, *args)
    assert recovered.exit_code == 0, recovered.output
    assert len(verdicts(tmp_path, card)) == 1


def test_concurrent_same_transition_appends_once(tmp_path: Path):
    store, card, owner, artifact, args = setup_guard(tmp_path)
    payload = candidate_evidence(artifact, COMMIT, TREE, REF)
    revision = args[args.index("--expected-source-revision") + 1]

    def relay(_):
        return record_guarded_verdict(
            tmp_path,
            card,
            owner,
            "PASS_FOR_REVIEW",
            payload,
            expected_source_revision=revision,
            expected_claim_revision="c" * 32,
            transition_id="d" * 64,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(relay, range(2)))
    assert first["event_id"] == second["event_id"]
    assert len(verdicts(tmp_path, card)) == 1


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--expected-source-revision", "invalid"),
        ("--expected-claim-revision", "e" * 64),
        ("--transition-id", "../unsafe"),
    ],
)
def test_malformed_guard_refuses_before_board_write(tmp_path: Path, flag: str, value: str):
    store, card, owner, artifact, args = setup_guard(tmp_path)
    args[args.index(flag) + 1] = value
    result = _run(tmp_path, *args)
    assert result.exit_code != 0
    assert "guarded verdict invalid" in result.output
    assert verdicts(tmp_path, card) == []


def test_controller_expected_candidate_digest_is_checked_before_write(tmp_path: Path):
    import hashlib

    store, card, owner, artifact, args = setup_guard(tmp_path)
    args += ["--expected-candidate-sha256", hashlib.sha256(artifact.read_bytes()).hexdigest()]
    artifact.write_text("changed before the CLI read\n")
    result = _run(tmp_path, *args)
    assert result.exit_code != 0
    assert "guarded verdict candidate digest changed" in result.output
    assert verdicts(tmp_path, card) == []


def test_expected_candidate_digest_requires_guarded_mode(tmp_path: Path):
    store, card, owner, artifact, args = setup_guard(tmp_path)
    for flag in ("--expected-source-revision", "--expected-claim-revision", "--transition-id"):
        index = args.index(flag)
        del args[index : index + 2]
    result = _run(tmp_path, *args, "--expected-candidate-sha256", "a" * 64)
    assert result.exit_code != 0
    assert "requires revision guards" in result.output
    assert verdicts(tmp_path, card) == []
