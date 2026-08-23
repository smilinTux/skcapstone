#!/usr/bin/env python3
"""Run the ATLAS P3.2 fault-injection drill (coord card b993eaaa, epic fb3cc09d).

Single command:

    ~/.skenv/bin/python scripts/atlas_p32_fault_injection_drill.py

Exercises the merged-but-never-fired-together safety mechanisms (fault
injection, performed=False handling, cooldown, circuit breaker, post-action
verification, typed rollback + escalation, the signed action ledger, ITIL
correlation) end to end against an ISOLATED, guarded fleet root, and drills
the gaps the existing unit tests don't cover (duplicate observations, stale
evidence, auth expiry, a mid-run freeze race, scheduler overlap).

NEVER touches production. The root is resolved through
skcapstone.fleet.drill.resolve_drill_root before anything is written; a root
that resolves inside ~/.skcapstone raises before this script does anything
else. See src/skcapstone/operator_seat/fault_injection_drill.py for the full
safety rationale.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skcapstone.fleet import drill as fleet_drill  # noqa: E402
from skcapstone.fleet import store  # noqa: E402
from skcapstone.fleet.paths import FleetPaths  # noqa: E402
from skcapstone.operator_seat import fault_injection_drill as drill  # noqa: E402


def _print_header(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=None,
        help="Drill root (default: $SKFLEET_ROOT or /tmp/atlas-drill-fleet). "
        "Refused if it resolves inside the sovereign home.",
    )
    parser.add_argument(
        "--teardown",
        action="store_true",
        help="Delete the drill tree after the run instead of leaving it for inspection.",
    )
    args = parser.parse_args()

    candidate = args.root or drill.default_candidate_root()

    _print_header("ATLAS P3.2 fault-injection drill (coord b993eaaa / epic fb3cc09d)")
    print(f"candidate root : {candidate}")
    try:
        guarded = drill.guard_drill_root(candidate)
    except fleet_drill.UnsafeDrillRootError as exc:
        print(f"REFUSED: {exc}")
        return 2
    print(f"guarded root   : {guarded}")

    report = drill.run_all(candidate, keep_root=not args.teardown)

    _print_header("SCENARIO RESULTS")
    for result in report.results:
        print(result.line())
        for key, value in result.evidence.items():
            print(f"    {key}: {value}")

    _print_header("SUMMARY TABLE")
    print(f"{'scenario':<62} {'result':<8} evidence")
    print("-" * 100)
    for result in report.results:
        mark = "PASS" if result.passed else "FAIL"
        first_key = next(iter(result.evidence), None)
        sample = f"{first_key}={result.evidence.get(first_key)!r}" if first_key else result.note
        print(f"{result.name:<62} {mark:<8} {sample}")

    if report.findings:
        _print_header("FINDINGS (mechanisms that did NOT behave as fully documented)")
        for finding in report.findings:
            print(f"- {finding}")
    else:
        print("\nNo findings recorded.")

    ledger_dir = report.root / "atlas" / "action-ledger"
    state_path = report.root / "atlas" / "state" / "execution-state.json"
    _print_header("PERSISTED ARTIFACTS")
    print(f"action-ledger/ exists : {ledger_dir.exists()}")
    if ledger_dir.exists():
        intents_dir = ledger_dir / "intents"
        intents = sorted(intents_dir.glob("*.json")) if intents_dir.exists() else []
        events_dir = ledger_dir / "events"
        events = sorted(events_dir.glob("*.jsonl")) if events_dir.exists() else []
        print(f"  intents: {len(intents)}   event streams: {len(events)}")
        if events:
            sample_lines = events[0].read_text().splitlines()
            if sample_lines:
                sample = json.loads(sample_lines[-1])
                sig = sample.get("signature")
                if sig:
                    sample["signature"] = (
                        sig[:24] + "...<redacted>..." + sig[-16:]
                        if len(sig) > 48
                        else "<redacted>"
                    )
                print(f"  sample event ({events[0].name}, last line, signature redacted):")
                print("   ", json.dumps(sample, indent=2).replace("\n", "\n    "))
    print(f"execution-state.json exists : {state_path.exists()}")
    if state_path.exists():
        print("  contents:")
        print("   ", state_path.read_text().replace("\n", "\n    "))

    _print_header("PRODUCTION SAFETY CONFIRMATION")
    # Deliberately NOT default_paths(): that honors SKFLEET_ROOT, which is
    # exactly the variable an operator would have pointed at this drill's own
    # root to run it. The production reference here must be the real,
    # un-overridable sovereign tree regardless of what SKFLEET_ROOT says.
    prod_root = (fleet_drill.sovereign_home() / "fleet").resolve()
    prod_paths = FleetPaths(root=prod_root)
    prod_frozen = store.is_frozen(prod_paths)
    drill_outside_prod = not (
        report.root == prod_root or prod_root in report.root.parents
    )
    print(f"production fleet root       : {prod_root}")
    print(f"production frozen           : {prod_frozen}")
    print(f"drill root                  : {report.root}")
    print(f"drill root outside prod tree: {drill_outside_prod}")
    if not prod_frozen:
        print("!! PRODUCTION FREEZE IS OFF - this is unexpected and must be investigated,")
        print("!! the drill never toggles it and did not do so here.")
    if not drill_outside_prod:
        print("!! DRILL ROOT RESOLVED INSIDE THE PRODUCTION TREE - this should be impossible")
        print("!! given the guard; treat this run's results as invalid.")
        return 2

    ok = report.all_passed and prod_frozen and drill_outside_prod
    _print_header("RESULT")
    print(f"scenarios: {sum(r.passed for r in report.results)}/{len(report.results)} passed")
    print(f"overall: {'PASS' if ok else 'SEE FINDINGS/FAILURES ABOVE'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
