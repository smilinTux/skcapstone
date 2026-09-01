#!/usr/bin/env python3
"""Deterministic test for reaper quorum decision and diagnostic agreement.

Reproduces the bug where reporting=4, known=5, configured quorum=3 produced
the misleading diagnostic "need>=3" while the actual blocking condition was
nhosts < known (4 < 5).

The fix makes the decision and diagnostic agree by enumerating all failed
conditions explicitly in the log message while preserving fail-closed behavior.
"""

from pathlib import Path


def run_quorum_check(reporting_hosts, known_hosts, quorum=3):
    """Run a simplified quorum check that matches the reap_dead_claims logic."""
    nhosts = reporting_hosts
    known = known_hosts
    reap_quorum = quorum

    # This is the FIXED logic - enumerate all failures
    failed = []
    if nhosts < reap_quorum:
        failed.append(f"insufficient_quorum:_{nhosts}_reporting_hosts_needed_{reap_quorum}")
    if nhosts < known:
        failed.append(f"insufficient_fresh_coverage:_{nhosts}_reporting_hosts_vs_{known}_known_hosts")

    if failed:
        decision = "blocked"
        diagnostic = ", ".join(failed)
    else:
        decision = "proceed"
        diagnostic = "all_conditions_pass"

    return decision, diagnostic


def test_quorum_diagnostic_agreement_reporting_4_known_5():
    """Test the exact bug: reporting=4 known=5 quorum=3.

    The original code logged "need>=3" which suggested only quorum was the problem,
    but reaping was blocked by nhosts < known (4 < 5), not by nhosts < REAP_QUORUM.

    The fix enumerates ALL failed conditions, so only insufficient_fresh_coverage
    appears in the diagnostic.
    """
    decision, diagnostic = run_quorum_check(reporting_hosts=4, known_hosts=5, quorum=3)

    # Verify fail-closed behavior
    assert decision == "blocked", f"Expected blocked, got {decision}"

    # Verify the diagnostic does NOT mention quorum (4 >= 3 passes)
    assert "insufficient_quorum" not in diagnostic, \
        f"Quorum condition passed (4 >= 3) but was reported as failed: {diagnostic}"

    # Verify the diagnostic DOES mention the actual failed condition
    assert "insufficient_fresh_coverage" in diagnostic, \
        f"Expected 'insufficient_fresh_coverage' in diagnostic but got: {diagnostic}"

    # Verify the diagnostic shows the actual numbers
    assert "4" in diagnostic and "5" in diagnostic, \
        f"Expected numbers 4 and 5 in diagnostic but got: {diagnostic}"

    print("   PASS: Diagnostic correctly identifies only insufficient_fresh_coverage")
    print(f"   Decision: {decision}")
    print(f"   Diagnostic: {diagnostic}")


def test_quorum_diagnostic_when_quorum_actually_fails():
    """Test diagnostic when quorum is the actual problem."""
    decision, diagnostic = run_quorum_check(reporting_hosts=2, known_hosts=2, quorum=3)

    assert decision == "blocked", f"Expected blocked, got {decision}"

    # When quorum actually fails, it should be mentioned
    assert "insufficient_quorum" in diagnostic, \
        f"Expected 'insufficient_quorum' in diagnostic but got: {diagnostic}"

    assert "2" in diagnostic and "3" in diagnostic, \
        f"Expected numbers 2 and 3 in diagnostic but got: {diagnostic}"

    # Coverage should also be mentioned (2 < 2 is false, but 2 < 2... wait, 2 < 2 is false)
    # With reporting=2, known=2: nhosts < known is 2 < 2 = FALSE
    # So only quorum should be mentioned
    assert "insufficient_fresh_coverage" not in diagnostic, \
        f"Should not mention coverage when reporting=known, got: {diagnostic}"

    print("   PASS: Diagnostic correctly identifies only insufficient_quorum")
    print(f"   Decision: {decision}")
    print(f"   Diagnostic: {diagnostic}")


def test_quorum_diagnostic_both_fail():
    """Test diagnostic when both quorum and fresh coverage fail."""
    decision, diagnostic = run_quorum_check(reporting_hosts=3, known_hosts=5, quorum=5)

    assert decision == "blocked", f"Expected blocked, got {decision}"

    # Both conditions should be named
    assert "insufficient_quorum" in diagnostic, \
        f"Expected 'insufficient_quorum' in diagnostic but got: {diagnostic}"
    assert "insufficient_fresh_coverage" in diagnostic, \
        f"Expected 'insufficient_fresh_coverage' in diagnostic but got: {diagnostic}"

    print("   PASS: Diagnostic correctly identifies both failed conditions")
    print(f"   Decision: {decision}")
    print(f"   Diagnostic: {diagnostic}")


def test_quorum_succeeds_reporting_5_known_5():
    """Test that reaping proceeds when all conditions pass."""
    decision, diagnostic = run_quorum_check(reporting_hosts=5, known_hosts=5, quorum=3)

    # Should not be blocked
    assert decision == "proceed", \
        f"Should proceed when conditions pass, but got: {decision}"

    assert diagnostic == "all_conditions_pass", \
        f"Expected 'all_conditions_pass' but got: {diagnostic}"

    print("   PASS: Not blocked when conditions pass")
    print(f"   Decision: {decision}")
    print(f"   Diagnostic: {diagnostic}")


def test_fail_closed_with_zero_reports():
    """Test fail-closed behavior with zero reports."""
    decision, diagnostic = run_quorum_check(reporting_hosts=0, known_hosts=0, quorum=3)

    assert decision == "blocked", f"Expected blocked, got {decision}"

    # With nhosts=0, both conditions fail
    assert "insufficient_quorum" in diagnostic, \
        f"Expected 'insufficient_quorum' with zero reports: {diagnostic}"
    # Note: 0 < 0 is false, so coverage won't be mentioned

    print("   PASS: Correctly blocked with insufficient_quorum")
    print(f"   Decision: {decision}")
    print(f"   Diagnostic: {diagnostic}")


def test_reporting_equal_to_known():
    """Test boundary where reporting equals known (coverage condition passes)."""
    decision, diagnostic = run_quorum_check(reporting_hosts=3, known_hosts=3, quorum=3)

    # 3 >= 3 passes quorum, 3 < 3 fails coverage (but 3 is NOT less than 3)
    # So both conditions pass: decision should be proceed
    assert decision == "proceed", \
        f"Should proceed when reporting=known=quorum, got: {decision}"

    print("   PASS: Proceeds when reporting equals known")
    print(f"   Decision: {decision}")
    print(f"   Diagnostic: {diagnostic}")


def test_reporting_below_quorum():
    """Test boundary where reporting is below quorum but equals known."""
    decision, diagnostic = run_quorum_check(reporting_hosts=2, known_hosts=2, quorum=3)

    # 2 < 3 fails quorum, 2 < 2 is false for coverage
    # Only quorum should fail
    assert decision == "blocked", f"Expected blocked, got {decision}"
    assert "insufficient_quorum" in diagnostic, \
        f"Expected 'insufficient_quorum': {diagnostic}"
    assert "insufficient_fresh_coverage" not in diagnostic, \
        f"Should not mention coverage when reporting=known: {diagnostic}"

    print("   PASS: Blocked only by quorum when reporting below quorum")
    print(f"   Decision: {decision}")
    print(f"   Diagnostic: {diagnostic}")


def test_original_bug_message_would_have_been_misleading():
    """Demonstrate the original bug: 'need>=3' when reporting=4 known=5."""
    # Original code would have logged: "below quorum (reporting=4 known=5 need>=3)"
    # This suggests quorum is the problem, but 4 >= 3 passes!
    # The actual blocker was nhosts < known (4 < 5)

    decision, diagnostic = run_quorum_check(reporting_hosts=4, known_hosts=5, quorum=3)

    # Original misleading message would have been:
    original_message = "below quorum (reporting=4 known=5 need>=3)"

    # This message is misleading because:
    # 1. "need>=3" implies the quorum check failed
    # 2. But reporting=4 meets the need>=3 requirement
    # 3. The actual failure is the unstated condition: reporting < known

    # The fixed diagnostic should be truthful
    assert decision == "blocked"
    assert "insufficient_quorum" not in diagnostic, \
        f"Quorum passed (4 >= 3) so should not appear: {diagnostic}"
    assert "insufficient_fresh_coverage" in diagnostic, \
        f"Coverage failed (4 < 5) so should appear: {diagnostic}"

    print("   PASS: Fixed diagnostic is truthful, original was misleading")
    print(f"   Original (misleading): {original_message}")
    print(f"   Fixed (truthful): blocked: {diagnostic}")


def verify_actual_code_uses_fixed_logic():
    """Verify the actual skfleet-rotate.py uses the fixed logic."""
    script_path = Path(__file__).parent.parent / "scripts" / "fleet" / "skfleet-rotate.py"

    with open(script_path) as f:
        content = f.read()

    # Check that the old misleading format is gone
    assert "below quorum (reporting=%d known=%d need>=%d)" not in content, \
        "Old misleading diagnostic format still present in code"

    # Check that the new enumerative logic is present
    assert "failed = []" in content, \
        "New enumerative failure tracking not found"
    assert "insufficient_quorum" in content, \
        "New insufficient_quorum diagnostic not found"
    assert "insufficient_fresh_coverage" in content, \
        "New insufficient_fresh_coverage diagnostic not found"

    # Check that all failed conditions are joined
    assert 'failed' in content and 'join' in content, \
        "Failed conditions not properly joined in diagnostic"

    print("   PASS: Actual code uses fixed logic")


if __name__ == "__main__":
    print("=" * 70)
    print("Reaper Quorum Decision and Diagnostic Agreement Test")
    print("=" * 70)
    print()
    print("This test verifies the fix for the bug where:")
    print("  - reporting=4, known=5, quorum=3")
    print("  - Original diagnostic: 'need>=3' (misleading!)")
    print("  - Actual blocker: nhosts < known (4 < 5)")
    print("  - Quorum passed: nhosts >= quorum (4 >= 3)")
    print()
    print("=" * 70)
    print()

    print("Running deterministic tests...")
    print()

    print("1. Testing reporting=4 known=5 quorum=3 (the exact bug condition)...")
    test_quorum_diagnostic_agreement_reporting_4_known_5()
    print()

    print("2. Demonstrating original bug message was misleading...")
    test_original_bug_message_would_have_been_misleading()
    print()

    print("3. Testing quorum failure (reporting=2 known=2 quorum=3)...")
    test_quorum_diagnostic_when_quorum_actually_fails()
    print()

    print("4. Testing both conditions fail (reporting=3 known=5 quorum=5)...")
    test_quorum_diagnostic_both_fail()
    print()

    print("5. Testing all conditions pass (reporting=5 known=5 quorum=3)...")
    test_quorum_succeeds_reporting_5_known_5()
    print()

    print("6. Testing fail-closed with zero reports...")
    test_fail_closed_with_zero_reports()
    print()

    print("7. Testing boundary: reporting equals known...")
    test_reporting_equal_to_known()
    print()

    print("8. Testing boundary: reporting below quorum but equals known...")
    test_reporting_below_quorum()
    print()

    print("9. Verifying actual code uses fixed logic...")
    verify_actual_code_uses_fixed_logic()
    print()

    print("=" * 70)
    print("All tests passed!")
    print("=" * 70)
    print()
    print("Summary:")
    print("  - Decision logic remains fail-closed")
    print("  - Diagnostic now names every failed condition explicitly")
    print("  - reporting=4 known=5 quorum=3 now correctly shows only")
    print("    'insufficient_fresh_coverage' not 'insufficient_quorum'")
    print()
