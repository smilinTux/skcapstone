"""Claim-time churn breaker, exercised against a real CardStore.

Every fixture here writes through ``CardStore.create`` and
``CardStore.append_event`` and reads ownership back through ``CardStore.fold``.
No hand-written JSONL, and no hand-rolled replay: a recent bug in this repo
shipped because a local replay diverged from the real fold on 75 of 143 cards,
and the tests could not see it because they had written the events themselves.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone.fleet import churn_breaker as cb

ENFORCE = {"SKFLEET_CHURN_BREAKER_MODE": "enforce"}
REPORT = {"SKFLEET_CHURN_BREAKER_MODE": "report"}


def _ready_card(home: Path, card_id: str, title: str = "churn fixture") -> CardStore:
    store = CardStore(home)
    store.create(CardCore(id=card_id, kind="task", title=title, created_by="test"))
    store.append_event(card_id, "move", "test-seat", column="ready")
    return store


def _claim(store: CardStore, card_id: str, owner: str) -> str:
    """One claim attempt, returning the claim revision the fold will hold."""
    return store.append_event(card_id, "claim", owner, owner=owner)["event_id"]


def _release(store: CardStore, card_id: str, owner: str, revision: str) -> None:
    """A release that the fold actually honours.

    The fold refuses a release whose ``released_owner`` and
    ``expected_claim_revision`` do not match the held claim, and records a
    release_conflict instead. Getting this wrong leaves the card owned, which
    would quietly turn every cycle below into a same-owner re-claim and hide
    the very distinction these tests exist to prove.
    """
    store.append_event(
        card_id,
        "release_claim",
        owner,
        released_owner=owner,
        expected_claim_revision=revision,
        abandon_reason="dependency-unsatisfied",
    )


def _cycle(store: CardStore, card_id: str, owner: str) -> None:
    """A full claim then release, which is one wasted dispatch."""
    _release(store, card_id, owner, _claim(store, card_id, owner))


# ----------------------------------------------------------------------
# Constraint 4: a legitimate re-claim must not be counted as churn.
# ----------------------------------------------------------------------


def test_same_owner_reclaim_of_a_held_card_is_one_attempt(tmp_path) -> None:
    """Ten claim events by the holder are one dispatch, not ten.

    A worker re-claiming a card it already holds is normal and writes a second
    claim event; the fold settles both with one release. This is the case a
    naive event count converts into a churn signature out of nothing.
    """
    card_id = f"{1:08x}"
    store = _ready_card(tmp_path, card_id)
    for _ in range(10):
        _claim(store, card_id, "pi-codex-chiap08")

    signature = cb.read_signature(tmp_path, card_id)

    assert store.fold(card_id).owner == "pi-codex-chiap08"
    assert signature.claim_attempts == 1
    assert signature.distinct_owners == 1
    verdict = cb.evaluate(signature, max_claim_attempts=5, max_owners=3)
    assert verdict.refuse is False
    assert verdict.reason == "under-threshold"
    assert (
        cb.assert_claim_permitted(tmp_path, card_id, "pi-codex-chiap08", ENFORCE).refuse is False
    )


def test_claim_release_cycles_by_one_owner_do_count(tmp_path) -> None:
    """A release ends the hold, so the next claim is a fresh wasted dispatch.

    This is the 83e498b6 shape: one seat re-claiming every ten minutes. It must
    NOT be excused by the same-owner rule, which only excuses a re-claim of a
    card still held.
    """
    card_id = f"{2:08x}"
    store = _ready_card(tmp_path, card_id)
    for _ in range(5):
        _cycle(store, card_id, "pi-codex-chiap08")

    signature = cb.read_signature(tmp_path, card_id)

    assert store.fold(card_id).owner is None
    assert signature.claim_attempts == 5
    assert signature.distinct_owners == 1
    assert cb.evaluate(signature, max_claim_attempts=5, max_owners=3).reason == "churn-claims"


def test_three_distinct_owners_trip_the_breaker_before_the_claim_count(tmp_path) -> None:
    card_id = f"{3:08x}"
    store = _ready_card(tmp_path, card_id)
    for owner in ("seat-a", "seat-b", "seat-c"):
        _cycle(store, card_id, owner)

    signature = cb.read_signature(tmp_path, card_id)

    assert (signature.claim_attempts, signature.distinct_owners) == (3, 3)
    assert cb.evaluate(signature, max_claim_attempts=5, max_owners=3).reason == "churn-owners"
    with pytest.raises(cb.ClaimRefusedError) as caught:
        cb.assert_claim_permitted(tmp_path, card_id, "seat-d", ENFORCE)
    assert "blocked_on" in str(caught.value)


# ----------------------------------------------------------------------
# Recording a blocker is what re-permits the card.
# ----------------------------------------------------------------------


def test_recorded_blocker_on_the_card_re_permits_the_claim(tmp_path) -> None:
    card_id = f"{4:08x}"
    store = _ready_card(tmp_path, card_id)
    for owner in ("seat-a", "seat-b", "seat-c"):
        _cycle(store, card_id, owner)
    store.append_event(
        card_id,
        "link",
        "seat-c",
        link_key="blocked_on",
        link_value="human referent=approval:tailscale-admin-console",
    )

    signature = cb.read_signature(tmp_path, card_id)

    assert signature.blocker is not None
    assert cb.evaluate(signature, max_claim_attempts=5, max_owners=3).reason == "blocker-recorded"
    assert cb.assert_claim_permitted(tmp_path, card_id, "seat-d", ENFORCE).refuse is False


def test_blocked_verdict_in_the_evidence_store_re_permits_the_claim(tmp_path) -> None:
    """Verdicts land in coordination/card_events, not in the card's own shard.

    ``coord link`` and ``verdict`` write only to the evidence overlay. A
    breaker that read the fold alone would be blind to the exact artefact it
    tells workers to produce, and would refuse a card that had already
    explained itself.
    """
    card_id = f"{5:08x}"
    store = _ready_card(tmp_path, card_id)
    for owner in ("seat-a", "seat-b", "seat-c"):
        _cycle(store, card_id, owner)
    events = tmp_path / "coordination" / "card_events"
    events.mkdir(parents=True)
    (events / "noroc2027.jsonl").write_text(
        json.dumps(
            {
                "card_id": card_id,
                "ts": "2026-09-18T07:00:00+00:00",
                "action": "link",
                "link_key": "verdict",
                "link_value": (
                    "BLOCKED. blocked_on=human referent=approval:tailscale-admin-console"
                ),
                "writer": "seat-c",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    signature = cb.read_signature(tmp_path, card_id)

    assert signature.blocker is not None and "tailscale" in signature.blocker
    assert cb.assert_claim_permitted(tmp_path, card_id, "seat-d", ENFORCE).refuse is False


def test_a_later_pass_supersedes_the_block_and_the_breaker_refuses_again(tmp_path) -> None:
    """Only the latest outcome counts, or a discharged block would hold forever."""
    card_id = f"{6:08x}"
    store = _ready_card(tmp_path, card_id)
    for owner in ("seat-a", "seat-b", "seat-c"):
        _cycle(store, card_id, owner)
    events = tmp_path / "coordination" / "card_events"
    events.mkdir(parents=True)
    rows = [
        {
            "card_id": card_id,
            "ts": "2026-09-18T07:00:00+00:00",
            "link_key": "verdict",
            "link_value": "BLOCKED. blocked_on=human referent=approval:tailscale",
        },
        {
            "card_id": card_id,
            "ts": "2026-09-18T09:00:00+00:00",
            "link_key": "verdict",
            "link_value": "PASS. the operator opened the console",
        },
    ]
    (events / "noroc2027.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )

    assert cb.read_signature(tmp_path, card_id).blocker is None
    with pytest.raises(cb.ClaimRefusedError):
        cb.assert_claim_permitted(tmp_path, card_id, "seat-d", ENFORCE)


def test_a_bare_category_does_not_re_permit_the_claim(tmp_path) -> None:
    """ "blocked_on: human" says a person is needed without saying which decision.

    ``validate_blocked_verdict`` already refuses that shape for exactly this
    reason. Accepting it here would let a card re-permit itself with one word
    and the loop would resume.
    """
    card_id = f"{7:08x}"
    store = _ready_card(tmp_path, card_id)
    for owner in ("seat-a", "seat-b", "seat-c"):
        _cycle(store, card_id, owner)
    store.append_event(card_id, "link", "seat-c", link_key="blocked_on", link_value="human")

    assert cb.read_signature(tmp_path, card_id).blocker is None
    with pytest.raises(cb.ClaimRefusedError):
        cb.assert_claim_permitted(tmp_path, card_id, "seat-d", ENFORCE)


def test_a_completed_card_is_never_churn(tmp_path) -> None:
    """However many claims it took, a card that finished is not stuck."""
    card_id = f"{8:08x}"
    store = _ready_card(tmp_path, card_id)
    for owner in ("seat-a", "seat-b", "seat-c", "seat-d"):
        _cycle(store, card_id, owner)
    revision = _claim(store, card_id, "seat-e")
    store.append_event(card_id, "move", "seat-e", column="done", claim_revision=revision)

    signature = cb.read_signature(tmp_path, card_id)

    assert store.fold(card_id).status.value == "done"
    assert signature.terminal is True
    assert cb.evaluate(signature, max_claim_attempts=5, max_owners=3).refuse is False


def test_a_voided_card_is_never_churn(tmp_path) -> None:
    card_id = f"{9:08x}"
    store = _ready_card(tmp_path, card_id)
    for owner in ("seat-a", "seat-b", "seat-c"):
        _cycle(store, card_id, owner)
    store.append_event(card_id, "void", "chef", reason="Superseded by successor")

    signature = cb.read_signature(tmp_path, card_id)

    assert signature.terminal is True
    assert cb.assert_claim_permitted(tmp_path, card_id, "seat-d", ENFORCE).refuse is False


def test_an_unknown_card_is_permitted(tmp_path) -> None:
    """A card with no events cannot be churning, and must not be denied work."""
    signature = cb.read_signature(tmp_path, f"{10:08x}")
    assert (signature.claim_attempts, signature.distinct_owners) == (0, 0)
    assert cb.assert_claim_permitted(tmp_path, f"{10:08x}", "seat-a", ENFORCE).refuse is False


# ----------------------------------------------------------------------
# Modes and thresholds.
# ----------------------------------------------------------------------


def test_off_is_the_default_and_returns_before_doing_any_work(tmp_path, monkeypatch) -> None:
    """off must not read the store at all, the way claim_expiry's off does."""

    def explode(*_args, **_kwargs):  # pragma: no cover - must never run
        raise AssertionError("the breaker read the store while switched off")

    monkeypatch.setattr(cb, "read_signature", explode)
    assert cb.mode_from_env({}) == "off"
    assert cb.mode_from_env({"SKFLEET_CHURN_BREAKER_MODE": "ENFORCE_TYPO"}) == "off"
    assert cb.check_claim(tmp_path, f"{11:08x}", "seat-a", {}) is None
    assert cb.assert_claim_permitted(tmp_path, f"{11:08x}", "seat-a", {}) is None


def test_report_mode_names_the_refusal_without_raising(tmp_path, caplog) -> None:
    card_id = f"{12:08x}"
    store = _ready_card(tmp_path, card_id)
    for owner in ("seat-a", "seat-b", "seat-c"):
        _cycle(store, card_id, owner)

    with caplog.at_level("WARNING"):
        verdict = cb.assert_claim_permitted(tmp_path, card_id, "seat-d", REPORT)

    assert verdict.refuse is True
    assert verdict.reason == "churn-owners"
    assert "churn breaker report" in caplog.text


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "", "five", "0", "1", "-3"])
def test_unusable_thresholds_fall_back_to_the_defaults(raw) -> None:
    """nan is the one that matters.

    It parses as a float and survives a ``< floor`` test, because every
    comparison against nan is False. ``attempts >= nan`` is then False for
    every card, which silently disarms the breaker while the mode still reads
    enforce. Values below the floor are refused too: a threshold of 1 would
    refuse a card on its first honest retry.
    """
    claims, owners = cb.thresholds_from_env(
        {"SKFLEET_CHURN_CLAIM_ATTEMPTS": raw, "SKFLEET_CHURN_OWNERS": raw}
    )
    assert (claims, owners) == (cb.DEFAULT_MAX_CLAIM_ATTEMPTS, cb.DEFAULT_MAX_OWNERS)


def test_configured_thresholds_are_honoured(tmp_path) -> None:
    card_id = f"{13:08x}"
    store = _ready_card(tmp_path, card_id)
    for owner in ("seat-a", "seat-b"):
        _cycle(store, card_id, owner)

    env = dict(ENFORCE, SKFLEET_CHURN_OWNERS="2", SKFLEET_CHURN_CLAIM_ATTEMPTS="2")
    assert cb.thresholds_from_env(env) == (2, 2)
    with pytest.raises(cb.ClaimRefusedError):
        cb.assert_claim_permitted(tmp_path, card_id, "seat-c", env)

    loose = dict(ENFORCE, SKFLEET_CHURN_OWNERS="9", SKFLEET_CHURN_CLAIM_ATTEMPTS="9")
    assert cb.assert_claim_permitted(tmp_path, card_id, "seat-c", loose).refuse is False


def test_a_malformed_event_line_cannot_stop_a_claim(tmp_path) -> None:
    """One bad line once stopped the rotation on five hosts for 40 minutes."""
    card_id = f"{14:08x}"
    store = _ready_card(tmp_path, card_id)
    _cycle(store, card_id, "seat-a")
    shard = next((tmp_path / "cards" / card_id / "events").glob("*.jsonl"))
    with shard.open("a", encoding="utf-8") as handle:
        handle.write('"Pushed branch to origin and opened PR #2"\n')
        handle.write("{not json at all\n")

    signature = cb.read_signature(tmp_path, card_id)

    assert signature.claim_attempts == 1


# ----------------------------------------------------------------------
# The chokepoint itself.
# ----------------------------------------------------------------------


def test_every_claim_path_in_this_repo_consults_the_breaker() -> None:
    """A breaker only one path respects is worthless.

    Board.claim_task lives in skcoord and has no gate of its own, so this repo
    covers each of its OWN call sites instead. This test fails the moment a
    seventh appears without the guard.
    """
    src = Path(__file__).parents[1] / "src" / "skcapstone"
    offenders = []
    for path in sorted(src.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if ".claim_task(" not in text:
            continue
        if "assert_claim_permitted" not in text:
            offenders.append(str(path.relative_to(src)))
    assert offenders == [], f"claim path with no churn breaker: {offenders}"


def test_the_cli_claim_path_refuses_a_churning_card(tmp_path, monkeypatch) -> None:
    """End to end through the command the fleet rotation actually shells out to.

    skfleet-rotate dispatches with ``skcapstone coord claim <id> --agent <name>``
    as a subprocess, so this is the path that produced the 47 re-claims on
    83e498b6. It must refuse before Board.claim_task is reached.
    """
    import click
    from click.testing import CliRunner

    from skcapstone.cli.coord import register_coord_commands

    card_id = f"{15:08x}"
    store = _ready_card(tmp_path, card_id)
    for owner in ("seat-a", "seat-b", "seat-c"):
        _cycle(store, card_id, owner)
    monkeypatch.setenv("SKFLEET_CHURN_BREAKER_MODE", "enforce")

    @click.group()
    def root() -> None:
        """Test harness root."""

    register_coord_commands(root)
    result = CliRunner().invoke(
        root, ["coord", "claim", card_id, "--home", str(tmp_path), "--agent", "seat-d"]
    )

    assert result.exit_code == 1
    assert "churn breaker" in result.output
    assert store.fold(card_id).owner is None
