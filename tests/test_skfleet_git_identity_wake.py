"""Known operational capability wakes once after authored correction."""

from pathlib import Path

import pytest

from tests.test_skfleet_backoff_wake import BackoffHarness, _native_namespace


def test_native_git_identity_waits_then_retries_once(tmp_path: Path) -> None:
    """Use the producer's typed verdict and consume the exact wake generation."""
    card = "888b4895"
    verdict = "2026-10-03T14:08:19.260373+00:00"
    events = {
        card: [
            {
                "ts": verdict,
                "action": "verdict",
                "verdict": "BLOCKED",
                "blocked_on": "capability",
                "referent": "git-author-identity",
            }
        ]
    }
    ns = _native_namespace(tmp_path, events)
    assert ns["blocked_backoff"](card) is True
    events[card].append({"ts": "2026-10-03T14:26:23.700080+00:00", "action": "describe"})
    assert ns["blocked_backoff"](card) is False
    ns["_wake_launch_times"][card].append(ns["_ts_epoch"]("2026-10-03T14:30:00Z"))
    assert ns["blocked_backoff"](card) is True
    events[card].append({"ts": "2026-10-03T14:31:00Z", "action": "describe"})
    assert ns["blocked_backoff"](card) is False


@pytest.mark.parametrize(
    "category,referent",
    [
        ("capability", "unknown-capability"),
        ("capability", "git-author-identity referent=ac:1"),
        ("human", "git-author-identity"),
        ("dependency", "card:bbbbbbbb"),
    ],
)
def test_other_blockers_do_not_gain_authored_wake(
    tmp_path: Path, category: str, referent: str
) -> None:
    """A named Git capability does not broaden unrelated blockers."""
    board = BackoffHarness(tmp_path)
    card = "888b4895"
    board.outcome(
        card, "2026-10-03T14:08:19Z", f"BLOCKED blocked_on={category} referent={referent}"
    )
    board.event(card, "2026-10-03T14:26:23Z", "describe")
    assert board.blocked(card) is True


@pytest.mark.parametrize("action", ["describe", "add_label"])
def test_old_or_bookkeeping_changes_do_not_wake(tmp_path: Path, action: str) -> None:
    """Require a new material authored generation, not bookkeeping."""
    board = BackoffHarness(tmp_path)
    card = "888b4895"
    board.outcome(
        card, "2026-10-03T14:08:19Z", "BLOCKED blocked_on=capability referent=git-author-identity"
    )
    if action == "describe":
        board.event(card, "2026-10-03T14:08:19Z", action)
    else:
        board.evidence_event(card, "2026-10-03T14:26:23Z", action, label="triaged")
    assert board.blocked(card) is True


def test_producer_emits_the_supported_exact_named_referent() -> None:
    """Keep the typed producer contract visible to the consumer regression."""
    source = Path(__file__).resolve().parents[1] / "src/skcapstone/fleet/production_brief.py"
    assert "blocked_on=capability referent=git-author-identity" in source.read_text()
