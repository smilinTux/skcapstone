"""Controlled branch-protection probe for card d3df978d."""

import deliberately_missing_d3df978d_probe_module  # noqa: F401


def test_controlled_branch_gate_probe() -> None:
    """Fail only on the unmerged proof branch."""
    assert False, "controlled d3df978d branch-protection proof"
