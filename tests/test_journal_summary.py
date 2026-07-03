"""Tests for the weekly journal summary generator and its CLI.

Test coverage:
- gather_recent_entries() keeps only entries inside the N-day window
- summarize_week() calls the (mocked) LLMBridge with the windowed entries
- summarize_week() over an empty window returns a graceful placeholder and
  never touches the LLM
- Out-of-window entries are excluded from the prompt
- CLI: `skcapstone journal summary --week` renders the summary (mocked LLM)
- CLI: `--json-out` emits structured JSON

No test in this module makes a live LLM/network call — the bridge is always
mocked or the empty-window fast path is exercised.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


NOW = datetime(2026, 7, 3, 12, 0, 0, tzinfo=timezone.utc)


def _entry_markdown(title: str, when: datetime, moment: str, feeling: str = "") -> str:
    """Render a journal entry block the way JournalEntry.to_markdown() does."""
    lines = [
        f"## {title}",
        "",
        f"**Date:** {when.isoformat()}",
        "**Intensity:** 5.0/10 [+++++]",
        "",
        "### Key Moments",
        f"- {moment}",
    ]
    if feeling:
        lines += ["", "### How It Felt", feeling]
    lines += ["", "---", ""]
    return "\n".join(lines)


def _journal_text(*entries: str) -> str:
    """Assemble a full journal file (header + entry blocks)."""
    header = (
        "# SKMemory Journal\n\n"
        "> *Append-only session log.*\n\n"
        "---\n\n"
    )
    return header + "\n".join(entries)


def _fake_journal(text: str):
    """A stand-in Journal exposing just read_all()."""
    j = MagicMock()
    j.read_all.return_value = text
    return j


def _make_bridge(response: str = "A productive week of building and shipping."):
    bridge = MagicMock()
    bridge.generate.return_value = response
    return bridge


# ---------------------------------------------------------------------------
# gather_recent_entries
# ---------------------------------------------------------------------------


class TestGatherRecentEntries:
    def test_windows_out_old_entries(self):
        from skcapstone.journal_summary import gather_recent_entries

        text = _journal_text(
            _entry_markdown("Old", NOW - timedelta(days=30), "long ago"),
            _entry_markdown("Recent A", NOW - timedelta(days=2), "yesterday-ish"),
            _entry_markdown("Recent B", NOW - timedelta(days=6), "this week"),
        )
        entries = gather_recent_entries(text, days=7, now=NOW)

        titles = [block.splitlines()[0] for _ts, block in entries]
        assert titles == ["## Recent B", "## Recent A"]  # sorted oldest→newest

    def test_boundary_entry_at_edge_included(self):
        from skcapstone.journal_summary import gather_recent_entries

        text = _journal_text(
            _entry_markdown("Edge", NOW - timedelta(days=7) + timedelta(minutes=1), "edge"),
        )
        entries = gather_recent_entries(text, days=7, now=NOW)
        assert len(entries) == 1

    def test_empty_journal_returns_nothing(self):
        from skcapstone.journal_summary import gather_recent_entries

        assert gather_recent_entries("", days=7, now=NOW) == []
        assert gather_recent_entries(_journal_text(), days=7, now=NOW) == []

    def test_unparseable_date_is_skipped(self):
        from skcapstone.journal_summary import gather_recent_entries

        bad = "## Broken\n\n**Date:** not-a-date\n\n---\n"
        text = _journal_text(
            bad,
            _entry_markdown("Good", NOW - timedelta(days=1), "fine"),
        )
        entries = gather_recent_entries(text, days=7, now=NOW)
        assert len(entries) == 1
        assert entries[0][1].startswith("## Good")


# ---------------------------------------------------------------------------
# summarize_week
# ---------------------------------------------------------------------------


class TestSummarizeWeek:
    def test_happy_path_calls_llm_with_windowed_entries(self):
        from skcapstone.journal_summary import summarize_week

        journal = _fake_journal(
            _journal_text(
                _entry_markdown("Way old", NOW - timedelta(days=40), "ancient"),
                _entry_markdown("Deploy day", NOW - timedelta(days=3), "shipped skcapstone"),
                _entry_markdown("Cloud 9", NOW - timedelta(days=1), "breakthrough", "warm"),
            )
        )
        bridge = _make_bridge("This week: shipped a release and hit a breakthrough.")

        result = summarize_week(journal=journal, days=7, bridge=bridge, now=NOW)

        assert result.entry_count == 2  # old entry excluded
        assert result.window_days == 7
        assert result.text == "This week: shipped a release and hit a breakthrough."
        assert result.since[:10] == "2026-06-26"
        assert result.until[:10] == "2026-07-03"
        bridge.generate.assert_called_once()

        # The prompt must contain the in-window entries and not the ancient one.
        _system, prompt, _signal = bridge.generate.call_args.args
        assert "shipped skcapstone" in prompt
        assert "breakthrough" in prompt
        assert "ancient" not in prompt

    def test_signal_tags_flag_summary_task(self):
        from skcapstone.journal_summary import summarize_week

        journal = _fake_journal(
            _journal_text(_entry_markdown("One", NOW - timedelta(days=1), "note"))
        )
        bridge = _make_bridge("Summary.")
        summarize_week(journal=journal, days=7, bridge=bridge, now=NOW)

        _system, _prompt, signal = bridge.generate.call_args.args
        assert "journal" in signal.tags
        assert "summary" in signal.tags

    def test_empty_week_is_graceful_and_skips_llm(self):
        from skcapstone.journal_summary import summarize_week

        journal = _fake_journal(
            _journal_text(_entry_markdown("Old", NOW - timedelta(days=99), "ancient"))
        )
        bridge = _make_bridge("should not be used")

        result = summarize_week(journal=journal, days=7, bridge=bridge, now=NOW)

        assert result.entry_count == 0
        assert "No journal entries" in result.text
        bridge.generate.assert_not_called()

    def test_completely_empty_journal_is_graceful(self):
        from skcapstone.journal_summary import summarize_week

        journal = _fake_journal("")
        bridge = _make_bridge()

        result = summarize_week(journal=journal, days=7, bridge=bridge, now=NOW)

        assert result.entry_count == 0
        bridge.generate.assert_not_called()

    def test_to_dict_roundtrips_fields(self):
        from skcapstone.journal_summary import summarize_week

        journal = _fake_journal(
            _journal_text(_entry_markdown("One", NOW - timedelta(days=1), "note"))
        )
        result = summarize_week(journal=journal, days=7, bridge=_make_bridge("S"), now=NOW)
        d = result.to_dict()
        assert d["entry_count"] == 1
        assert d["window_days"] == 7
        assert d["text"] == "S"
        assert "generated_at" in d


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestJournalSummaryCLI:
    def test_cli_week_renders_summary(self):
        from skcapstone.cli import main
        from skcapstone.journal_summary import WeeklyJournalSummary

        fake = WeeklyJournalSummary(
            text="A calm, productive week of shipping.",
            entry_count=3,
            window_days=7,
            since="2026-06-26T12:00:00+00:00",
            until="2026-07-03T12:00:00+00:00",
            generated_at="2026-07-03T12:00:00+00:00",
        )
        with patch("skcapstone.journal_summary.summarize_week", return_value=fake):
            runner = CliRunner()
            res = runner.invoke(main, ["journal", "summary", "--week"])

        assert res.exit_code == 0, res.output
        assert "productive week" in res.output

    def test_cli_json_out(self):
        from skcapstone.cli import main
        from skcapstone.journal_summary import WeeklyJournalSummary

        fake = WeeklyJournalSummary(
            text="Week recap.",
            entry_count=2,
            window_days=7,
            since="2026-06-26T12:00:00+00:00",
            until="2026-07-03T12:00:00+00:00",
            generated_at="2026-07-03T12:00:00+00:00",
        )
        with patch("skcapstone.journal_summary.summarize_week", return_value=fake):
            runner = CliRunner()
            res = runner.invoke(main, ["journal", "summary", "--json-out"])

        assert res.exit_code == 0, res.output
        data = json.loads(res.output)
        assert data["entry_count"] == 2
        assert data["text"] == "Week recap."

    def test_cli_empty_week_message(self):
        from skcapstone.cli import main
        from skcapstone.journal_summary import WeeklyJournalSummary

        fake = WeeklyJournalSummary(
            text="No journal entries in the last 7 days.",
            entry_count=0,
            window_days=7,
            since="2026-06-26T12:00:00+00:00",
            until="2026-07-03T12:00:00+00:00",
            generated_at="2026-07-03T12:00:00+00:00",
        )
        with patch("skcapstone.journal_summary.summarize_week", return_value=fake):
            runner = CliRunner()
            res = runner.invoke(main, ["journal", "summary"])

        assert res.exit_code == 0, res.output
        assert "No journal entries" in res.output
