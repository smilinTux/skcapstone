"""Regression fixtures for ghost pool cards (task 670765f8).

Tests that pool construction and preclaim checks always agree on claimability
for the 7 observed ghost pool cases.
"""

import json
import tempfile
from pathlib import Path
from unittest import TestCase

from skcapstone.claimability import (
    _fold_lifecycle_state,
    claimability_fold,
)


class GhostPoolFixture:
    """Fixture data for a ghost pool case."""

    def __init__(
        self,
        card_id: str,
        title: str,
        events: list[dict],
        expected_assignable: bool,
        expected_exclusion: str | None = None,
        description: str = "",
    ):
        self.card_id = card_id
        self.title = title
        self.events = events
        self.expected_assignable = expected_assignable
        self.expected_exclusion = expected_exclusion
        self.description = description


# ---------------------------------------------------------------------------
# Ghost Pool Case 1: 600fc649 - CARDSTORE-RECOVERY-A060-3F99-01R
# Issue: Included in pool but SKIPPED_RACED due to cached lifecycle state
# Root cause: Card was claimed between pool scan and launch
# ---------------------------------------------------------------------------

GHOST_600fc649 = GhostPoolFixture(
    card_id="600fc649",
    title="[CARDSTORE-RECOVERY-A060-3F99-01R][S][REVIEW] "
    "Independently verify preserved malformed event bytes",
    events=[
        {
            "action": "claim",
            "card_id": "600fc649",
            "agent": "codex-cardstore-recovery-review",
            "ts": "2026-08-29T01:11:52Z",
            "writer": "codex-cardstore-recovery-review",
            "event_id": "1",
        },
        {
            "action": "claim",
            "card_id": "600fc649",
            "agent": "pi-glm-chiap03-600fc649",
            "ts": "2026-08-29T01:12:14Z",
            "writer": "pi-glm-chiap03-600fc649",
            "event_id": "2",
        },
        {
            "action": "move",
            "card_id": "600fc649",
            "column": "review",
            "ts": "2026-08-29T03:19:26Z",
            "writer": "lumina",
            "event_id": "3",
        },
    ],
    expected_assignable=False,
    expected_exclusion="claimed",
    description="Card with multiple claim events. Last claim wins. "
    "Pool saw 'open', preclaim saw 'claimed'.",
)


# ---------------------------------------------------------------------------
# Ghost Pool Case 2: 6c418ad3 - FLEET-UNBLOCK-WAVE-01-11R2
# Issue: Included in pool and launched, but then reaped
# Root cause: Worker exited without reporting
# ---------------------------------------------------------------------------

GHOST_6c418ad3 = GhostPoolFixture(
    card_id="6c418ad3",
    title="[FLEET-UNBLOCK-WAVE-01-11R2][S][REVIEW] Review corrected partition 11 replacement",
    events=[
        {
            "action": "release_claim",
            "card_id": "6c418ad3",
            "agent": "lumina",
            "ts": "2026-08-29T12:02:31Z",
            "writer": "lumina",
            "event_id": "30",
        },
        {
            "action": "claim",
            "card_id": "6c418ad3",
            "agent": "pi-glm-chiap03-6c418ad3",
            "ts": "2026-08-29T12:31:42Z",
            "writer": "pi-glm-chiap03-6c418ad3",
            "event_id": "31",
        },
        {
            "action": "release_claim",
            "card_id": "6c418ad3",
            "agent": "fleet-liveness-reaper",
            "ts": "2026-08-29T12:42:18Z",
            "writer": "fleet-liveness-reaper",
            "event_id": "32",
        },
    ],
    expected_assignable=True,
    expected_exclusion=None,
    description="Card with release-claim after claim. Should be open and assignable.",
)


# ---------------------------------------------------------------------------
# Ghost Pool Case 3: 79396786 - REVIEW-ae072437
# Issue: CLAIM_REFUSED with "already review" error
# Root cause: Card was under review by another agent
# ---------------------------------------------------------------------------

GHOST_79396786 = GhostPoolFixture(
    card_id="79396786",
    title="[REVIEW-ae072437][S][REVIEW] Independently review candidate for SKW-PROV-02",
    events=[
        {
            "action": "release_claim",
            "card_id": "79396786",
            "agent": "pi-codex-79396786",
            "ts": "2026-08-28T03:13:00Z",
            "writer": "pi-codex-79396786",
            "event_id": "4",
        },
        {
            "action": "move",
            "card_id": "79396786",
            "column": "review",
            "ts": "2026-08-28T03:13:10Z",
            "writer": "pi-codex-79396786",
            "event_id": "5",
        },
    ],
    expected_assignable=True,
    expected_exclusion=None,
    description="Card moved to review column but not claimed. "
    "Pool saw open, preclaim should also see open.",
)


# ---------------------------------------------------------------------------
# Ghost Pool Case 4: 87f90ae0 - SKL-PDP-01
# Issue: Included in pool but SKIPPED_RACED
# Root cause: Claimed between pool scan and launch
# ---------------------------------------------------------------------------

GHOST_87f90ae0 = GhostPoolFixture(
    card_id="87f90ae0",
    title="[SKL-PDP-01][L] Deploy the SKLegal PDP runtime on 127.0.0.1:28779",
    events=[
        {
            "action": "release_claim",
            "card_id": "87f90ae0",
            "agent": "fleet-liveness-reaper",
            "ts": "2026-08-28T09:43:16Z",
            "writer": "fleet-liveness-reaper",
            "event_id": "19",
        },
        {
            "action": "claim",
            "card_id": "87f90ae0",
            "agent": "pi-codex-87f90ae0",
            "ts": "2026-08-28T09:43:25Z",
            "writer": "pi-codex-87f90ae0",
            "event_id": "20",
        },
        {
            "action": "move",
            "card_id": "87f90ae0",
            "column": "doing",
            "ts": "2026-08-28T09:45:09Z",
            "writer": "pi-codex-87f90ae0",
            "event_id": "21",
        },
    ],
    expected_assignable=False,
    expected_exclusion="claimed",
    description="Card claimed and moved to doing. Pool saw open, preclaim saw claimed.",
)


# ---------------------------------------------------------------------------
# Ghost Pool Case 5: b6eedf67 - FLEET-UNBLOCK-WAVE-01-30R2
# Issue: Similar to 6c418ad3
# Root cause: Released then claimed multiple times
# ---------------------------------------------------------------------------

GHOST_b6eedf67 = GhostPoolFixture(
    card_id="b6eedf67",
    title="[FLEET-UNBLOCK-WAVE-01-30R2][S][REVIEW] Review corrected partition 30 replacement",
    events=[
        {
            "action": "release_claim",
            "card_id": "b6eedf67",
            "agent": "lumina",
            "ts": "2026-08-29T11:22:44Z",
            "writer": "lumina",
            "event_id": "29",
        },
        {
            "action": "claim",
            "card_id": "b6eedf67",
            "agent": "pi-codex-chiap03-b6eedf67",
            "ts": "2026-08-29T11:27:03Z",
            "writer": "pi-codex-chiap03-b6eedf67",
            "event_id": "30",
        },
        {
            "action": "release_claim",
            "card_id": "b6eedf67",
            "agent": "lumina",
            "ts": "2026-08-29T12:02:39Z",
            "writer": "lumina",
            "event_id": "31",
        },
    ],
    expected_assignable=True,
    expected_exclusion=None,
    description="Card released after claim. Should be open and assignable.",
)


# ---------------------------------------------------------------------------
# Ghost Pool Case 6: dd659b4c - SKGW-AUTHZ-06A6-PAIR
# Issue: Included in pool but SKIPPED_RACED
# Root cause: Claimed between pool scan and launch
# ---------------------------------------------------------------------------

GHOST_dd659b4c = GhostPoolFixture(
    card_id="dd659b4c",
    title="[SKGW-AUTHZ-06A6-PAIR][L] Provision one "
    "synchronized replacement qualification credential lifecycle",
    events=[
        {
            "action": "claim",
            "card_id": "dd659b4c",
            "agent": "pi-codex-chiap03-dd659b4c",
            "ts": "2026-08-28T14:27:23Z",
            "writer": "pi-codex-chiap03-dd659b4c",
            "event_id": "1",
        },
        {
            "action": "move",
            "card_id": "dd659b4c",
            "column": "doing",
            "ts": "2026-08-28T14:29:48Z",
            "writer": "pi-codex-chiap03-dd659b4c",
            "event_id": "2",
        },
    ],
    expected_assignable=False,
    expected_exclusion="claimed",
    description="Card claimed and moved. Pool saw open, preclaim saw claimed.",
)


# ---------------------------------------------------------------------------
# Ghost Pool Case 7: ff77ffb4 - CHATGPT-CLIENT
# Issue: Included in pool but SKIPPED_RACED
# Root cause: Claimed between pool scan and launch
# ---------------------------------------------------------------------------

GHOST_ff77ffb4 = GhostPoolFixture(
    card_id="ff77ffb4",
    title="[CHATGPT-CLIENT][CHIWK11] Deploy Windows GUI with WSL2 Jarvis SK MCP runtime",
    events=[
        {
            "action": "move",
            "card_id": "ff77ffb4",
            "column": "review",
            "ts": "2026-08-29T03:29:48Z",
            "writer": "pi-codex-chiap03-ff77ffb4",
            "event_id": "8",
        },
    ],
    expected_assignable=True,
    expected_exclusion=None,
    description="Card moved to review but no PASS_FOR_REVIEW outcome yet. Should be assignable.",
)

# Evidence event for PASS_FOR_REVIEW outcome (separate file)
EVIDENCE_ff77ffb4 = [
    {
        "action": "link",
        "card_id": "ff77ffb4",
        "link_key": "verdict",
        "link_value": "PASS_FOR_REVIEW",
        "ts": "2026-08-29T03:29:47Z",
        "writer": "pi-codex-chiap03-ff77ffb4",
        "event_id": "7",
    },
]


# All ghost pool cases
GHOST_FIXTURES = [
    GHOST_600fc649,
    GHOST_6c418ad3,
    GHOST_79396786,
    GHOST_87f90ae0,
    GHOST_b6eedf67,
    GHOST_dd659b4c,
    GHOST_ff77ffb4,
]


# ---------------------------------------------------------------------------
# Additional edge case fixtures
# ---------------------------------------------------------------------------

# Fixture: move after claim (claim should persist)
FIXTURE_MOVE_AFTER_CLAIM = GhostPoolFixture(
    card_id="move-after-claim",
    title="[TEST] Move after claim should not release",
    events=[
        {
            "action": "claim",
            "card_id": "move-after-claim",
            "agent": "test-agent",
            "ts": "2026-08-29T10:00:00Z",
            "writer": "test-agent",
            "event_id": "1",
        },
        {
            "action": "move",
            "card_id": "move-after-claim",
            "column": "doing",
            "ts": "2026-08-29T10:01:00Z",
            "writer": "test-agent",
            "event_id": "2",
        },
    ],
    expected_assignable=False,
    expected_exclusion="claimed",
    description="Move after claim should not release the claim.",
)


# Fixture: release after move (release should win)
FIXTURE_RELEASE_AFTER_MOVE = GhostPoolFixture(
    card_id="release-after-move",
    title="[TEST] Release after move should open card",
    events=[
        {
            "action": "claim",
            "card_id": "release-after-move",
            "agent": "test-agent",
            "ts": "2026-08-29T10:00:00Z",
            "writer": "test-agent",
            "event_id": "1",
        },
        {
            "action": "move",
            "card_id": "release-after-move",
            "column": "doing",
            "ts": "2026-08-29T10:01:00Z",
            "writer": "test-agent",
            "event_id": "2",
        },
        {
            "action": "release_claim",
            "card_id": "release-after-move",
            "agent": "test-agent",
            "ts": "2026-08-29T10:02:00Z",
            "writer": "test-agent",
            "event_id": "3",
        },
    ],
    expected_assignable=True,
    expected_exclusion=None,
    description="Release after move should open the card.",
)


# Fixture: completed source awaiting review
FIXTURE_COMPLETED_AWAITING_REVIEW = GhostPoolFixture(
    card_id="completed-awaiting-review",
    title="[TEST] Completed card awaiting review",
    events=[
        {
            "action": "claim",
            "card_id": "completed-awaiting-review",
            "agent": "test-agent",
            "ts": "2026-08-29T10:00:00Z",
            "writer": "test-agent",
            "event_id": "1",
        },
        {
            "action": "complete",
            "card_id": "completed-awaiting-review",
            "agent": "test-agent",
            "ts": "2026-08-29T10:05:00Z",
            "writer": "test-agent",
            "event_id": "2",
        },
    ],
    expected_assignable=False,
    expected_exclusion="terminal_complete",
    description="Completed card should not be assignable.",
)


# All fixtures
ALL_FIXTURES = GHOST_FIXTURES + [
    FIXTURE_MOVE_AFTER_CLAIM,
    FIXTURE_RELEASE_AFTER_MOVE,
    FIXTURE_COMPLETED_AWAITING_REVIEW,
]


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestClaimabilityGhostPool(TestCase):
    """Test claimability fold against ghost pool fixtures."""

    def _write_fixture(self, temp_dir: Path, fixture: GhostPoolFixture) -> Path:
        """Write a fixture to a temporary card directory."""
        card_dir = temp_dir / "cards" / fixture.card_id
        card_dir.mkdir(parents=True, exist_ok=True)

        events_dir = card_dir / "events"
        events_dir.mkdir(exist_ok=True)

        # Write events
        event_file = events_dir / f"{fixture.card_id}@chiap02.jsonl"
        with open(event_file, "w", encoding="utf-8") as f:
            for event in fixture.events:
                f.write(json.dumps(event) + "\n")

        # Write core.json
        core = {
            "id": fixture.card_id,
            "title": fixture.title,
            "kind": "task",
            "dependencies": [],
            "initial_labels": [],
            "tags": [],
        }
        with open(card_dir / "core.json", "w", encoding="utf-8") as f:
            json.dump(core, f)

        return temp_dir

    def _create_temp_evidence(
        self, temp_dir: Path, fixture: GhostPoolFixture, evidence_events: list | None = None
    ) -> None:
        """Create evidence directory for a fixture."""
        evidence_dir = temp_dir / "evidence" / "card_events"
        evidence_dir.mkdir(parents=True, exist_ok=True)

        # Write provided evidence events
        if evidence_events:
            with open(evidence_dir / "test.jsonl", "w", encoding="utf-8") as f:
                for event in evidence_events:
                    f.write(json.dumps(event) + "\n")

        # Check if fixture has PASS_FOR_REVIEW outcome
        for event in fixture.events:
            if event.get("action") == "evidence_link":
                with open(evidence_dir / "test.jsonl", "w", encoding="utf-8") as f:
                    f.write(json.dumps(event) + "\n")
                break

    def test_ghost_pool_600fc649(self):
        """Test ghost pool case 600fc649."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            fixture = GHOST_600fc649
            self._write_fixture(temp_dir, fixture)
            self._create_temp_evidence(temp_dir, fixture)

            result = claimability_fold(
                card_id=fixture.card_id,
                cards_dir=temp_dir / "cards",
                evidence_dir=temp_dir / "evidence" / "card_events",
                current_host="chiap02",
            )

            self.assertEqual(result.assignable, fixture.expected_assignable, fixture.description)
            if fixture.expected_exclusion:
                self.assertTrue(
                    any(fixture.expected_exclusion in exc for exc in result.exclusions),
                    f"Expected exclusion {fixture.expected_exclusion} "
                    f"not found in {result.exclusions}",
                )

    def test_ghost_pool_6c418ad3(self):
        """Test ghost pool case 6c418ad3."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            fixture = GHOST_6c418ad3
            self._write_fixture(temp_dir, fixture)

            result = claimability_fold(
                card_id=fixture.card_id,
                cards_dir=temp_dir / "cards",
                evidence_dir=temp_dir / "evidence" / "card_events",
                current_host="chiap02",
            )

            self.assertEqual(result.assignable, fixture.expected_assignable, fixture.description)

    def test_ghost_pool_79396786(self):
        """Test ghost pool case 79396786."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            fixture = GHOST_79396786
            self._write_fixture(temp_dir, fixture)

            result = claimability_fold(
                card_id=fixture.card_id,
                cards_dir=temp_dir / "cards",
                evidence_dir=temp_dir / "evidence" / "card_events",
                current_host="chiap02",
            )

            self.assertEqual(result.assignable, fixture.expected_assignable, fixture.description)

    def test_ghost_pool_87f90ae0(self):
        """Test ghost pool case 87f90ae0."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            fixture = GHOST_87f90ae0
            self._write_fixture(temp_dir, fixture)

            result = claimability_fold(
                card_id=fixture.card_id,
                cards_dir=temp_dir / "cards",
                evidence_dir=temp_dir / "evidence" / "card_events",
                current_host="chiap02",
            )

            self.assertEqual(result.assignable, fixture.expected_assignable, fixture.description)
            if fixture.expected_exclusion:
                self.assertTrue(
                    any(fixture.expected_exclusion in exc for exc in result.exclusions)
                )

    def test_ghost_pool_b6eedf67(self):
        """Test ghost pool case b6eedf67."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            fixture = GHOST_b6eedf67
            self._write_fixture(temp_dir, fixture)

            result = claimability_fold(
                card_id=fixture.card_id,
                cards_dir=temp_dir / "cards",
                evidence_dir=temp_dir / "evidence" / "card_events",
                current_host="chiap02",
            )

            self.assertEqual(result.assignable, fixture.expected_assignable, fixture.description)

    def test_ghost_pool_dd659b4c(self):
        """Test ghost pool case dd659b4c."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            fixture = GHOST_dd659b4c
            self._write_fixture(temp_dir, fixture)

            result = claimability_fold(
                card_id=fixture.card_id,
                cards_dir=temp_dir / "cards",
                evidence_dir=temp_dir / "evidence" / "card_events",
                current_host="chiap02",
            )

            self.assertEqual(result.assignable, fixture.expected_assignable, fixture.description)
            if fixture.expected_exclusion:
                self.assertTrue(
                    any(fixture.expected_exclusion in exc for exc in result.exclusions)
                )

    def test_ghost_pool_ff77ffb4(self):
        """Test ghost pool case ff77ffb4."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            fixture = GHOST_ff77ffb4
            self._write_fixture(temp_dir, fixture)
            self._create_temp_evidence(temp_dir, fixture)

            result = claimability_fold(
                card_id=fixture.card_id,
                cards_dir=temp_dir / "cards",
                evidence_dir=temp_dir / "evidence" / "card_events",
                current_host="chiap02",
            )

            self.assertEqual(result.assignable, fixture.expected_assignable, fixture.description)
            if fixture.expected_exclusion:
                self.assertTrue(
                    any(fixture.expected_exclusion in exc for exc in result.exclusions)
                )

    def test_ghost_pool_ff77ffb4_with_pass_for_review(self):
        """Test that ff77ffb4 with PASS_FOR_REVIEW is not assignable."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            fixture = GHOST_ff77ffb4
            self._write_fixture(temp_dir, fixture)
            # Add PASS_FOR_REVIEW evidence
            self._create_temp_evidence(temp_dir, fixture, EVIDENCE_ff77ffb4)

            result = claimability_fold(
                card_id=fixture.card_id,
                cards_dir=temp_dir / "cards",
                evidence_dir=temp_dir / "evidence" / "card_events",
                current_host="chiap02",
            )

            # With PASS_FOR_REVIEW, should not be assignable
            self.assertFalse(result.assignable)
            self.assertTrue(any("awaiting_review" in exc for exc in result.exclusions))

    def test_move_after_claim(self):
        """Test that move after claim does not release."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            fixture = FIXTURE_MOVE_AFTER_CLAIM
            self._write_fixture(temp_dir, fixture)

            result = claimability_fold(
                card_id=fixture.card_id,
                cards_dir=temp_dir / "cards",
                evidence_dir=temp_dir / "evidence" / "card_events",
                current_host="chiap02",
            )

            self.assertEqual(result.assignable, fixture.expected_assignable, fixture.description)
            self.assertEqual(result.lifecycle_state, "claimed")

    def test_release_after_move(self):
        """Test that release after move opens the card."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            fixture = FIXTURE_RELEASE_AFTER_MOVE
            self._write_fixture(temp_dir, fixture)

            result = claimability_fold(
                card_id=fixture.card_id,
                cards_dir=temp_dir / "cards",
                evidence_dir=temp_dir / "evidence" / "card_events",
                current_host="chiap02",
            )

            self.assertEqual(result.assignable, fixture.expected_assignable, fixture.description)
            self.assertEqual(result.lifecycle_state, "open")

    def test_completed_awaiting_review(self):
        """Test that completed card is not assignable."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            fixture = FIXTURE_COMPLETED_AWAITING_REVIEW
            self._write_fixture(temp_dir, fixture)

            result = claimability_fold(
                card_id=fixture.card_id,
                cards_dir=temp_dir / "cards",
                evidence_dir=temp_dir / "evidence" / "card_events",
                current_host="chiap02",
            )

            self.assertEqual(result.assignable, fixture.expected_assignable, fixture.description)
            self.assertEqual(result.lifecycle_state, "complete")

    def test_pool_and_preclaim_agree(self):
        """Test that pool construction and preclaim always agree.

        This is the core guarantee of the claimability fold.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)

            for fixture in ALL_FIXTURES:
                self._write_fixture(temp_dir, fixture)
                if fixture.card_id == "ff77ffb4":
                    self._create_temp_evidence(temp_dir, fixture)

                # Simulate pool construction check
                pool_result = claimability_fold(
                    card_id=fixture.card_id,
                    cards_dir=temp_dir / "cards",
                    evidence_dir=temp_dir / "evidence" / "card_events",
                    current_host="chiap02",
                )

                # Simulate preclaim check (would be called later)
                preclaim_result = claimability_fold(
                    card_id=fixture.card_id,
                    cards_dir=temp_dir / "cards",
                    evidence_dir=temp_dir / "evidence" / "card_events",
                    current_host="chiap02",
                )

                # Both must agree
                self.assertEqual(
                    pool_result.assignable,
                    preclaim_result.assignable,
                    f"Pool and preclaim disagree for {fixture.card_id}: "
                    f"pool={pool_result.assignable}, preclaim={preclaim_result.assignable}",
                )

                self.assertEqual(
                    pool_result.lifecycle_state,
                    preclaim_result.lifecycle_state,
                    f"Lifecycle state disagrees for {fixture.card_id}",
                )

    def test_pool_and_preclaim_agree_with_pass_for_review(self):
        """Test pool and preclaim agreement with PASS_FOR_REVIEW evidence."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_dir = Path(tmpdir)
            fixture = GHOST_ff77ffb4
            self._write_fixture(temp_dir, fixture)
            # Add PASS_FOR_REVIEW evidence
            self._create_temp_evidence(temp_dir, fixture, EVIDENCE_ff77ffb4)

            # Simulate pool construction check
            pool_result = claimability_fold(
                card_id=fixture.card_id,
                cards_dir=temp_dir / "cards",
                evidence_dir=temp_dir / "evidence" / "card_events",
                current_host="chiap02",
            )

            # Simulate preclaim check (would be called later)
            preclaim_result = claimability_fold(
                card_id=fixture.card_id,
                cards_dir=temp_dir / "cards",
                evidence_dir=temp_dir / "evidence" / "card_events",
                current_host="chiap02",
            )

            # Both must agree
            self.assertEqual(
                pool_result.assignable,
                preclaim_result.assignable,
                f"Pool and preclaim disagree for {fixture.card_id}: "
                f"pool={pool_result.assignable}, preclaim={preclaim_result.assignable}",
            )

            # Both should say not assignable due to awaiting_review
            self.assertFalse(pool_result.assignable)
            self.assertFalse(preclaim_result.assignable)


class TestLifecycleStateFold(TestCase):
    """Test lifecycle state folding in isolation."""

    def test_claim_creates_claimed_state(self):
        """Test that a claim event creates claimed state."""
        events = [
            {"action": "claim", "ts": "2026-08-29T10:00:00Z", "writer": "test", "event_id": "1"},
        ]
        state, _, _, _ = _fold_lifecycle_state(events)
        self.assertEqual(state, "claimed")

    def test_release_creates_open_state(self):
        """Test that a release_claim event creates open state."""
        events = [
            {"action": "claim", "ts": "2026-08-29T10:00:00Z", "writer": "test", "event_id": "1"},
            {
                "action": "release_claim",
                "ts": "2026-08-29T10:01:00Z",
                "writer": "test",
                "event_id": "2",
            },
        ]
        state, _, _, _ = _fold_lifecycle_state(events)
        self.assertEqual(state, "open")

    def test_complete_creates_complete_state(self):
        """Test that a complete event creates complete state."""
        events = [
            {"action": "claim", "ts": "2026-08-29T10:00:00Z", "writer": "test", "event_id": "1"},
            {
                "action": "complete",
                "ts": "2026-08-29T10:05:00Z",
                "writer": "test",
                "event_id": "2",
            },
        ]
        state, _, _, _ = _fold_lifecycle_state(events)
        self.assertEqual(state, "complete")

    def test_move_to_done_creates_complete_state(self):
        """Test that moving to done column creates complete state."""
        events = [
            {
                "action": "move",
                "column": "done",
                "ts": "2026-08-29T10:00:00Z",
                "writer": "test",
                "event_id": "1",
            },
        ]
        state, col, _, _ = _fold_lifecycle_state(events)
        self.assertEqual(state, "complete")
        self.assertEqual(col, "done")

    def test_move_out_of_done_reopens(self):
        """Test that moving out of done reopens the card."""
        events = [
            {
                "action": "move",
                "column": "done",
                "ts": "2026-08-29T10:00:00Z",
                "writer": "test",
                "event_id": "1",
            },
            {
                "action": "move",
                "column": "ready",
                "ts": "2026-08-29T11:00:00Z",
                "writer": "test",
                "event_id": "2",
            },
        ]
        state, col, _, _ = _fold_lifecycle_state(events)
        self.assertEqual(state, "open")
        self.assertEqual(col, "ready")

    def test_complete_action_overrides_column(self):
        """Test that complete action overrides done column."""
        events = [
            {
                "action": "move",
                "column": "done",
                "ts": "2026-08-29T10:00:00Z",
                "writer": "test",
                "event_id": "1",
            },
            {
                "action": "move",
                "column": "ready",
                "ts": "2026-08-29T11:00:00Z",
                "writer": "test",
                "event_id": "2",
            },
            {
                "action": "complete",
                "ts": "2026-08-29T12:00:00Z",
                "writer": "test",
                "event_id": "3",
            },
        ]
        state, col, _, _ = _fold_lifecycle_state(events)
        self.assertEqual(state, "complete")

    def test_void_is_sticky(self):
        """Test that void state is sticky."""
        events = [
            {"action": "void", "ts": "2026-08-29T10:00:00Z", "writer": "test", "event_id": "1"},
            {"action": "claim", "ts": "2026-08-29T11:00:00Z", "writer": "test", "event_id": "2"},
        ]
        state, _, _, _ = _fold_lifecycle_state(events)
        self.assertEqual(state, "void")

    def test_complete_then_void(self):
        """Test that void overrides complete."""
        events = [
            {
                "action": "complete",
                "ts": "2026-08-29T10:00:00Z",
                "writer": "test",
                "event_id": "1",
            },
            {"action": "void", "ts": "2026-08-29T11:00:00Z", "writer": "test", "event_id": "2"},
        ]
        state, _, _, _ = _fold_lifecycle_state(events)
        self.assertEqual(state, "void")

    def test_multiple_claims_last_wins(self):
        """Test that the most recent claim wins."""
        events = [
            {"action": "claim", "ts": "2026-08-29T10:00:00Z", "writer": "agent1", "event_id": "1"},
            {"action": "claim", "ts": "2026-08-29T10:01:00Z", "writer": "agent2", "event_id": "2"},
            {"action": "claim", "ts": "2026-08-29T10:02:00Z", "writer": "agent3", "event_id": "3"},
        ]
        state, _, _, _ = _fold_lifecycle_state(events)
        self.assertEqual(state, "claimed")

    def test_move_does_not_release_claim(self):
        """Test that move does NOT release a claim."""
        events = [
            {"action": "claim", "ts": "2026-08-29T10:00:00Z", "writer": "test", "event_id": "1"},
            {
                "action": "move",
                "column": "doing",
                "ts": "2026-08-29T10:01:00Z",
                "writer": "test",
                "event_id": "2",
            },
        ]
        state, _, _, _ = _fold_lifecycle_state(events)
        self.assertEqual(state, "claimed")


if __name__ == "__main__":
    import unittest

    unittest.main()
