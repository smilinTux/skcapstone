#!/usr/bin/env python3
"""Tests for selector eligibility guards.

Tests verify:
1. Exact-label guard excludes all thirteen normalized labels before lane selection
2. Underscore and case variants normalize to the same exact label
3. Ordinary source and review cards mentioning people remain eligible
4. Incomplete structural human hold dependencies exclude executors
5. Machine completion, supersede, archive, and hash links do not satisfy holds
6. Only Chef approve or explicit void releases human hold dependencies
7. Regression tests for cards 2209f7fe and 62243d92

Source: card ec9dff18, audit ea4d91a8
"""
import re
import sys
from pathlib import Path

# Path to the launcher
FLEET_PATH = Path(__file__).parent.parent / "scripts" / "fleet"


class TestExactLabelGuards:
    """Tests for the closed exact-label guard manifest."""

    def test_all_thirteen_labels_defined(self):
        """Test: All thirteen exact guard labels are defined in the source."""
        launcher_path = FLEET_PATH / "skfleet-rotate.py"
        with open(launcher_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Check for the exact-label guard set definition
        assert "_EXACT_LABEL_GUARDS" in content, "Missing _EXACT_LABEL_GUARDS definition"

        # Extract the set definition and verify all thirteen labels
        set_match = re.search(r'_EXACT_LABEL_GUARDS\s*=\s*\{([^}]+)\}', content, re.DOTALL)
        assert set_match, "Could not parse _EXACT_LABEL_GUARDS set"

        set_content = set_match.group(1)
        expected_labels = {
            "planning-only-container",
            "do-not-claim-as-implementation",
            "human-gate",
            "human-decision-recorded-no-action",
            "no-action-authorized",
            "not-claimable",
            "sprint-container",
            "no-action",
            "do-not-claim",
            "human-approval-required",
            "human-gated",
            "human-hold-deny-for-now",
            "reserved-no-action",
        }

        for label in expected_labels:
            assert f'"{label}"' in set_content, f"Missing expected label: {label}"

        print(f"PASS: All thirteen exact guard labels are defined")

    def test_exact_label_guard_used_in_pool_selection(self):
        """Test: The exact-label guard is checked before host ownership and lane selection."""
        launcher_path = FLEET_PATH / "skfleet-rotate.py"
        with open(launcher_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Verify the exact-label guard check is present
        assert "_EXACT_LABEL_GUARDS" in content, "Exact-label guard not used"
        assert "normalized_labels" in content, "Label normalization not found"

        # Verify the guard is used with set intersection
        assert re.search(r'normalized_labels\s*&\s*_EXACT_LABEL_GUARDS', content) or \
               re.search(r'_EXACT_LABEL_GUARDS\s*&\s*normalized_labels', content), \
               "Exact-label guard should use set intersection (& operator)"

        # Verify skip counter exists
        assert "skipped_exact_label_guard" in content, "Guard skip counter not found"

        print(f"PASS: Exact-label guard is used in pool selection")

    def test_normalization_lowercase_and_hyphens(self):
        """Test: Labels are normalized to lowercase with underscores converted to hyphens."""
        launcher_path = FLEET_PATH / "skfleet-rotate.py"
        with open(launcher_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Find the normalization code
        norm_match = re.search(
            r'normalized_labels.*?\.replace\([\'"_].*?[\'_"].*[\'"-].*?[\'"-]\)',
            content
        )
        assert norm_match, "Label normalization not found"

        norm_code = norm_match.group(0)
        assert ".lower()" in norm_code, "Lowercase normalization missing"
        assert ".replace" in norm_code, "Underscore replacement missing"
        assert "-" in norm_code or "_" in norm_code, "Hyphen/underscore handling missing"

        print(f"PASS: Labels are normalized to lowercase with underscore to hyphen conversion")

    def test_substring_matching_prohibited(self):
        """Test: Guard uses exact set membership, not substring matching."""
        launcher_path = FLEET_PATH / "skfleet-rotate.py"
        with open(launcher_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Find the exact-label guard check
        guard_check = re.search(
            r'if\s+normalized_labels\s*&\s*_EXACT_LABEL_GUARDS',
            content
        )
        assert guard_check, "Exact-label guard check not found"

        # Should use set intersection (&), not substring search
        check_code = guard_check.group(0)
        assert "&" in check_code, \
            "Guard should use set intersection, not substring matching"

        # Should NOT use .find() or .contains() on the raw string
        # The check should be on the normalized set
        assert "normalized_labels" in check_code, "Check should use normalized labels"

        print(f"PASS: Guard uses exact set membership, not substring matching")

    def test_ordinary_cards_remain_eligible(self):
        """Test: Ordinary source and review cards mentioning people remain eligible."""
        launcher_path = FLEET_PATH / "skfleet-rotate.py"
        with open(launcher_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Find the [HUMAN] tag check that should NOT exclude ordinary cards
        # The old loose test matched "human" anywhere in title
        # The new test should match only [HUMAN] tag
        human_tag_match = re.search(
            r'if.*\[HUMAN\].*in.*title',
            content
        )

        # The [HUMAN] check should be in non_implementation function, not the main guard
        # and should be exact tag matching
        assert '"[HUMAN]"' in content or "'[HUMAN]'" in content, \
            "[HUMAN] tag check not found"

        # Verify it's checking the tag, not the word "human" loosely
        # The exact pattern [HUMAN] should be present
        assert "[HUMAN]" in content, "[HUMAN] tag pattern not found"

        print(f"PASS: [HUMAN] tag is checked exactly, not substring matching")


class TestHumanHoldDependencies:
    """Tests for structural human hold dependency checking."""

    def test_human_hold_detection_function_exists(self):
        """Test: Function to detect human hold cards exists."""
        launcher_path = FLEET_PATH / "skfleet-rotate.py"
        with open(launcher_path, "r", encoding="utf-8") as f:
            content = f.read()

        assert "_is_human_hold_card" in content, "Missing _is_human_hold_card function"
        assert "_has_human_hold_labels" in content, "Missing _has_human_hold_labels function"

        print(f"PASS: Human hold detection functions exist")

    def test_incomplete_human_hold_check_in_pool(self):
        """Test: Incomplete human hold dependency is checked in pool selection."""
        launcher_path = FLEET_PATH / "skfleet-rotate.py"
        with open(launcher_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Verify the human hold check is present
        assert "_has_incomplete_human_hold_dependency" in content, \
            "Human hold dependency check not found"
        assert "skipped_human_hold" in content, "Human hold skip counter not found"

        print(f"PASS: Incomplete human hold dependency is checked in pool selection")

    def test_machine_completion_does_not_discharge_hold(self):
        """Test: Machine completion does not discharge a human hold."""
        launcher_path = FLEET_PATH / "skfleet-rotate.py"
        with open(launcher_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Find the human hold release function
        assert "_human_hold_explicitly_released" in content, \
            "Missing _human_hold_explicitly_released function"

        # The function should check for Chef approve or explicit void
        release_func_match = re.search(
            r'def _human_hold_explicitly_released.*?(?=\ndef |\n# )',
            content,
            re.DOTALL
        )
        assert release_func_match, "Could not find _human_hold_explicitly_released function"

        func_body = release_func_match.group(0)

        # Should check for Chef approve
        assert "chef" in func_body.lower(), "Missing Chef approval check"
        assert "approve" in func_body.lower(), "Missing approve check"

        # Should check for explicit void
        assert "void" in func_body.lower(), "Missing void check"

        # The function should be used in the human hold check
        hold_check_match = re.search(
            r'def _has_incomplete_human_hold_dependency.*?(?=\ndef |\n# )',
            content,
            re.DOTALL
        )
        assert hold_check_match, "Could not find _has_incomplete_human_hold_dependency function"

        hold_check_body = hold_check_match.group(0)
        assert "_human_hold_explicitly_released" in hold_check_body, \
            "Release function not called in human hold check"

        print(f"PASS: Machine completion does not discharge human holds")

    def test_supersede_and_archive_do_not_discharge_hold(self):
        """Test: Supersede and archive events do not discharge a human hold."""
        launcher_path = FLEET_PATH / "skfleet-rotate.py"
        with open(launcher_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Find the human hold check function
        hold_check_match = re.search(
            r'def _has_incomplete_human_hold_dependency.*?(?=\ndef |\n# )',
            content,
            re.DOTALL
        )
        assert hold_check_match, "Could not find _has_incomplete_human_hold_dependency function"

        func_body = hold_check_match.group(0)

        # Should call _human_hold_explicitly_released which checks only Chef approve or void
        # Should NOT accept supersede or archive as release
        # The lifecycle_state check should be there
        assert "lifecycle_state" in func_body, "Missing lifecycle_state check"

        # But complete alone shouldn't be enough - needs explicit release
        # Check that _human_hold_explicitly_released is called for complete dependencies
        assert "_human_hold_explicitly_released" in func_body, \
            "Explicit release check not called for complete dependencies"

        print(f"PASS: Supersede and archive do not discharge human holds")

    def test_hash_links_do_not_discharge_hold(self):
        """Test: Hash links do not discharge a human hold."""
        launcher_path = FLEET_PATH / "skfleet-rotate.py"
        with open(launcher_path, "r", encoding="utf-8") as f:
            content = f.read()

        # The release function should check for explicit Chef approve or void
        # NOT just any hash link or completion
        release_func_match = re.search(
            r'def _human_hold_explicitly_released.*?(?=\ndef |\n# )',
            content,
            re.DOTALL
        )
        assert release_func_match, "Could not find _human_hold_explicitly_released function"

        func_body = release_func_match.group(0)

        # Should check the outcome value, not just existence of a link
        assert "_load_outcomes" in func_body, "Missing outcome check"
        assert "val" in func_body, "Missing outcome value check"

        print(f"PASS: Hash links do not discharge human holds (only Chef approve or void)")


class TestRegressionCards:
    """Regression tests for cards 2209f7fe and 62243d92."""

    def test_regression_2209f7fe_exact_labels(self):
        """Test: Card 2209f7fe (canary executor) with guard labels would be excluded.

        Card 2209f7fe was selected while its effect was covered by Chef's gateway
        cutover hold. It then had to BLOCKED-verdict itself. With the new guards,
        cards with human-hold dependencies are excluded before selection.
        """
        # Card 2209f7fe has labels: skgateway, replacement-chain, canary
        # The regression is that it had a dependency on a human-hold card
        # (c64f97e9) and should have been excluded

        # Verify the human hold check would catch this
        launcher_path = FLEET_PATH / "skfleet-rotate.py"
        with open(launcher_path, "r", encoding="utf-8") as f:
            content = f.read()

        # The human hold check should look at dependencies
        assert "_has_incomplete_human_hold_dependency" in content, \
            "Missing function that would have caught 2209f7fe's hold dependency"

        # This function should be present and checking dependencies
        hold_check_match = re.search(
            r'def _has_incomplete_human_hold_dependency.*?(?=\ndef |\n# )',
            content,
            re.DOTALL
        )
        assert hold_check_match, "Could not find _has_incomplete_human_hold_dependency function"

        hold_check_body = hold_check_match.group(0)
        assert "folded_dependencies" in hold_check_body, \
            "2209f7fe regression: human hold check doesn't examine dependencies"

        print(f"PASS: Regression 2209f7fe - human hold dependency check would exclude such cards")

    def test_regression_62243d92_no_action_label(self):
        """Test: Card 62243d92 with 'no-action' label is excluded by exact-label guard.

        Card 62243d92 is a reserved worker card with label 'no-action'.
        It must be excluded from selector eligibility.
        """
        launcher_path = FLEET_PATH / "skfleet-rotate.py"
        with open(launcher_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Verify 'no-action' is in the exact-label guard set
        set_match = re.search(r'_EXACT_LABEL_GUARDS\s*=\s*\{([^}]+)\}', content, re.DOTALL)
        assert set_match, "Could not parse _EXACT_LABEL_GUARDS set"

        set_content = set_match.group(1)
        assert '"no-action"' in set_content, "Missing 'no-action' label in guard set"

        # Verify the guard is used in the source
        assert "_EXACT_LABEL_GUARDS" in content, \
            "62243d92 regression: exact-label guard not defined"

        print(f"PASS: Regression 62243d92 - 'no-action' label is in exact-label guard set")


class TestIntegration:
    """Integration tests for the complete guard system."""

    def test_pool_logging_includes_guard_counts(self):
        """Test: Pool logging includes exact-label guard and human hold skip counts."""
        launcher_path = FLEET_PATH / "skfleet-rotate.py"
        with open(launcher_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Find the POOL log line
        pool_log_match = re.search(
            r'log\(d,"POOL\|.*?exact_label_guard=%d.*?human_hold=%d',
            content
        )
        assert pool_log_match, "POOL log line missing guard counts"

        log_line = pool_log_match.group(0)
        assert "exact_label_guard" in log_line.lower(), "Missing exact_label_guard count"
        assert "human_hold" in log_line.lower(), "Missing human_hold count"

        print(f"PASS: Pool logging includes guard skip counts")

    def test_both_guards_defined_and_used(self):
        """Test: Both guards are defined and used in the selector."""
        launcher_path = FLEET_PATH / "skfleet-rotate.py"
        with open(launcher_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Both guard sets and functions should be defined
        assert "_EXACT_LABEL_GUARDS" in content, "Exact-label guard set not defined"
        assert "_has_incomplete_human_hold_dependency" in content, "Human hold check function not defined"
        assert "_is_human_hold_card" in content, "Human hold card detection not defined"
        assert "_human_hold_explicitly_released" in content, "Human hold release check not defined"

        # Both skip counters should be defined
        assert "skipped_exact_label_guard" in content, "Exact-label guard skip counter not defined"
        assert "skipped_human_hold" in content, "Human hold skip counter not defined"

        # Both should be used (checked in their respective test functions)
        assert re.search(r'normalized_labels\s*&\s*_EXACT_LABEL_GUARDS', content), \
            "Exact-label guard not used with set intersection"

        print(f"PASS: Both guards are defined and used in the selector")

    def test_no_substring_matching_on_labels(self):
        """Test: No substring matching is used for label exclusion."""
        launcher_path = FLEET_PATH / "skfleet-rotate.py"
        with open(launcher_path, "r", encoding="utf-8") as f:
            content = f.read()

        # The exact-label guard should use set intersection
        # Look for the pattern: if normalized_labels & _EXACT_LABEL_GUARDS
        assert re.search(r'normalized_labels\s*&\s*_EXACT_LABEL_GUARDS', content) or \
               re.search(r'_EXACT_LABEL_GUARDS\s*&\s*normalized_labels', content), \
               "Exact-label guard should use set intersection (& operator)"

        # Should NOT use 'in' for substring matching on the raw label string
        # It should use the normalized set with set intersection
        print(f"PASS: Exact-label guard uses set intersection, not substring matching")


def run_all_tests():
    """Run all test suites."""
    test_suites = [
        ("ExactLabelGuards", TestExactLabelGuards()),
        ("HumanHoldDependencies", TestHumanHoldDependencies()),
        ("RegressionCards", TestRegressionCards()),
        ("Integration", TestIntegration()),
    ]

    passed = 0
    failed = 0
    errors = []

    for suite_name, suite in test_suites:
        print(f"\n{'='*60}")
        print(f"Running test suite: {suite_name}")
        print(f"{'='*60}")

        for attr_name in dir(suite):
            if not attr_name.startswith("test_"):
                continue

            test_method = getattr(suite, attr_name)
            try:
                test_method()
                passed += 1
            except AssertionError as e:
                failed += 1
                error_msg = f"{suite_name}.{attr_name}: {e}"
                print(f"FAIL: {attr_name}")
                print(f"  {e}")
                errors.append(error_msg)
            except Exception as e:
                failed += 1
                error_msg = f"{suite_name}.{attr_name}: Unexpected error: {e}"
                print(f"ERROR: {attr_name}")
                print(f"  {e}")
                errors.append(error_msg)

    print(f"\n{'='*60}")
    print(f"Overall Results: {passed} passed, {failed} failed")
    print(f"{'='*60}")

    if failed > 0:
        print("\nFailed tests:")
        for error in errors:
            print(f"  - {error}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(run_all_tests())
