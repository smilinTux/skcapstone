"""Tests for installer.plan() function."""
from skcapstone.fleet.installer import plan
from skcapstone.fleet.profile_doctor import DriftReport


def _drift(**kw):
    return DriftReport(**kw)


def test_plan_orders_by_tier_then_name_and_ignores_forbidden():
    drift = _drift(
        missing_required_packages=["capauth"],
        missing_required_units=["skgateway.service", "skchat-webui@lumina.service"],
        forbidden_units=["comfyui.service"],       # must NOT produce a step
        unexpected_units=["random.service"],       # must NOT produce a step
    )
    p = plan(drift)
    names = [(s.tier, s.name) for s in p.steps]
    # packages(tier1) -> core skgateway(tier4) -> skchat webui(tier5)
    assert names == [(1, "capauth"), (4, "skgateway.service"), (5, "skchat-webui@lumina.service")]


def test_plan_only_filters_to_named_items():
    drift = _drift(missing_required_units=["skgateway.service", "sknoded.service"])
    p = plan(drift, only=["sknoded.service"])
    assert [s.name for s in p.steps] == ["sknoded.service"]


def test_plan_empty_when_nothing_missing():
    assert plan(_drift()).steps == []
