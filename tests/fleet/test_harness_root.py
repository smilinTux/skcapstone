"""The validator and the sealed worker must name the same harness files."""

import sys
from pathlib import Path

from skcapstone.fleet import production_test_plan as plan


def test_harness_root_prefers_the_qualified_prefix(tmp_path):
    site = tmp_path / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    harness = site / "site-packages" / "skcapstone" / "fleet"
    harness.mkdir(parents=True)
    (harness / "production_pytest_selection.py").write_text("# harness\n")
    assert plan.harness_root(tmp_path, Path("/fallback")) == harness


def test_harness_root_falls_back_without_a_prefix(tmp_path):
    assert plan.harness_root(tmp_path, Path("/fallback")) == Path("/fallback")
