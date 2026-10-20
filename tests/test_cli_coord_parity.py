"""Focused CLI regressions for safe parity remediation guidance."""

from __future__ import annotations

from click.testing import CliRunner

from skcapstone import card_store
from skcapstone.cli import main


def test_parity_alert_does_not_recommend_destructive_reconcile(tmp_path, monkeypatch) -> None:
    """An unsafe direction must not be printed as automatic remediation."""
    monkeypatch.setattr(
        card_store,
        "parity_check",
        lambda home, open_drift_threshold: {
            "checked": 1,
            "matched": 0,
            "mismatches": [
                {
                    "id": "done0001",
                    "diff": {"status": ["backlog", "done"]},
                }
            ],
            "informational": [],
            "missing": [],
            "open_legacy": 1,
            "open_store": 0,
            "open_drift": 1,
            "open_drift_threshold": 0,
            "open_alert": True,
        },
    )

    result = CliRunner().invoke(
        main,
        [
            "coord",
            "parity",
            "--home",
            str(tmp_path),
            "--open-threshold",
            "0",
        ],
    )

    assert result.exit_code == 0, result.output
    plain = " ".join(result.output.split())
    assert "No automatic remediation is safe" in plain
    assert "reconcile --apply" not in result.output
