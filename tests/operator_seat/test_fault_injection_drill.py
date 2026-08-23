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
    # Scenarios 7 and 10 need a REAL capauth signer (a PGP identity in the
    # host keyring). A CI runner has none and never will, so on CI the ledger
    # correctly falls back to UNSIGNED and those two cannot run. That is an
    # ENVIRONMENT limit, not a defect, and it is kept separate from a genuine
    # failure on purpose: the standalone drill script still FAILS LOUDLY when
    # signing is unavailable, because "the estate is running unsigned" is
    # exactly the thing a human must never see pass quietly. Only this pytest
    # wrapper tolerates it, and it says so by name.
    needs_signer = {r.name for r in report.results if not r.passed and "signer" in (r.note or "")}
    failed = [r for r in report.results if not r.passed and r.name not in needs_signer]
    assert not failed, "\n".join(f"{r.name}: {r.note} {r.evidence}" for r in failed)
    if needs_signer:
        import pytest

        pytest.skip(f"no capauth signer on this host; unrunnable here: {sorted(needs_signer)}")

    # The two safety-critical artifacts genuinely exist on disk now.
    ledger_dir = report.root / "atlas" / "action-ledger"
    state_path = report.root / "atlas" / "state" / "execution-state.json"
    assert ledger_dir.is_dir()
    assert (ledger_dir / "intents").is_dir()
    assert any((ledger_dir / "intents").glob("*.json"))
    assert state_path.is_file()

    # Documented gaps are surfaced, not swallowed.
    finding_scenarios = {
        name
        for name in (
            "7. signed ledger (real PGP) + ITIL correlation",
            "9. stale evidence is not rejected [GAP]",
            "11. ledger cannot represent a later, separate occurrence [GAP]",
            "12. mid-run freeze race",
        )
    }
    reported = {r.name for r in report.results if r.finding}
    assert finding_scenarios & reported, "expected at least the known gaps to surface a finding"

    # The drill root is never, ever the resolved production tree.
    assert report.root != fleet_drill.sovereign_home() / "fleet"
    assert fleet_drill.sovereign_home() not in report.root.parents
