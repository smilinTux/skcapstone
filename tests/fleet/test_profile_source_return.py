"""Returning to an already qualified source requires a fresh exact test run."""

import copy
import json

import pytest

from skcapstone.fleet import production_test_plan as plan
from skcapstone.fleet import production_test_profile as profile
from tests.fleet.test_production_test_profile import qualified  # noqa: F401
from tests.fleet.test_production_tests import setup  # noqa: F401


def candidate_profile(home, core, path, change=None):
    old, digest = profile.read_profile(home, core["id"])
    current = copy.deepcopy(old)
    current["source_sha256"] = "1" * 64
    if change:
        current[change] = {"pytest_all": True} if change == "recipe" else "2" * 64
    directory = path.parent / core["id"]
    plan.private_dir(directory, create=True)
    plan.write_once(
        directory / (digest + ".json"),
        {
            "schema": "skfleet.test-profile-successor/v2",
            "predecessor_sha256": digest,
            "source_card_sha256": "3" * 64,
            "profile": current,
        },
    )
    return old, current, digest


def test_known_base_requires_requalification_not_reuse(qualified):  # noqa: F811
    home, core, policy, _, path = qualified
    old, current, _ = candidate_profile(home, core, path)
    with pytest.raises(profile.ProfileRequalificationRequired):
        profile.preflight(home, core, ["source-only"], policy, source_sha256=old["source_sha256"])
    assert profile.fingerprint_only_stale(
        current, profile.contract(core), policy, source_sha256=old["source_sha256"], home=home
    )
    assert profile.read_profile(home, core["id"])[0] == current


@pytest.mark.parametrize("change", ["recipe", "criteria_sha256", "unknown-source"])
def test_unknown_or_changed_historical_contract_stays_refused(qualified, change):  # noqa: F811
    home, core, policy, _, path = qualified
    old, _, _ = candidate_profile(home, core, path, None if change == "unknown-source" else change)
    target = "4" * 64 if change == "unknown-source" else old["source_sha256"]
    with pytest.raises(plan.TestEvidenceError) as caught:
        profile.preflight(home, core, ["source-only"], policy, source_sha256=target)
    assert not isinstance(caught.value, profile.ProfileRequalificationRequired)


def test_return_plan_seals_history_digest_and_requires_fresh_receipt(setup):  # noqa: F811
    s = setup
    s.plan_path.unlink()  # Remove only the fixture's unrelated default plan.
    core = {
        "id": s.binding["source_card"],
        "meta": {"repository": "https://example.invalid/repo"},
        "acceptance_criteria": ["Run the source parser checks."],
    }
    source_sha = plan.workspace_source_fingerprint(core["meta"]["repository"], s.workspace)
    path = profile.qualify_profile(
        s.home,
        core,
        s.policy,
        {"pytest": {"tests/test_parser.py": 1}, "compile": [], "lint": [], "changelog": False},
        "operator",
        "b" * 64,
        source_sha256=source_sha,
    )
    old, current, old_digest = candidate_profile(s.home, core, path)
    _, predecessor = profile.read_profile(s.home, core["id"])
    output = plan.seal_plan(
        s.home,
        s.binding,
        s.workspace,
        s.policy,
        current["qualified_by"],
        current["qualification_sha256"],
        profile=current,
        requalification=True,
        profile_predecessor_sha256=predecessor,
    )
    sealed, _, digest = plan.load_plan(s.home, s.binding)
    assert sealed["source_profile_sha256"] == old_digest
    assert not (plan.run_directory(s.home, digest) / "receipt.json").exists()
    assert profile.read_profile(s.home, core["id"])[0] == current
    corrupted = json.loads(output.read_text())
    corrupted["source_profile_sha256"] = "9" * 64
    with pytest.raises(plan.TestEvidenceError, match="source profile"):
        plan._validate_plan(s.home, s.binding, corrupted, successor=False)
