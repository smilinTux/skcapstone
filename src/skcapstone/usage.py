"""
LLM token usage tracking — input/output tokens per model per day.

Records are stored in ~/.skcapstone/usage/tokens-{date}.json, one file
per calendar day (UTC).  Each file accumulates calls to record_usage()
atomically using a threading lock.

Cost estimation uses approximate per-million-token pricing by model
family.  Local models (ollama, passthrough) have zero cost.

Usage:
    from skcapstone.usage import UsageTracker
    tracker = UsageTracker(home=Path("~/.skcapstone"))
    tracker.record_usage("ollama:llama3.1", input_tokens=512, output_tokens=128)
    report = tracker.get_daily()
    print(report)
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("skcapstone.usage")


# ---------------------------------------------------------------------------
# Cost table  (USD per 1 000 000 tokens)
# ---------------------------------------------------------------------------

#: Approximate prices in USD per 1M tokens.  Keys are model-family prefixes
#: matched with str.startswith().  First match wins (ordered from most
#: specific to least specific).
_COST_TABLE: list[tuple[str, float, float]] = [
    # (prefix, input_per_1M, output_per_1M)
    # Anthropic Claude family
    ("claude-opus", 15.0, 75.0),
    ("claude-sonnet", 3.0, 15.0),
    ("claude-haiku", 0.25, 1.25),
    ("claude", 3.0, 15.0),              # generic claude fallback
    # OpenAI
    ("gpt-4o-mini", 0.15, 0.60),
    ("gpt-4o", 2.50, 10.0),
    ("gpt-4", 10.0, 30.0),
    ("gpt-3.5", 0.50, 1.50),
    # NVIDIA NIM / meta on NIM
    ("nvidia/", 1.00, 4.00),
    ("meta/llama", 1.00, 4.00),
    # Groq (fast inference, very cheap)
    ("groq:", 0.05, 0.10),
    # Grok (xAI)
    ("grok", 5.00, 15.0),
    # Kimi
    ("kimi", 0.60, 2.50),
    # Ollama and passthrough → free (local)
    ("ollama", 0.0, 0.0),
    ("passthrough", 0.0, 0.0),
    ("llama", 0.0, 0.0),
    ("mistral", 0.0, 0.0),
    ("qwen", 0.0, 0.0),
    ("phi", 0.0, 0.0),
    ("gemma", 0.0, 0.0),
]


def _cost_per_million(model: str) -> tuple[float, float]:
    """Return (input_per_1M, output_per_1M) cost for a model string.

    Args:
        model: Model identifier, e.g. 'ollama:llama3.1' or 'claude-sonnet-4-6'.

    Returns:
        Tuple of (input cost, output cost) per 1 000 000 tokens in USD.
    """
    lower = model.lower()
    for prefix, inp, out in _COST_TABLE:
        if lower.startswith(prefix.lower()):
            return inp, out
    # Unknown model — use a conservative estimate
    return 1.0, 4.0


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class ModelUsageSummary(BaseModel):
    """Aggregated token counts for a single model."""

    model: str = Field(description="Model identifier")
    calls: int = Field(default=0, description="Number of API calls recorded")
    input_tokens: int = Field(default=0, description="Total prompt/input tokens")
    output_tokens: int = Field(default=0, description="Total completion/output tokens")
    estimated_cost_usd: float = Field(
        default=0.0, description="Estimated cost in USD"
    )

    @property
    def total_tokens(self) -> int:
        """Sum of input and output tokens."""
        return self.input_tokens + self.output_tokens


class DailyUsageReport(BaseModel):
    """Full usage report for a single calendar day."""

    date: str = Field(description="Calendar date (YYYY-MM-DD, UTC)")
    models: dict[str, ModelUsageSummary] = Field(
        default_factory=dict, description="Per-model usage summaries"
    )

    @property
    def total_calls(self) -> int:
        """Total API calls across all models."""
        return sum(m.calls for m in self.models.values())

    @property
    def total_input_tokens(self) -> int:
        """Total input tokens across all models."""
        return sum(m.input_tokens for m in self.models.values())

    @property
    def total_output_tokens(self) -> int:
        """Total output tokens across all models."""
        return sum(m.output_tokens for m in self.models.values())

    @property
    def total_tokens(self) -> int:
        """Total tokens (input + output) across all models."""
        return self.total_input_tokens + self.total_output_tokens

    @property
    def total_cost_usd(self) -> float:
        """Total estimated cost in USD across all models."""
        return sum(m.estimated_cost_usd for m in self.models.values())


# ---------------------------------------------------------------------------
# UsageTracker
# ---------------------------------------------------------------------------


class UsageTracker:
    """Thread-safe LLM token usage tracker.

    Persists one JSON file per calendar day under
    ``{home}/usage/tokens-{date}.json``.

    Args:
        home: Agent home directory (e.g. ~/.skcapstone).
    """

    def __init__(self, home: Path) -> None:
        self._home = Path(home).expanduser()
        self._usage_dir = self._home / "usage"
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    def record_usage(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        date_str: Optional[str] = None,
    ) -> None:
        """Record a single LLM call's token usage.

        Args:
            model: Model identifier (e.g. 'ollama:llama3.1', 'claude-sonnet-4-6').
            input_tokens: Number of input/prompt tokens consumed.
            output_tokens: Number of output/completion tokens produced.
            date_str: Override date in 'YYYY-MM-DD' format (defaults to today UTC).
        """
        if date_str is None:
            date_str = _today_str()
        inp_cost, out_cost = _cost_per_million(model)
        cost = (input_tokens * inp_cost + output_tokens * out_cost) / 1_000_000

        with self._lock:
            data = self._load_raw(date_str)
            entry = data["models"].setdefault(
                model,
                {"calls": 0, "input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0},
            )
            entry["calls"] += 1
            entry["input_tokens"] += input_tokens
            entry["output_tokens"] += output_tokens
            entry["estimated_cost_usd"] = round(entry["estimated_cost_usd"] + cost, 8)
            self._save_raw(date_str, data)

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------

    def get_daily(self, date_str: Optional[str] = None) -> DailyUsageReport:
        """Return usage report for a single day.

        Args:
            date_str: 'YYYY-MM-DD' string (defaults to today UTC).

        Returns:
            DailyUsageReport for that date.
        """
        if date_str is None:
            date_str = _today_str()
        with self._lock:
            data = self._load_raw(date_str)
        return _raw_to_report(date_str, data)

    def get_weekly(self, anchor: Optional[str] = None) -> list[DailyUsageReport]:
        """Return daily usage reports for the last 7 days.

        Args:
            anchor: End date 'YYYY-MM-DD' (defaults to today UTC).

        Returns:
            List of DailyUsageReport, one per day, oldest first.
        """
        return self._range_reports(7, anchor)

    def get_monthly(self, anchor: Optional[str] = None) -> list[DailyUsageReport]:
        """Return daily usage reports for the last 30 days.

        Args:
            anchor: End date 'YYYY-MM-DD' (defaults to today UTC).

        Returns:
            List of DailyUsageReport, one per day, oldest first.
        """
        return self._range_reports(30, anchor)

    def aggregate(self, reports: list[DailyUsageReport]) -> DailyUsageReport:
        """Aggregate multiple daily reports into one summary.

        Args:
            reports: List of DailyUsageReport instances.

        Returns:
            A single DailyUsageReport with aggregated totals.
            The date field is set to 'range: {first}..{last}'.
        """
        if not reports:
            return DailyUsageReport(date="empty")
        merged: dict[str, ModelUsageSummary] = {}
        for report in reports:
            for model, summary in report.models.items():
                if model not in merged:
                    merged[model] = ModelUsageSummary(model=model)
                m = merged[model]
                m.calls += summary.calls
                m.input_tokens += summary.input_tokens
                m.output_tokens += summary.output_tokens
                m.estimated_cost_usd = round(
                    m.estimated_cost_usd + summary.estimated_cost_usd, 8
                )
        date_label = f"{reports[0].date}..{reports[-1].date}"
        return DailyUsageReport(date=date_label, models=merged)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _range_reports(
        self, days: int, anchor: Optional[str]
    ) -> list[DailyUsageReport]:
        """Return reports for the last *days* calendar days up to anchor."""
        end = _parse_date(anchor) if anchor else date.today()
        reports = []
        for offset in range(days - 1, -1, -1):
            d = end - timedelta(days=offset)
            reports.append(self.get_daily(d.strftime("%Y-%m-%d")))
        return reports

    def _load_raw(self, date_str: str) -> dict:
        """Load raw usage dict from disk (no lock — caller must hold lock)."""
        path = self._usage_dir / f"tokens-{date_str}.json"
        if not path.exists():
            return {"date": date_str, "models": {}}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read usage file %s: %s", path, exc)
            return {"date": date_str, "models": {}}

    def _save_raw(self, date_str: str, data: dict) -> None:
        """Persist raw usage dict to disk (no lock — caller must hold lock)."""
        self._usage_dir.mkdir(parents=True, exist_ok=True)
        path = self._usage_dir / f"tokens-{date_str}.json"
        try:
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.error("Failed to write usage file %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _today_str() -> str:
    """Return today's date in UTC as 'YYYY-MM-DD'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _parse_date(date_str: str) -> date:
    """Parse 'YYYY-MM-DD' to a date object."""
    return datetime.strptime(date_str, "%Y-%m-%d").date()


def _raw_to_report(date_str: str, data: dict) -> DailyUsageReport:
    """Convert a raw usage dict to a DailyUsageReport."""
    models: dict[str, ModelUsageSummary] = {}
    for model, entry in data.get("models", {}).items():
        models[model] = ModelUsageSummary(
            model=model,
            calls=entry.get("calls", 0),
            input_tokens=entry.get("input_tokens", 0),
            output_tokens=entry.get("output_tokens", 0),
            estimated_cost_usd=entry.get("estimated_cost_usd", 0.0),
        )
    return DailyUsageReport(date=date_str, models=models)
