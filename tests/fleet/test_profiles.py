"""Tests for the Profile kind model (epic 3bbf39ea, card de9cf1d0).

The profile layer decides what a node is allowed to have installed, so its
validator is the last thing standing between a typo and a fleet-wide
uninstall. Every contradiction must raise, never resolve silently.
"""

from __future__ import annotations

import pytest

from skcapstone.fleet import profiles


def _minimal(**overrides) -> dict:
    """The smallest spec that validates: both consequential fields stated."""
    spec = {"stateTier": "none", "capauthIdentityClass": "worker"}
    spec.update(overrides)
    return spec


# ------------------------------------------------------------- defaults ---


def test_defaults_are_conservative() -> None:
    spec = profiles.normalize_profile_spec(_minimal())
    assert spec["description"] == ""
    assert spec["packages"] == {"required": [], "allowed": [], "mustNot": []}
    assert spec["units"] == {"required": [], "allowed": [], "mustNot": []}
    assert spec["unitsIgnore"] == []
    assert spec["syncFolders"] == []
    assert spec["deleted"] is False
    # An empty profile asserts nothing; it must not read as "remove everything".
    assert spec["stateTier"] == "none"
    assert spec["capauthIdentityClass"] == "worker"


def test_explicit_fields_survive() -> None:
    spec = profiles.normalize_profile_spec(
        {
            "description": "GPU worker: serve inference, hold zero sovereign state.",
            "packages": {
                "required": ["skcapstone"],
                "allowed": ["skcapstone", "capauth"],
                "mustNot": ["skmemory"],
            },
            "units": {
                "required": ["skai-beellama.service"],
                "allowed": ["skai-beellama.service", "comfyui.service"],
                "mustNot": ["skchat-daemon.service"],
            },
            "unitsIgnore": ["gpg-agent*.socket", "dirmngr.socket"],
            "stateTier": "control-bus",
            "capauthIdentityClass": "worker",
            "syncFolders": ["skfleet-control"],
        }
    )
    assert spec["packages"]["required"] == ["skcapstone"]
    assert spec["packages"]["mustNot"] == ["skmemory"]
    assert spec["units"]["allowed"] == ["comfyui.service", "skai-beellama.service"]
    assert spec["unitsIgnore"] == ["dirmngr.socket", "gpg-agent*.socket"]
    assert spec["stateTier"] == "control-bus"
    assert spec["syncFolders"] == ["skfleet-control"]


def test_name_lists_are_sorted_and_deduplicated() -> None:
    spec = profiles.normalize_profile_spec(
        _minimal(units={"allowed": ["b.service", "a.service", "b.service", " a.service "]})
    )
    assert spec["units"]["allowed"] == ["a.service", "b.service"]


# -------------------------------------------------------- required axes ---


def test_empty_spec_names_the_missing_field() -> None:
    with pytest.raises(profiles.ProfileSpecError) as exc:
        profiles.normalize_profile_spec({})
    assert "stateTier" in str(exc.value)


def test_missing_identity_class_names_the_missing_field() -> None:
    with pytest.raises(profiles.ProfileSpecError) as exc:
        profiles.normalize_profile_spec({"stateTier": "none"})
    assert "capauthIdentityClass" in str(exc.value)


@pytest.mark.parametrize("tier", sorted(profiles.STATE_TIERS))
def test_every_known_state_tier_validates(tier: str) -> None:
    spec = profiles.normalize_profile_spec(_minimal(stateTier=tier))
    assert spec["stateTier"] == tier


@pytest.mark.parametrize("klass", sorted(profiles.IDENTITY_CLASSES))
def test_every_known_identity_class_validates(klass: str) -> None:
    spec = profiles.normalize_profile_spec(_minimal(capauthIdentityClass=klass))
    assert spec["capauthIdentityClass"] == klass


def test_unknown_state_tier_raises() -> None:
    with pytest.raises(profiles.ProfileSpecError, match="unknown stateTier"):
        profiles.normalize_profile_spec(_minimal(stateTier="half-replica"))


def test_unknown_identity_class_raises() -> None:
    with pytest.raises(profiles.ProfileSpecError, match="unknown capauthIdentityClass"):
        profiles.normalize_profile_spec(_minimal(capauthIdentityClass="root"))


def test_the_two_axes_are_independent() -> None:
    """A builder-standby holds a full replica while running almost nothing,
    and a worker runs a lot while holding nothing. Neither field constrains
    the other, so every combination must validate."""
    for tier in sorted(profiles.STATE_TIERS):
        for klass in sorted(profiles.IDENTITY_CLASSES):
            spec = profiles.normalize_profile_spec(
                _minimal(stateTier=tier, capauthIdentityClass=klass)
            )
            assert (spec["stateTier"], spec["capauthIdentityClass"]) == (tier, klass)


# ---------------------------------------------------------- contradiction ---


def test_unit_in_both_allowed_and_must_not_raises() -> None:
    with pytest.raises(profiles.ProfileSpecError, match="both 'allowed' and 'mustNot'"):
        profiles.normalize_profile_spec(
            _minimal(
                units={
                    "allowed": ["skchat-daemon.service"],
                    "mustNot": ["skchat-daemon.service"],
                }
            )
        )


def test_package_in_both_allowed_and_must_not_raises() -> None:
    with pytest.raises(profiles.ProfileSpecError, match="both 'allowed' and 'mustNot'"):
        profiles.normalize_profile_spec(
            _minimal(packages={"allowed": ["skmemory"], "mustNot": ["skmemory"]})
        )


def test_required_not_allowed_raises() -> None:
    with pytest.raises(profiles.ProfileSpecError, match="not in 'allowed'"):
        profiles.normalize_profile_spec(
            _minimal(units={"required": ["sknoded.service"], "allowed": []})
        )


def test_required_is_not_silently_widened_into_allowed() -> None:
    """The manifest must say what it means: requiring a name never adds it
    to allowed behind the author's back."""
    with pytest.raises(profiles.ProfileSpecError):
        profiles.normalize_profile_spec(
            _minimal(packages={"required": ["capauth"], "allowed": ["skcapstone"]})
        )


# ------------------------------------------------------------- malformed ---


def test_non_dict_spec_raises() -> None:
    with pytest.raises(profiles.ProfileSpecError, match="must be a dict"):
        profiles.normalize_profile_spec(["stateTier"])  # type: ignore[arg-type]


def test_name_list_block_must_be_a_dict() -> None:
    with pytest.raises(profiles.ProfileSpecError, match="must be a dict of name lists"):
        profiles.normalize_profile_spec(_minimal(units=["a.service"]))


def test_unknown_key_in_name_list_block_raises() -> None:
    with pytest.raises(profiles.ProfileSpecError, match="unknown keys"):
        profiles.normalize_profile_spec(_minimal(units={"forbidden": ["a.service"]}))


def test_name_list_must_be_a_list() -> None:
    with pytest.raises(profiles.ProfileSpecError, match="must be a list"):
        profiles.normalize_profile_spec(_minimal(units={"allowed": "a.service"}))


@pytest.mark.parametrize("bad", ["", "   ", None, 7, ["nested"]])
def test_empty_or_non_string_names_raise(bad: object) -> None:
    with pytest.raises(profiles.ProfileSpecError, match="non-empty"):
        profiles.normalize_profile_spec(_minimal(packages={"allowed": [bad]}))


def test_sync_folders_must_be_strings() -> None:
    with pytest.raises(profiles.ProfileSpecError, match="non-empty strings"):
        profiles.normalize_profile_spec(_minimal(syncFolders=[{"id": "skfleet-control"}]))


def test_units_ignore_must_be_a_list() -> None:
    with pytest.raises(profiles.ProfileSpecError, match="must be a list"):
        profiles.normalize_profile_spec(_minimal(unitsIgnore="gpg-agent*.socket"))


def test_description_must_be_a_string() -> None:
    with pytest.raises(profiles.ProfileSpecError, match="description must be a string"):
        profiles.normalize_profile_spec(_minimal(description=["worker"]))


def test_deleted_tombstone_is_coerced_to_bool() -> None:
    spec = profiles.normalize_profile_spec(_minimal(deleted=1))
    assert spec["deleted"] is True
