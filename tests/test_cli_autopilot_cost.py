"""Tests for the `skcapstone autopilot-cost` overview command.

The command is a thin, lazily-imported presentation layer over
skharness.autocode.autopilot_cost.summary(); these tests exercise the CLI
wiring (registration, --help, --json-out, the "unavailable" fallback) rather
than re-testing the ledger aggregation itself (that's skharness's own
tests/test_autopilot_cost.py).

No em/en dashes anywhere (SKWorld hard rule).
"""

from __future__ import annotations

import builtins
import json

import pytest
from click.testing import CliRunner


@pytest.fixture(autouse=True)
def _isolate_cost_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("SKAI_COST_DIR", str(tmp_path / "autopilot-cost"))


def _require_autopilot_cost():
    """Skip when the optional skharness sibling is missing or too old to
    ship ``skharness.autocode.autopilot_cost``.

    skharness is deliberately NOT a declared dependency of skcapstone (the
    command lazily imports it and degrades to a friendly message), so these
    rendering tests can only run where the sibling is installed; CI installs
    it from git main.
    """
    return pytest.importorskip(
        "skharness.autocode.autopilot_cost",
        reason="skharness with autocode.autopilot_cost is not installed here",
    )


def test_help():
    from skcapstone.cli import main

    result = CliRunner().invoke(main, ["autopilot-cost", "--help"])
    assert result.exit_code == 0
    assert "cost overview" in result.output.lower()


def test_runs_clean_with_no_ledger_data():
    _require_autopilot_cost()

    from skcapstone.cli import main

    result = CliRunner().invoke(main, ["autopilot-cost"])
    assert result.exit_code == 0
    assert "Autopilot Cost Overview" in result.output
    assert "No runs recorded yet" in result.output


def test_text_output_leads_with_joules():
    # Joules are the canonical SKWorld cost unit (Chef's ask); the text
    # overview must lead with the joule figure, USD following in parens.
    from datetime import datetime, timezone

    apc = _require_autopilot_cost()
    JOULE_PER_USD, record_run = apc.JOULE_PER_USD, apc.record_run

    from skcapstone.cli import main

    today = datetime.now(timezone.utc).date().isoformat()
    record_run(
        card_id="c1",
        repo="skrender",
        tokens=1000,
        cost_usd=3.5,
        passed=True,
        pr="https://x/pr/1",
        ts=f"{today}T00:00:00+00:00",
    )

    result = CliRunner().invoke(main, ["autopilot-cost"])
    assert result.exit_code == 0
    joules = round(3.5 * JOULE_PER_USD)
    assert f"{joules:,} J" in result.output
    assert "($3.50)" in result.output
    assert result.output.index(f"{joules:,} J") < result.output.index("($3.50)")


def test_json_out_reports_recorded_run():
    from datetime import datetime, timezone

    apc = _require_autopilot_cost()
    JOULE_PER_USD, record_run = apc.JOULE_PER_USD, apc.record_run

    from skcapstone.cli import main

    today = datetime.now(timezone.utc).date().isoformat()
    record_run(
        card_id="c1",
        repo="skrender",
        tokens=1000,
        cost_usd=3.5,
        passed=True,
        pr="https://x/pr/1",
        ts=f"{today}T00:00:00+00:00",
    )

    result = CliRunner().invoke(main, ["autopilot-cost", "--json-out"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["today"]["cost_usd"] == 3.5
    assert data["today"]["tokens"] == 1000
    assert data["today"]["joules"] == round(3.5 * JOULE_PER_USD)
    assert data["by_repo"]["skrender"]["runs"] == 1
    assert data["by_repo"]["skrender"]["joules"] == round(3.5 * JOULE_PER_USD)
    assert data["cap_joules"] == round(data["cap_usd"] * JOULE_PER_USD)


def test_friendly_message_when_skharness_unavailable(monkeypatch):
    from skcapstone.cli import main

    real_import = builtins.__import__

    def _blocked_import(name, *args, **kwargs):
        if name == "skharness.autocode.autopilot_cost" or name.startswith("skharness"):
            raise ImportError("simulated: skharness not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    result = CliRunner().invoke(main, ["autopilot-cost"])
    assert result.exit_code == 0
    assert "cost tracking unavailable" in result.output
