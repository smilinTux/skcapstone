"""Tests for backend registry resolution."""

from skcapstone.fleet.install_backends import UNSUPPORTED, resolve, tier_of


def test_resolve_maps_units_to_owning_backend():
    assert resolve("skgateway.service", "unit") == "core"
    assert resolve("skchat-webui@lumina.service", "unit") == "skchat"
    assert resolve("skwhisper@lumina.service", "unit") == "agent"
    assert resolve("capauth-authz.service", "unit") == "capauth-authz"


def test_packages_kind_always_resolves_to_packages_backend():
    assert resolve("capauth", "package") == "packages"


def test_unknown_required_unit_is_unsupported_not_silent():
    assert resolve("totally-made-up.service", "unit") == UNSUPPORTED


def test_cloud9_daemon_is_unsupported_no_installer_owns_it():
    # cloud9 ships only a unit template; skmemory's installer knows nothing
    # about it, so it must surface as needs_manual rather than being routed
    # through the wrong installer.
    assert resolve("cloud9-daemon@lumina.service", "unit") == UNSUPPORTED


def test_tier_orders_packages_before_skchat_plane():
    assert tier_of("packages") < tier_of("skchat")
    assert tier_of("capauth-authz") < tier_of("skcomms") < tier_of("core")
