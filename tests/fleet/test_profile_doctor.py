"""Tests for the pure drift diff (card ffcb1a3d).

Two properties carry the weight. The module must stay pure, because it is
the thing that decides whether a node is "wrong". And an unfinished manifest
must degrade to noise, never to a wall of error-grade findings, or nobody
will ship the first draft of a profile.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skcapstone.fleet import profile_doctor
from skcapstone.fleet.profiles import normalize_profile_spec


def _inventory(units: list[str], packages: list[str] | None = None) -> dict:
    return {
        "units": {"user": {u: "enabled" for u in units}},
        "packages": {p: "1.0" for p in (packages or [])},
        "collectedAt": "2026-08-15T00:00:00Z",
    }


def _profile(**spec) -> dict:
    base = {"stateTier": "none", "capauthIdentityClass": "worker"}
    base.update(spec)
    return normalize_profile_spec(base)


WORKER = _profile(
    units={
        "required": ["skai-beellama.service"],
        "allowed": ["skai-beellama.service", "comfyui.service"],
        "mustNot": ["skchat-daemon.service"],
    },
    packages={
        "required": ["skcapstone"],
        "allowed": ["skcapstone", "capauth"],
        "mustNot": ["skmemory"],
    },
    unitsIgnore=["gpg-agent*.socket", "dirmngr.socket"],
)


# ------------------------------------------------------------- clean ---


def test_a_clean_node_yields_six_empty_lists_and_ok() -> None:
    report = profile_doctor.diff(
        _inventory(["skai-beellama.service", "comfyui.service"], ["skcapstone", "capauth"]),
        WORKER,
    )
    assert report.as_dict() == {
        "missing_required_units": [],
        "forbidden_units": [],
        "unexpected_units": [],
        "missing_required_packages": [],
        "forbidden_packages": [],
        "unexpected_packages": [],
        "severity": "ok",
    }
    assert report.clean is True
    assert report.findings() == []


def test_a_node_may_omit_an_allowed_but_not_required_unit() -> None:
    report = profile_doctor.diff(
        _inventory(["skai-beellama.service"], ["skcapstone", "capauth"]), WORKER
    )
    assert report.clean is True


# ------------------------------------------------------- every category ---


def test_every_category_at_once() -> None:
    report = profile_doctor.diff(
        _inventory(
            ["skchat-daemon.service", "syncthing.service", "gpg-agent.socket"],
            ["skmemory", "pytest"],
        ),
        WORKER,
    )
    assert report.missing_required_units == ["skai-beellama.service"]
    assert report.forbidden_units == ["skchat-daemon.service"]
    assert report.unexpected_units == ["syncthing.service"]  # gpg-agent ignored
    assert report.missing_required_packages == ["skcapstone"]
    assert report.forbidden_packages == ["skmemory"]
    assert report.unexpected_packages == ["pytest"]
    assert report.severity == "error"


def test_findings_rows_are_graded_and_cover_everything() -> None:
    report = profile_doctor.diff(
        _inventory(["skchat-daemon.service", "syncthing.service"], ["skmemory"]), WORKER
    )
    rows = report.findings()
    grades = {grade for grade, _, _ in rows}
    assert grades == {"error", "warn", "info"}
    assert ("error", "forbidden_units", "skchat-daemon.service") in rows
    assert len(rows) == sum(len(v) for k, v in report.as_dict().items() if k != "severity")


# ------------------------------------------------------------ severity ---


def test_forbidden_alone_is_error() -> None:
    report = profile_doctor.diff(
        _inventory(["skai-beellama.service", "skchat-daemon.service"], ["skcapstone"]), WORKER
    )
    assert report.severity == "error"


def test_missing_required_alone_is_warn_not_error() -> None:
    """A node mid-install is not a node misbehaving."""
    report = profile_doctor.diff(_inventory([], ["skcapstone"]), WORKER)
    assert report.missing_required_units == ["skai-beellama.service"]
    assert report.severity == "warn"


def test_unexpected_alone_is_info_not_error() -> None:
    """Usually the manifest lagging reality. Grading it error would train
    everyone to ignore the whole report."""
    report = profile_doctor.diff(
        _inventory(["skai-beellama.service", "extra.service"], ["skcapstone"]), WORKER
    )
    assert report.unexpected_units == ["extra.service"]
    assert report.severity == "info"


def test_severity_takes_the_worst_finding() -> None:
    report = profile_doctor.diff(
        _inventory(["extra.service", "skchat-daemon.service"], ["skcapstone"]), WORKER
    )
    assert report.unexpected_units and report.forbidden_units
    assert report.severity == "error"


# -------------------------------------------------------------- ignore ---


def test_ignored_units_never_appear_as_unexpected() -> None:
    report = profile_doctor.diff(
        _inventory(
            [
                "skai-beellama.service",
                "gpg-agent.socket",
                "gpg-agent-ssh.socket",
                "dirmngr.socket",
            ],
            ["skcapstone"],
        ),
        WORKER,
    )
    assert report.unexpected_units == []
    assert report.clean is True


def test_an_ignore_glob_cannot_launder_a_forbidden_unit() -> None:
    """ "I take no position on this" must never override "this must not be
    here". Otherwise a broad ignore pattern silently disarms the mustNot
    list, which is the one list with teeth."""
    profile = _profile(
        units={"allowed": [], "mustNot": ["skchat-daemon.service"]},
        unitsIgnore=["sk*"],
    )
    report = profile_doctor.diff(_inventory(["skchat-daemon.service"]), profile)
    assert report.forbidden_units == ["skchat-daemon.service"]
    assert report.severity == "error"


def test_unit_ignore_patterns_do_not_leak_into_packages() -> None:
    profile = _profile(packages={"allowed": []}, unitsIgnore=["sk*"])
    report = profile_doctor.diff(_inventory([], ["skmemory"]), profile)
    assert report.unexpected_packages == ["skmemory"]


# ------------------------------------------------------ degrade safely ---


def test_an_empty_profile_produces_info_never_error() -> None:
    """The first draft of a manifest asserts nothing. It must be safe to
    ship: no forbidden findings, no missing-required findings."""
    report = profile_doctor.diff(_inventory(["a.service"], ["skcapstone"]), _profile())
    assert report.forbidden_units == []
    assert report.missing_required_units == []
    assert report.severity == "info"


def test_an_empty_inventory_is_not_read_as_forbidden() -> None:
    report = profile_doctor.diff(_inventory([], []), WORKER)
    assert report.forbidden_units == []
    assert report.forbidden_packages == []
    assert report.severity == "warn"


def test_a_missing_units_block_does_not_raise() -> None:
    assert profile_doctor.diff({}, WORKER).severity == "warn"


def test_only_the_user_scope_is_governed() -> None:
    """System scope is distro baseline; a role profile does not govern it,
    and diffing it would bury the report in 78 rows of snapd."""
    inv = _inventory(["skai-beellama.service"], ["skcapstone", "capauth"])
    inv["units"]["system"] = {"snapd.service": "enabled", "ollama.service": "enabled"}
    assert profile_doctor.diff(inv, WORKER).clean is True


# ---------------------------------------------------------- determinism ---


def test_lists_are_sorted_and_reports_compare_equal() -> None:
    inv = _inventory(["z.service", "a.service", "m.service"], ["skcapstone"])
    first = profile_doctor.diff(inv, WORKER)
    second = profile_doctor.diff(inv, WORKER)
    assert first == second
    assert first.unexpected_units == sorted(first.unexpected_units)


def test_report_is_frozen() -> None:
    report = profile_doctor.diff(_inventory([]), WORKER)
    with pytest.raises(Exception):
        report.severity = "ok"  # type: ignore[misc]


# --------------------------------------------------------------- purity ---


def test_module_performs_no_io() -> None:
    source = Path(profile_doctor.__file__).read_text(encoding="utf-8")
    for token in ("subprocess", "open(", "Path(", "os.", "systemctl", "requests"):
        assert token not in source, f"{token!r} found in a module that must stay pure"


def test_module_has_no_actuation_verb() -> None:
    source = Path(profile_doctor.__file__).read_text(encoding="utf-8")
    for verb in ("start", "stop", "enable(", "disable(", "install", "kill"):
        assert f".{verb}" not in source, f"actuation-shaped call {verb!r} in the diff module"
