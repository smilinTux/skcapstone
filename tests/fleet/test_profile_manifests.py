"""Fleet-wide invariants over the shipped profile manifests (card 1a90486c).

Reads the REAL manifest directory, never a fixture copy, so these assertions
guard the files that actually ship. A fifth manifest cannot appear, and an
existing one cannot drift, without failing here first.

Manifest home is `deploy/fleet-objects/profile/` per decision card c5ad2471,
alongside the other real loadable fleet objects. `docs/fleet/profiles.md` is
the schema reference, not a second home for the manifests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcapstone.fleet.profiles import (
    IDENTITY_CLASSES,
    STATE_TIERS,
    ProfileSpecError,
    normalize_profile_spec,
)

MANIFEST_DIR = Path(__file__).resolve().parents[2] / "deploy" / "fleet-objects" / "profile"

#: Naming a fifth role has to be a deliberate edit here, not a silent addition.
EXPECTED_ROLES = {"control", "builder-standby", "worker-gpu", "observer"}


def _manifest_paths() -> list[Path]:
    return sorted(MANIFEST_DIR.glob("*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_manifest_directory_exists_and_is_not_empty() -> None:
    assert MANIFEST_DIR.is_dir(), f"no manifest directory at {MANIFEST_DIR}"
    assert _manifest_paths(), "no manifests shipped"


def test_exactly_the_four_expected_roles_ship() -> None:
    assert {p.stem for p in _manifest_paths()} == EXPECTED_ROLES


@pytest.mark.parametrize("path", _manifest_paths(), ids=lambda p: p.stem)
def test_every_manifest_parses_and_validates(path: Path) -> None:
    doc = _load(path)
    assert doc["kind"] == "profile"
    try:
        normalize_profile_spec(doc["spec"])
    except ProfileSpecError as exc:
        pytest.fail(f"{path.name} fails validation: {exc}")


@pytest.mark.parametrize("path", _manifest_paths(), ids=lambda p: p.stem)
def test_file_name_matches_the_role_name(path: Path) -> None:
    """The object's name IS its role, and `skfleet get profiles` binds nodes
    to it by that name, so a mismatch would bind to nothing."""
    assert _load(path)["name"] == path.stem


@pytest.mark.parametrize("path", _manifest_paths(), ids=lambda p: p.stem)
def test_no_name_is_both_allowed_and_forbidden(path: Path) -> None:
    spec = normalize_profile_spec(_load(path)["spec"])
    for field in ("units", "packages"):
        block = spec[field]
        overlap = set(block["allowed"]) & set(block["mustNot"])
        assert not overlap, f"{path.name} {field}: {sorted(overlap)} in both allowed and mustNot"


@pytest.mark.parametrize("path", _manifest_paths(), ids=lambda p: p.stem)
def test_every_manifest_forbids_something(path: Path) -> None:
    """A profile with an empty mustNot asserts nothing with teeth: mustNot is
    the only category graded as an error by the drift report."""
    spec = normalize_profile_spec(_load(path)["spec"])
    assert (
        spec["units"]["mustNot"] or spec["packages"]["mustNot"]
    ), f"{path.name} forbids nothing, so it can never produce an error-grade finding"


@pytest.mark.parametrize("path", _manifest_paths(), ids=lambda p: p.stem)
def test_enum_values_come_from_the_module_frozensets(path: Path) -> None:
    spec = normalize_profile_spec(_load(path)["spec"])
    assert spec["stateTier"] in STATE_TIERS
    assert spec["capauthIdentityClass"] in IDENTITY_CLASSES


def test_exactly_one_manifest_holds_the_operator_identity_class() -> None:
    """There is one control seat. That is a deliberate SPOF, mitigated by a
    warm replica and a drilled promotion runbook, not by a second operator."""
    operators = {
        p.stem
        for p in _manifest_paths()
        if normalize_profile_spec(_load(p)["spec"])["capauthIdentityClass"] == "operator"
    }
    assert operators == {"control"}


def test_full_replica_is_exactly_control_and_builder_standby() -> None:
    """NOTE, deliberate deviation from the card text.

    Card 1a90486c asks for "exactly one manifest declares stateTier
    full-replica (control)". That contradicts the role model the epic is
    built on: builder-standby IS the warm state replica and the promotion
    target. Two copies of the STATE, one copy of each running SERVICE. If
    .41 were not full-replica there would be nothing to promote.

    So the invariant worth pinning is which nodes hold state, not how few.
    """
    tiers = {
        p.stem: normalize_profile_spec(_load(p)["spec"])["stateTier"] for p in _manifest_paths()
    }
    assert {name for name, tier in tiers.items() if tier == "full-replica"} == {
        "control",
        "builder-standby",
    }
    # worker-gpu is control-bus, NOT none: it joins the scoped skfleet-control
    # folder, so it does hold the fleet store. `none` is reserved for the node
    # that holds no SK state at all, which is the observer.
    assert tiers["worker-gpu"] == "control-bus"
    assert tiers["observer"] == "none"


def test_the_worker_holds_no_state_and_joins_no_sovereign_folder() -> None:
    """The load-bearing property of the whole epic."""
    spec = normalize_profile_spec(_load(MANIFEST_DIR / "worker-gpu.json")["spec"])
    assert spec["stateTier"] == "control-bus"
    assert spec["capauthIdentityClass"] == "worker"
    assert spec["syncFolders"] == ["skfleet-control"]
    assert "skcapstone-sync" not in spec["syncFolders"]
    for package in ("skmemory", "skcoord", "skseed"):
        assert package in spec["packages"]["mustNot"], f"worker must forbid {package}"


def test_the_observer_can_never_be_installed_into() -> None:
    spec = normalize_profile_spec(_load(MANIFEST_DIR / "observer.json")["spec"])
    assert spec["units"]["required"] == []
    assert spec["packages"]["required"] == []
    assert spec["stateTier"] == "none"
    assert spec["syncFolders"] == []
    assert "no installation ever targets an observer" in spec["description"].lower()


def test_the_standby_may_not_run_the_control_plane_loops() -> None:
    """A travelling laptop acting as a second control seat is the
    single-writer violation the store's ownership guard exists to prevent."""
    spec = normalize_profile_spec(_load(MANIFEST_DIR / "builder-standby.json")["spec"])
    for unit in ("skgateway.service", "skoperator.timer"):
        assert unit in spec["units"]["mustNot"], f"standby must forbid {unit}"


def test_only_the_worker_may_run_model_serving_units() -> None:
    for path in _manifest_paths():
        spec = normalize_profile_spec(_load(path)["spec"])
        allowed = set(spec["units"]["allowed"])
        if path.stem == "worker-gpu":
            assert "skai-beellama.service" in allowed
        else:
            assert "skai-beellama.service" not in allowed, f"{path.stem} may not serve models"


def test_manifests_match_their_generator() -> None:
    """The manifests are generated from real node inventories. Hand-editing
    one would make it drift from the box it describes, so regeneration must
    be a no-op."""
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(repo / "scripts" / "fleet" / "gen-profile-manifests.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=repo,
    )
    assert result.returncode == 0, f"manifests differ from the generator:\n{result.stdout}"


def test_the_tier_agrees_with_the_folders_the_role_actually_joins() -> None:
    """A tier is a claim about how much state a node holds, and syncFolders is
    the mechanism that makes the claim true or false. They must not disagree.

    worker-gpu shipped as `none` while joining `skfleet-control`, which is
    self-contradictory: it does hold the fleet store. This pins the rule so
    the two cannot drift apart again.
    """
    for path in _manifest_paths():
        spec = normalize_profile_spec(_load(path)["spec"])
        tier, folders = spec["stateTier"], spec["syncFolders"]
        if tier == "none":
            assert not folders, (
                f"{path.stem} claims tier 'none' but joins {folders}; a node that "
                "joins a state folder is not holding no state"
            )
        else:
            assert folders, (
                f"{path.stem} claims tier {tier!r} but joins no folder, so nothing "
                "delivers the state the tier promises"
            )
