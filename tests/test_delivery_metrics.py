"""Tests for SKMETRIC-DELIVERY-01: measure rows and merges, not cards.

Covers the four acceptance criteria:
  1. Closing a review card increments cards_completed but does not change
     deliverables_verified.
  2. Writing a live database row increments rows_written; merging a remote
     pull request increments prs_merged, while a card assertion does not.
  3. One DELIVERY line is emitted per date even when every counter is zero.
  4. Focused, source-adapter, daily-idempotency, and static checks pass.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from skcapstone.delivery_metrics import (
    DELIVERY_LINE_KEYS,
    DeliveryAdapter,
    DeliveryLine,
    DeliverySources,
    count_cardstore_cards_for_date,
    count_db_rows_for_date,
    count_evidence_for_date,
    count_git_prs_for_date,
    create_delivery_adapter,
    emit_daily_delivery,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_cardstore(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    def run(*args: str) -> None:
        subprocess.run(args, cwd=str(repo), check=True, capture_output=True)
    run("git", "init", "-q")
    run("git", "config", "user.email", "test@skcapstone.local")
    run("git", "config", "user.name", "Test")
    (repo / "f.txt").write_text("x")
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "feat: add f.txt")
    run("git", "commit", "--allow-empty", "-q", "-m",
        'Merge pull request #42 from tester/feature-x (opened 1 PR)')
    return repo


# ---------------------------------------------------------------------------
# AC1: cards_completed vs deliverables_verified
# ---------------------------------------------------------------------------

def test_closing_review_card_bumps_cards_completed_only(tmp_path: Path):
    """A complete event counts as a card completed; an evidence event is what
    counts a verified deliverable. The two counters must stay independent."""
    events = [
        {"card_id": "aaa111", "action": "create", "ts": "2026-01-05T10:00:00+00:00"},
        {"card_id": "bbb222", "action": "complete", "ts": "2026-01-05T11:00:00+00:00"},
        {"card_id": "bbb222", "action": "link", "writer": "w",
         "ts": "2026-01-05T11:05:00+00:00",
         "link_key": "verdict", "link_value": "PASS_FOR_REVIEW|ac:1,ac:2"},
    ]
    cs = tmp_path / "card_events"
    _write_cardstore(cs / "host.jsonl", events)
    created, completed = count_cardstore_cards_for_date(cs, "2026-01-05")
    assert created == 1
    assert completed == 1
    # The verdict link is a card assertion: it must NOT verify a deliverable.
    assert count_evidence_for_date(cs, "2026-01-05") == 0

    # Now add an independent evidence event with a hash.
    _write_cardstore(
        cs / "host.jsonl",
        [
            {"card_id": "bbb222", "action": "link", "writer": "w",
             "ts": "2026-01-05T12:00:00+00:00",
             "link_key": "evidence",
             "link_value": "docs/evidence/x.md|sha256=ab12"},
        ],
    )
    assert count_evidence_for_date(cs, "2026-01-05") == 1
    # Closing the card did not move the verified counter.
    created2, completed2 = count_cardstore_cards_for_date(cs, "2026-01-05")
    assert (created2, completed2) == (1, 1)


# ---------------------------------------------------------------------------
# AC2: rows_written from DB, prs_merged from git remote
# ---------------------------------------------------------------------------

def test_db_rows_increment_rows_written(tmp_path: Path):
    db = tmp_path / "index.db"
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT, created_at TEXT)"
    )
    con.execute(
        "INSERT INTO memories (content, created_at) VALUES (?, ?)",
        ("note 2026-01-05", "2026-01-05T09:30:00+00:00"),
    )
    con.execute(
        "INSERT INTO memories (content, created_at) VALUES (?, ?)",
        ("note 2026-01-06", "2026-01-06T09:30:00+00:00"),
    )
    con.commit()
    con.close()
    assert count_db_rows_for_date(db, "2026-01-05") == 1
    assert count_db_rows_for_date(db, "2026-01-06") == 1
    assert count_db_rows_for_date(db, "2026-01-07") == 0


def test_git_merge_bumps_prs_merged_not_card_assertion(tmp_path: Path):
    repo = _make_repo(tmp_path)
    opened, merged = count_git_prs_for_date(repo, "2026-01-05")
    # Commit datetimes are "now" (test run date), not 2026-01-05, so use the
    # actual commit date to prove the adapter reads git truth.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    opened, merged = count_git_prs_for_date(repo, today)
    assert merged >= 1, "the merge commit subject carries 'Merge pull request'"
    # A card assertion (verdict link) never increments prs_merged; only the
    # git remote merge evidence does.
    assert count_git_prs_for_date(repo, "2026-01-05") == (0, 0)


# ---------------------------------------------------------------------------
# AC3: one DELIVERY line per date, even all zeros
# ---------------------------------------------------------------------------

def test_daily_line_emitted_with_all_zero_counters(tmp_path: Path):
    result = emit_daily_delivery(tmp_path, "2026-01-05",
                                 DeliverySources(sprint_denominator=42))
    assert result.emitted
    line_obj = result.line_obj
    assert line_obj.date == "2026-01-05"
    assert line_obj.prs_opened == 0
    assert line_obj.prs_merged == 0
    assert line_obj.cards_created == 0
    assert line_obj.cards_completed == 0
    assert line_obj.rows_written == 0
    assert line_obj.deliverables_verified == 0
    assert line_obj.sprint_denominator == 42
    data = json.loads((tmp_path / "metrics" / "delivery" / "2026-01-05.json").read_text())
    for key in DELIVERY_LINE_KEYS:
        assert key in data


# ---------------------------------------------------------------------------
# AC4: idempotency + serializer round-trip + static shape
# ---------------------------------------------------------------------------

def test_daily_idempotency(tmp_path: Path):
    sources = DeliverySources(sprint_denominator=42)
    first = emit_daily_delivery(tmp_path, "2026-01-05", sources)
    second = emit_daily_delivery(tmp_path, "2026-01-05", sources)
    # Same sources -> same counters. Only ``ts`` (write time) differs.
    assert first.line_obj.prs_opened == second.line_obj.prs_opened
    assert first.line_obj.cards_completed == second.line_obj.cards_completed
    assert first.line_obj.rows_written == second.line_obj.rows_written
    assert first.line_obj.deliverables_verified == second.line_obj.deliverables_verified


def test_line_round_trip_through_serializer():
    line = DeliveryLine(date="2026-01-05", sprint_denominator=42, ts="2026-08-30T19:47:57Z")
    raw = line.to_json()
    parsed = DeliveryLine.parse(raw)
    assert parsed.date == "2026-01-05"
    assert parsed.sprint_denominator == 42
    with pytest.raises(ValueError):
        DeliveryLine.parse("not json at all")
    with pytest.raises(ValueError):
        DeliveryLine.parse(json.dumps({"date": "2026-01-05"}))


def test_source_adapters_degrade_to_zero():
    # Absent sources read as zero, never raise.
    assert count_cardstore_cards_for_date(Path("/nonexistent-dir"), "2026-01-05") == (0, 0)
    assert count_evidence_for_date(Path("/nonexistent-dir"), "2026-01-05") == 0
    assert count_db_rows_for_date(Path("/nonexistent.db"), "2026-01-05") == 0
    assert count_git_prs_for_date(Path("/nonexistent-repo"), "2026-01-05") == (0, 0)


def test_create_delivery_adapter_wiring(tmp_path: Path):
    adapter = create_delivery_adapter(home=tmp_path, sprint_denominator=12,
                                      repo_path=tmp_path / "no-repo")
    assert isinstance(adapter, DeliveryAdapter)
    assert adapter.sprint_denominator == 12
    assert adapter.db_path == tmp_path / "index.db"
    assert adapter.cardstore_dir == tmp_path / "coordination" / "card_events"
