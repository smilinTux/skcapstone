"""CR-P3.2 fault-injection drill (card b993eaaa, epic fb3cc09d): assembled
end-to-end proof that the ATLAS safety mechanisms work TOGETHER, not just in
isolation. Runs the real harness against an isolated tmp_path root -- never
production -- and checks the guard, the persisted artifacts, and that every
scenario behaved as documented (or, for the deliberate gap scenarios,
correctly demonstrated the gap).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skcapstone.fleet import drill as fleet_drill
from skcapstone.operator_seat import fault_injection_drill as drill


def test_refuses_a_root_inside_the_sovereign_tree():
    with pytest.raises(fleet_drill.UnsafeDrillRootError):
        drill.guard_drill_root(fleet_drill.sovereign_home() / "fleet")
    with pytest.raises(fleet_drill.UnsafeDrillRootError):
        drill.guard_drill_root(fleet_drill.sovereign_home())


def test_refuses_traversal_into_the_sovereign_tree():
    # A path that RESOLVES inside the sovereign home must be refused even when
    # spelled with a redundant "up and back down" traversal segment -- the
    # guard judges the resolved path, not the string.
    sneaky = f"{fleet_drill.sovereign_home()}/decoy/../fleet"
    assert Path(sneaky).resolve() == fleet_drill.sovereign_home() / "fleet"
    with pytest.raises(fleet_drill.UnsafeDrillRootError):
        drill.guard_drill_root(sneaky)


def test_full_drill_runs_clean_against_an_isolated_root(tmp_path):
    root = tmp_path / "atlas-drill-fleet"
    report = drill.run_all(root, keep_root=True)

    # Every scenario ran (no None crept in) and produced a verdict.
    assert len(report.results) == len(drill.SCENARIOS)
    failed = [r for r in report.results if not r.passed]
    assert not failed, "\n".join(f"{r.name}: {r.note} {r.evidence}" for r in failed)

    # The two safety-critical artifacts genuinely exist on disk now.
    ledger_dir = report.root / "atlas" / "action-ledger"
    state_path = report.root / "atlas" / "state" / "execution-state.json"
    assert ledger_dir.is_dir()
    assert (ledger_dir / "intents").is_dir()
    assert any((ledger_dir / "intents").glob("*.json"))
    assert state_path.is_file()

    # Documented gaps are surfaced, not swallowed. Card 0e98a570 fixed the
    # ledger-terminal-dead-end gap (scenario 11) and the auto-created-ITIL-
    # change correlation loss (scenario 7), so neither carries a `finding`
    # any more -- only the gaps this drill is NOT permitted to patch remain.
    finding_scenarios = {
        name
        for name in (
            "9. stale evidence is not rejected [GAP]",
            "12. mid-run freeze race",
        )
    }
    reported = {r.name for r in report.results if r.finding}
    assert finding_scenarios & reported, "expected at least the known gaps to surface a finding"

    # The drill root is never, ever the resolved production tree.
    assert report.root != fleet_drill.sovereign_home() / "fleet"
    assert fleet_drill.sovereign_home() not in report.root.parents
