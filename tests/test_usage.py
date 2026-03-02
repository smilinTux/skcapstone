"""Tests for LLM token usage tracking."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from skcapstone.usage import (
    DailyUsageReport,
    ModelUsageSummary,
    UsageTracker,
    _cost_per_million,
    _today_str,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Minimal agent home directory."""
    return tmp_path


@pytest.fixture
def tracker(home: Path) -> UsageTracker:
    """UsageTracker backed by a temp directory."""
    return UsageTracker(home)


# ---------------------------------------------------------------------------
# Cost table
# ---------------------------------------------------------------------------


class TestCostTable:
    """Unit tests for _cost_per_million()."""

    def test_claude_sonnet_priced(self) -> None:
        """Claude Sonnet returns non-zero pricing."""
        inp, out = _cost_per_million("claude-sonnet-4-6")
        assert inp > 0
        assert out > inp  # output always more expensive

    def test_ollama_free(self) -> None:
        """Ollama / local models have zero cost."""
        inp, out = _cost_per_million("ollama:llama3.1")
        assert inp == 0.0
        assert out == 0.0

    def test_passthrough_free(self) -> None:
        """Passthrough backend is always free."""
        inp, out = _cost_per_million("passthrough")
        assert inp == 0.0
        assert out == 0.0

    def test_unknown_model_has_nonzero_fallback(self) -> None:
        """Unknown models get a conservative non-zero price."""
        inp, out = _cost_per_million("some-unknown-model-xyz")
        assert inp > 0
        assert out > 0

    def test_gpt4o_priced(self) -> None:
        """GPT-4o returns positive pricing."""
        inp, out = _cost_per_million("gpt-4o")
        assert inp > 0
        assert out > inp

    def test_claude_opus_more_expensive_than_haiku(self) -> None:
        """Opus costs more per token than Haiku."""
        opus_inp, opus_out = _cost_per_million("claude-opus-4-6")
        haiku_inp, haiku_out = _cost_per_million("claude-haiku-4-5")
        assert opus_inp > haiku_inp
        assert opus_out > haiku_out


# ---------------------------------------------------------------------------
# UsageTracker.record_usage
# ---------------------------------------------------------------------------


class TestRecordUsage:
    """Tests for the write path."""

    def test_creates_usage_file(self, tracker: UsageTracker, home: Path) -> None:
        """record_usage creates tokens-{date}.json."""
        date_str = "2026-03-02"
        tracker.record_usage("ollama:llama3.1", 100, 50, date_str=date_str)
        path = home / "usage" / f"tokens-{date_str}.json"
        assert path.exists()

    def test_accumulates_calls(self, tracker: UsageTracker) -> None:
        """Multiple record_usage calls accumulate counters."""
        date_str = "2026-03-02"
        for _ in range(5):
            tracker.record_usage("ollama:llama3.1", 100, 50, date_str=date_str)
        report = tracker.get_daily(date_str)
        summary = report.models["ollama:llama3.1"]
        assert summary.calls == 5
        assert summary.input_tokens == 500
        assert summary.output_tokens == 250

    def test_multiple_models_tracked_separately(self, tracker: UsageTracker) -> None:
        """Different models accumulate independently."""
        date_str = "2026-03-02"
        tracker.record_usage("ollama:llama3.1", 100, 50, date_str=date_str)
        tracker.record_usage("claude-sonnet-4-6", 200, 80, date_str=date_str)
        report = tracker.get_daily(date_str)
        assert "ollama:llama3.1" in report.models
        assert "claude-sonnet-4-6" in report.models
        assert report.models["ollama:llama3.1"].calls == 1
        assert report.models["claude-sonnet-4-6"].calls == 1

    def test_cost_zero_for_local_model(self, tracker: UsageTracker) -> None:
        """Local ollama models accumulate zero estimated cost."""
        date_str = "2026-03-02"
        tracker.record_usage("ollama:llama3.1", 10_000, 5_000, date_str=date_str)
        report = tracker.get_daily(date_str)
        assert report.models["ollama:llama3.1"].estimated_cost_usd == 0.0

    def test_cost_nonzero_for_paid_model(self, tracker: UsageTracker) -> None:
        """Paid models accumulate a positive estimated cost."""
        date_str = "2026-03-02"
        tracker.record_usage("claude-sonnet-4-6", 1_000_000, 100_000, date_str=date_str)
        report = tracker.get_daily(date_str)
        assert report.models["claude-sonnet-4-6"].estimated_cost_usd > 0

    def test_json_file_readable(self, tracker: UsageTracker, home: Path) -> None:
        """Persisted file is valid JSON."""
        date_str = "2026-03-02"
        tracker.record_usage("ollama:llama3.1", 100, 50, date_str=date_str)
        path = home / "usage" / f"tokens-{date_str}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "models" in data
        assert "ollama:llama3.1" in data["models"]


# ---------------------------------------------------------------------------
# UsageTracker.get_daily
# ---------------------------------------------------------------------------


class TestGetDaily:
    """Tests for the daily read path."""

    def test_empty_day_returns_report(self, tracker: UsageTracker) -> None:
        """get_daily for a day with no data returns an empty report."""
        report = tracker.get_daily("2099-01-01")
        assert isinstance(report, DailyUsageReport)
        assert report.date == "2099-01-01"
        assert report.models == {}

    def test_total_tokens_property(self, tracker: UsageTracker) -> None:
        """total_tokens sums input + output across all models."""
        date_str = "2026-03-02"
        tracker.record_usage("ollama:llama3.1", 100, 40, date_str=date_str)
        tracker.record_usage("claude-sonnet-4-6", 200, 60, date_str=date_str)
        report = tracker.get_daily(date_str)
        assert report.total_input_tokens == 300
        assert report.total_output_tokens == 100
        assert report.total_tokens == 400

    def test_corrupt_file_returns_empty(self, home: Path) -> None:
        """A corrupt JSON file returns an empty report instead of crashing."""
        date_str = "2026-03-02"
        usage_dir = home / "usage"
        usage_dir.mkdir(parents=True)
        (usage_dir / f"tokens-{date_str}.json").write_text("not json {{{", encoding="utf-8")
        tracker = UsageTracker(home)
        report = tracker.get_daily(date_str)
        assert isinstance(report, DailyUsageReport)
        assert report.models == {}


# ---------------------------------------------------------------------------
# UsageTracker.get_weekly / get_monthly
# ---------------------------------------------------------------------------


class TestRangeQueries:
    """Tests for weekly and monthly range queries."""

    def test_weekly_returns_7_days(self, tracker: UsageTracker) -> None:
        """get_weekly returns exactly 7 DailyUsageReport objects."""
        reports = tracker.get_weekly()
        assert len(reports) == 7

    def test_monthly_returns_30_days(self, tracker: UsageTracker) -> None:
        """get_monthly returns exactly 30 DailyUsageReport objects."""
        reports = tracker.get_monthly()
        assert len(reports) == 30

    def test_weekly_includes_data(self, tracker: UsageTracker) -> None:
        """get_weekly includes a day that has data recorded."""
        from datetime import date, timedelta
        today = date.today().strftime("%Y-%m-%d")
        tracker.record_usage("ollama:llama3.1", 100, 50, date_str=today)
        reports = tracker.get_weekly()
        total = sum(r.total_calls for r in reports)
        assert total == 1

    def test_aggregate_sums_correctly(self, tracker: UsageTracker) -> None:
        """aggregate() totals across multiple days."""
        tracker.record_usage("ollama:llama3.1", 100, 50, date_str="2026-03-01")
        tracker.record_usage("ollama:llama3.1", 200, 80, date_str="2026-03-02")
        reports = [tracker.get_daily("2026-03-01"), tracker.get_daily("2026-03-02")]
        agg = tracker.aggregate(reports)
        m = agg.models["ollama:llama3.1"]
        assert m.calls == 2
        assert m.input_tokens == 300
        assert m.output_tokens == 130

    def test_aggregate_empty_list(self, tracker: UsageTracker) -> None:
        """aggregate([]) returns a safe empty report."""
        agg = tracker.aggregate([])
        assert agg.date == "empty"
        assert agg.models == {}


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    """Concurrent record_usage calls must not corrupt data."""

    def test_concurrent_writes_same_model(self, tracker: UsageTracker) -> None:
        """100 concurrent record_usage calls produce correct totals."""
        date_str = "2026-03-02"
        n = 100
        barrier = threading.Barrier(n)

        def _record():
            barrier.wait()
            tracker.record_usage("ollama:llama3.1", 10, 5, date_str=date_str)

        threads = [threading.Thread(target=_record) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        report = tracker.get_daily(date_str)
        m = report.models["ollama:llama3.1"]
        assert m.calls == n
        assert m.input_tokens == n * 10
        assert m.output_tokens == n * 5

    def test_concurrent_writes_different_models(self, tracker: UsageTracker) -> None:
        """Concurrent writes to different models don't lose data."""
        date_str = "2026-03-02"
        n = 50
        barrier = threading.Barrier(n * 2)

        def _record_a():
            barrier.wait()
            tracker.record_usage("model-a", 10, 5, date_str=date_str)

        def _record_b():
            barrier.wait()
            tracker.record_usage("model-b", 20, 10, date_str=date_str)

        threads = (
            [threading.Thread(target=_record_a) for _ in range(n)]
            + [threading.Thread(target=_record_b) for _ in range(n)]
        )
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        report = tracker.get_daily(date_str)
        assert report.models["model-a"].calls == n
        assert report.models["model-b"].calls == n


# ---------------------------------------------------------------------------
# ModelUsageSummary
# ---------------------------------------------------------------------------


class TestModelUsageSummary:
    """Unit tests for the ModelUsageSummary model."""

    def test_total_tokens(self) -> None:
        """total_tokens sums input and output."""
        m = ModelUsageSummary(
            model="test", calls=1, input_tokens=100, output_tokens=50
        )
        assert m.total_tokens == 150

    def test_defaults(self) -> None:
        """All counts default to zero."""
        m = ModelUsageSummary(model="test")
        assert m.calls == 0
        assert m.input_tokens == 0
        assert m.output_tokens == 0
        assert m.estimated_cost_usd == 0.0
        assert m.total_tokens == 0


# ---------------------------------------------------------------------------
# DailyUsageReport
# ---------------------------------------------------------------------------


class TestDailyUsageReport:
    """Unit tests for the DailyUsageReport model."""

    def test_empty_report_totals(self) -> None:
        """Empty report has all-zero aggregates."""
        r = DailyUsageReport(date="2026-03-02")
        assert r.total_calls == 0
        assert r.total_tokens == 0
        assert r.total_cost_usd == 0.0

    def test_total_cost_aggregates(self) -> None:
        """total_cost_usd sums across all models."""
        r = DailyUsageReport(
            date="2026-03-02",
            models={
                "m1": ModelUsageSummary(model="m1", estimated_cost_usd=0.10),
                "m2": ModelUsageSummary(model="m2", estimated_cost_usd=0.25),
            },
        )
        assert abs(r.total_cost_usd - 0.35) < 1e-9
