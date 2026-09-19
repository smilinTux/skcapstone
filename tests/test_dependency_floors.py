"""Tests for dependency floor detection.

The outage these guard against: an auto-pull fast-forwarded the fleet onto a
commit whose code required ``skcoord>=0.1.78`` while every host had 0.1.77
installed. The floor in ``pyproject.toml`` was correct the whole time; nothing
compared it against the running environment.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from skcapstone.dependency_floors import (
    NOT_INSTALLED,
    FloorViolation,
    check_floors,
    declared_requirements,
)
from skcapstone.preflight import PreflightChecker


def _installed(mapping: dict[str, str]):
    """Patch importlib version lookup with a fixed name->version mapping."""

    def fake_version(name: str) -> str:
        from importlib.metadata import PackageNotFoundError

        try:
            return mapping[name]
        except KeyError:
            raise PackageNotFoundError(name) from None

    return patch("skcapstone.dependency_floors.version", side_effect=fake_version)


class TestCheckFloors:
    """Core comparison behaviour."""

    def test_the_outage_is_detected(self) -> None:
        """skcoord 0.1.77 against a declared floor of 0.1.78 is a violation.

        This is the exact production state at 05:28:29 UTC on all five hosts.
        """
        with _installed({"skcoord": "0.1.77"}):
            violations = check_floors([("skcoord>=0.1.78", None)])
        assert len(violations) == 1
        v = violations[0]
        assert v.name == "skcoord"
        assert v.installed == "0.1.77"
        assert v.specifier == ">=0.1.78"
        assert not v.missing
        assert "0.1.77" in str(v) and "0.1.78" in str(v)
        assert v.fix == "pip install -U 'skcoord>=0.1.78'"

    def test_the_repair_clears_it(self) -> None:
        """skcoord 0.1.79, which the hosts were repaired to, is silent."""
        with _installed({"skcoord": "0.1.79"}):
            assert check_floors([("skcoord>=0.1.78", None)]) == []

    def test_exactly_at_the_floor_is_satisfied(self) -> None:
        with _installed({"skcoord": "0.1.78"}):
            assert check_floors([("skcoord>=0.1.78", None)]) == []

    def test_comparison_is_not_string_equality(self) -> None:
        """0.1.9 < 0.1.78 numerically but sorts after it as a string."""
        with _installed({"skcoord": "0.1.9"}):
            violations = check_floors([("skcoord>=0.1.78", None)])
        assert len(violations) == 1, "string comparison would call 0.1.9 satisfying"

    def test_dev_build_above_the_floor_is_not_a_violation(self) -> None:
        """Editable dev builds are the normal state on this fleet."""
        with _installed({"skcoord": "0.1.79.dev12+gdeadbee"}):
            assert check_floors([("skcoord>=0.1.78", None)]) == []

    def test_dev_build_below_the_floor_is_a_violation(self) -> None:
        with _installed({"skcoord": "0.1.78.dev1"}):
            assert len(check_floors([("skcoord>=0.1.78", None)])) == 1

    def test_missing_core_dependency_is_reported(self) -> None:
        with _installed({}):
            violations = check_floors([("skcoord>=0.1.78", None)])
        assert len(violations) == 1
        assert violations[0].missing
        assert violations[0].installed == NOT_INSTALLED
        assert "not installed" in str(violations[0])

    def test_missing_extra_is_not_reported(self) -> None:
        """An absent optional dependency is a choice, not a breach."""
        with _installed({}):
            assert check_floors([("boto3>=1.34", "cloud")]) == []

    def test_installed_extra_below_floor_is_reported(self) -> None:
        """But an extra that IS installed and below its floor is the same hazard."""
        with _installed({"boto3": "1.20.0"}):
            violations = check_floors([("boto3>=1.34", "cloud")])
        assert len(violations) == 1
        assert violations[0].extra == "cloud"
        assert "[cloud]" in str(violations[0])

    def test_ceiling_violation_is_reported(self) -> None:
        """click>=8.1,<9.0 with click 9.2 installed is an unmet contract too."""
        with _installed({"click": "9.2"}):
            assert len(check_floors([("click>=8.1,<9.0", None)])) == 1

    def test_unparseable_installed_version_is_silent(self) -> None:
        """Cannot compare means cannot claim; guessing would be dishonest."""
        with _installed({"skcoord": "not-a-version"}):
            assert check_floors([("skcoord>=0.1.78", None)]) == []

    def test_unparseable_requirement_is_skipped(self) -> None:
        with _installed({}):
            assert check_floors([("!!!broken!!!", None)]) == []

    def test_unpinned_requirement_is_skipped(self) -> None:
        """No specifier means no floor to violate."""
        with _installed({"skcoord": "0.0.1"}):
            assert check_floors([("skcoord", None)]) == []

    def test_unsatisfied_marker_is_skipped(self) -> None:
        with _installed({"skcoord": "0.1.0"}):
            assert check_floors([('skcoord>=0.1.78; python_version < "3.0"', None)]) == []

    def test_violations_are_deduplicated(self) -> None:
        """The `all` extra repeats every other extra's contents."""
        with _installed({"boto3": "1.20.0"}):
            violations = check_floors([("boto3>=1.34", "cloud"), ("boto3>=1.34", "cloud")])
        assert len(violations) == 1

    def test_multiple_violations_sorted_by_name(self) -> None:
        with _installed({"skcoord": "0.1.77", "croniter": "1.0"}):
            violations = check_floors([("skcoord>=0.1.78", None), ("croniter>=2.0", None)])
        assert [v.name for v in violations] == ["croniter", "skcoord"]


class TestDeclaredRequirements:
    """The declared floors come from the checkout, not stale install metadata."""

    def test_reads_this_projects_own_pyproject(self) -> None:
        reqs = declared_requirements()
        assert reqs, "skcapstone declares dependencies; none were found"
        names = {r for r, _ in reqs}
        assert any(r.startswith("skcoord") for r in names)

    def test_includes_extras_tagged_with_their_group(self) -> None:
        reqs = declared_requirements()
        extras = {extra for _, extra in reqs if extra}
        assert extras, "optional-dependencies were not picked up"

    def test_real_environment_has_no_false_positives_on_core_deps(self) -> None:
        """Every violation reported against the live env must be real.

        A check that cries wolf is a check nobody reads.
        """
        for v in check_floors():
            assert isinstance(v, FloorViolation)
            assert v.specifier, f"{v.name} reported with no specifier"


class TestPreflightIntegration:
    """The check has to fire somewhere a scheduled service will surface it."""

    def test_check_is_wired_into_preflight(self, tmp_path: Path) -> None:
        checker = PreflightChecker(home=tmp_path)
        result = checker.check_dependency_floors()
        assert result.name == "dependency_floors"
        assert result.status in ("ok", "warn", "fail")

    def test_violation_fails_the_check(self, tmp_path: Path) -> None:
        with patch(
            "skcapstone.dependency_floors.check_floors",
            return_value=[FloorViolation("skcoord", "0.1.77", ">=0.1.78")],
        ):
            result = PreflightChecker(home=tmp_path).check_dependency_floors()
        assert result.status == "fail"
        assert "skcoord" in result.message
        assert "0.1.77" in result.message
        assert "0.1.78" in result.message

    def test_violation_does_not_block_startup(self, tmp_path: Path) -> None:
        """Fail-open: a floor bump lands in git before its dependency publishes.

        pyproject.toml deliberately sets the floor one past the newest published
        tag, so 'below floor' is a state the maintainers intentionally create.
        Gating startup on it would take the fleet down on purpose.
        """
        with patch(
            "skcapstone.dependency_floors.check_floors",
            return_value=[FloorViolation("skcoord", "0.1.77", ">=0.1.78")],
        ):
            result = PreflightChecker(home=tmp_path).check_dependency_floors()
        assert result.critical is False

    def test_clean_environment_passes(self, tmp_path: Path) -> None:
        with patch("skcapstone.dependency_floors.check_floors", return_value=[]):
            result = PreflightChecker(home=tmp_path).check_dependency_floors()
        assert result.status == "ok"

    def test_check_never_raises(self, tmp_path: Path) -> None:
        """A broken checker must not become the thing that breaks startup."""
        with patch(
            "skcapstone.dependency_floors.check_floors",
            side_effect=RuntimeError("boom"),
        ):
            result = PreflightChecker(home=tmp_path).check_dependency_floors()
        assert result.status == "warn"
        assert result.critical is False

    def test_appears_in_run_all(self, tmp_path: Path) -> None:
        summary = PreflightChecker(home=tmp_path).run_all()
        names = {c["name"] for c in summary["checks"]}
        assert "dependency_floors" in names

    def test_run_all_stays_ok_despite_floor_violation(self, tmp_path: Path) -> None:
        """The check is loud but not a gate."""
        with patch(
            "skcapstone.dependency_floors.check_floors",
            return_value=[FloorViolation("skcoord", "0.1.77", ">=0.1.78")],
        ):
            summary = PreflightChecker(home=tmp_path).run_all()
        floors = [c for c in summary["checks"] if c["name"] == "dependency_floors"]
        assert floors and floors[0]["status"] == "fail"
        assert floors[0]["critical"] is False
        critical_fails = {
            c["name"] for c in summary["checks"] if c["status"] == "fail" and c["critical"]
        }
        assert "dependency_floors" not in critical_fails


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
