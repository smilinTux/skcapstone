"""Tests for rollout drift detection (spec A.6, Task 3).

The whole point of this module is that a version check would have reported
every host healthy during all four real drift incidents measured on this
fleet: three different skmail binaries with no version difference (the file
belonged to no package), a dispatcher script copied out of band from the
package, a seat unit FAILED for weeks, and a rotate timer active but not
enabled for at least 7 weeks. These tests hold detect_drift to content and
state, never to a version string: content digests for unit files and the
dispatcher script, the git_sha, and an enabled-versus-active mismatch as a
first-class finding.

Every host state is built under a throwaway tmp_path `home`, never the real
`~/.skcapstone`. `repo_root` is this real checkout, reused the same way
test_deployment_manifest.py reuses it, so expected digests come from the
actual shipped systemd/ tree and dispatcher script rather than a hand-built
fake tree.

Live systemd is never touched: `skfleet_readiness.subprocess.run` is
monkeypatched, following the exact pattern tests/fleet/test_readiness_wiring.py
already established, so detect_drift's reuse of Task 2's `unit_in_scope` (and
the same module's `_systemctl_show_value`) is exercised against fixed
fixture output instead of a live systemd instance.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(REPO_ROOT / "scripts" / "fleet"))
import skfleet_readiness  # noqa: E402

from skcapstone.fleet import rollout_drift  # noqa: E402
from skcapstone.fleet.rollout_drift import Drift, detect_drift  # noqa: E402

ATLAS_SERVICE = "skfleet-atlas.service"
ATLAS_TIMER = "skfleet-atlas.timer"
CORE_SERVICE = "skcapstone.service"  # ships with no paired timer
DISPATCHER_NAME = "skfleet-rotate.py"


def _fake_systemctl(responses):
    """A subprocess.run replacement answering `systemctl --user show <unit>
    -p <prop> --value` from `responses[(unit, prop)]`, defaulting an unlisted
    (unit, prop) pair to LoadState=not-found so an unmentioned unit reads as
    genuinely unknown rather than silently succeeding.
    """

    def fake_run(cmd, **kwargs):
        assert cmd[:3] == ["systemctl", "--user", "show"], f"unexpected subprocess call: {cmd}"
        unit = cmd[3]
        prop = cmd[cmd.index("-p") + 1]
        value = responses.get((unit, prop), "not-found" if prop == "LoadState" else "")
        import subprocess

        return subprocess.CompletedProcess(cmd, 0, stdout=value + "\n", stderr="")

    return fake_run


@pytest.fixture(autouse=True)
def _no_live_systemctl(monkeypatch):
    """Every test must explicitly supply systemctl fixture output; a stray
    live call is a bug in the test, not a fact about the real fleet.
    """
    monkeypatch.setattr(skfleet_readiness.subprocess, "run", _fake_systemctl({}))


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def _install_unit(home: Path, unit_name: str) -> None:
    dest = home / ".config" / "systemd" / "user" / unit_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO_ROOT / "systemd" / unit_name, dest)


def _install_dispatcher(home: Path) -> None:
    dest = home / ".local" / "bin" / DISPATCHER_NAME
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO_ROOT / "scripts" / "fleet" / DISPATCHER_NAME, dest)


def _install_dist_info(home: Path, git_sha: str) -> None:
    dist_info = (
        home
        / ".skenv"
        / "lib"
        / "python3.12"
        / "site-packages"
        / (f"skcapstone-0.15.168.dev1+g{git_sha}.dist-info")
    )
    dist_info.mkdir(parents=True, exist_ok=True)


def _manifest(git_sha: str = "deadbeef") -> dict:
    return {
        "revision": "irrelevant-for-drift",
        "git_sha": git_sha,
        "package_version": "irrelevant-for-drift",
        "required_env": [],
        "units": [CORE_SERVICE, ATLAS_SERVICE],
    }


def _install_all_script_files(home: Path) -> None:
    """Mirror every pyproject.toml script-files entry into ~/.skenv/bin, the
    real pip install location -- reproducing pip's own behavior, not just a
    raw copy: pip REWRITES a Python script's shebang line to point at the
    venv's own interpreter on install (measured live on chiap01: the
    installed copy of every .py script-files entry differs from the repo
    in exactly and only that one line). A raw copyfile here would make this
    "healthy host" fixture unrealistic -- quiet only because it never
    exercised the one transform every real host's install actually applies.
    """
    for entry in rollout_drift._script_files(REPO_ROOT):
        dest = home / ".skenv" / "bin" / Path(entry).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        content = (REPO_ROOT / entry).read_bytes()
        if entry.endswith(".py") and content.startswith(b"#!"):
            _, _, rest = content.partition(b"\n")
            content = b"#!" + str(home / ".skenv" / "bin" / "python").encode() + b"\n" + rest
        dest.write_bytes(content)


def _matching_host(home: Path, git_sha: str = "deadbeef") -> None:
    """Build a home tree that agrees with `_manifest(git_sha)` in every way."""
    _install_unit(home, CORE_SERVICE)
    _install_unit(home, ATLAS_SERVICE)
    _install_dispatcher(home)
    _install_dist_info(home, git_sha)
    _install_all_script_files(home)


def _in_scope_atlas_responses() -> dict:
    """skfleet-atlas.timer active and enabled: fully converged, in scope."""
    return {
        (ATLAS_TIMER, "LoadState"): "loaded",
        (ATLAS_TIMER, "ActiveState"): "active",
        (ATLAS_TIMER, "UnitFileState"): "enabled",
    }


def _in_scope_rotate_responses() -> dict:
    return {
        ("skfleet-rotate.timer", "LoadState"): "loaded",
        ("skfleet-rotate.timer", "ActiveState"): "active",
        ("skfleet-rotate.timer", "UnitFileState"): "enabled",
    }


def _all_responses() -> dict:
    responses = {}
    responses.update(_in_scope_atlas_responses())
    responses.update(_in_scope_rotate_responses())
    return responses


# ---------------------------------------------------------------------------
# No drift when a host genuinely matches its manifest
# ---------------------------------------------------------------------------


def test_matching_host_reports_no_drift(home: Path, monkeypatch):
    monkeypatch.setattr(skfleet_readiness.subprocess, "run", _fake_systemctl(_all_responses()))
    monkeypatch.setenv("SKFLEET_NODE", "node-test")
    _matching_host(home)

    drifts = detect_drift(_manifest(), home, REPO_ROOT)

    assert drifts == []


# ---------------------------------------------------------------------------
# Changed unit file
# ---------------------------------------------------------------------------


def test_changed_unit_file_is_reported(home: Path, monkeypatch):
    monkeypatch.setattr(skfleet_readiness.subprocess, "run", _fake_systemctl(_all_responses()))
    monkeypatch.setenv("SKFLEET_NODE", "node-test")
    _matching_host(home)
    installed = home / ".config" / "systemd" / "user" / ATLAS_SERVICE
    installed.write_text(installed.read_text() + "\n# hand-edited on the host\n")

    drifts = detect_drift(_manifest(), home, REPO_ROOT)

    changed = [d for d in drifts if d.kind == "changed" and ATLAS_SERVICE in d.artifact]
    assert len(changed) == 1
    drift = changed[0]
    assert isinstance(drift, Drift)
    assert drift.host == "node-test"
    assert drift.expected != drift.found
    assert drift.found is not None


# ---------------------------------------------------------------------------
# Changed dispatcher script
# ---------------------------------------------------------------------------


def test_changed_dispatcher_script_is_reported(home: Path, monkeypatch):
    monkeypatch.setattr(skfleet_readiness.subprocess, "run", _fake_systemctl(_all_responses()))
    monkeypatch.setenv("SKFLEET_NODE", "node-test")
    _matching_host(home)
    installed = home / ".local" / "bin" / DISPATCHER_NAME
    installed.write_text(installed.read_text() + "\n# a stale, hand-copied edit\n")

    drifts = detect_drift(_manifest(), home, REPO_ROOT)

    changed = [d for d in drifts if d.kind == "changed" and "dispatcher" in d.artifact]
    assert len(changed) == 1
    assert changed[0].artifact == "dispatcher:skfleet-rotate.py"


# ---------------------------------------------------------------------------
# Missing is distinct from changed
# ---------------------------------------------------------------------------


def test_missing_unit_is_reported_distinctly_from_changed(home: Path, monkeypatch):
    monkeypatch.setattr(skfleet_readiness.subprocess, "run", _fake_systemctl(_all_responses()))
    monkeypatch.setenv("SKFLEET_NODE", "node-test")
    _matching_host(home)
    # skcapstone.service is never installed at all on this host.
    (home / ".config" / "systemd" / "user" / CORE_SERVICE).unlink()
    # skfleet-atlas.service IS installed but its content has drifted.
    installed = home / ".config" / "systemd" / "user" / ATLAS_SERVICE
    installed.write_text(installed.read_text() + "\n# hand-edited\n")

    drifts = detect_drift(_manifest(), home, REPO_ROOT)

    missing = [d for d in drifts if CORE_SERVICE in d.artifact]
    changed = [d for d in drifts if ATLAS_SERVICE in d.artifact and d.kind == "changed"]
    assert len(missing) == 1
    assert missing[0].kind == "missing"
    assert missing[0].found is None
    assert missing[0].expected is not None
    assert len(changed) == 1
    assert missing[0].kind != changed[0].kind


# ---------------------------------------------------------------------------
# Legitimate local state: an out-of-scope role's absence is not drift
# ---------------------------------------------------------------------------


def test_dispatcher_absent_on_a_seat_only_host_is_not_drift(home: Path, monkeypatch):
    """The chiap08 case: this host deliberately does not run the dispatcher.
    skfleet-rotate.timer is loaded but inactive and disabled here (measured,
    real shape), so unit_in_scope must say out-of-scope and the dispatcher's
    absence must not be reported.
    """
    responses = _in_scope_atlas_responses()
    responses.update(
        {
            ("skfleet-rotate.timer", "LoadState"): "loaded",
            ("skfleet-rotate.timer", "ActiveState"): "inactive",
            ("skfleet-rotate.timer", "UnitFileState"): "disabled",
        }
    )
    monkeypatch.setattr(skfleet_readiness.subprocess, "run", _fake_systemctl(responses))
    monkeypatch.setenv("SKFLEET_NODE", "node-test")
    _install_unit(home, CORE_SERVICE)
    _install_unit(home, ATLAS_SERVICE)
    _install_dist_info(home, "deadbeef")
    # deliberately no dispatcher script installed

    drifts = detect_drift(_manifest(), home, REPO_ROOT)

    assert [d for d in drifts if "dispatcher" in d.artifact] == []


# ---------------------------------------------------------------------------
# Unit enablement versus activity
# ---------------------------------------------------------------------------


def test_active_but_not_enabled_unit_is_reported(home: Path, monkeypatch):
    """The measured skfleet-rotate.timer incident, generalised: a timer that
    is active right now but carries no timers.target.wants symlink is a
    latent total outage waiting for the next reboot, and no content digest
    catches it because the unit FILE is correct.
    """
    responses = {
        (ATLAS_TIMER, "LoadState"): "loaded",
        (ATLAS_TIMER, "ActiveState"): "active",
        (ATLAS_TIMER, "UnitFileState"): "disabled",
    }
    responses.update(_in_scope_rotate_responses())
    monkeypatch.setattr(skfleet_readiness.subprocess, "run", _fake_systemctl(responses))
    monkeypatch.setenv("SKFLEET_NODE", "node-test")
    _matching_host(home)

    drifts = detect_drift(_manifest(), home, REPO_ROOT)

    mismatches = [d for d in drifts if d.kind == "enablement_mismatch"]
    assert len(mismatches) == 1
    drift = mismatches[0]
    assert ATLAS_TIMER in drift.artifact
    assert drift.expected == "enabled"
    assert drift.found == "disabled"
    assert drift.host == "node-test"


def test_active_and_enabled_unit_reports_no_enablement_drift(home: Path, monkeypatch):
    monkeypatch.setattr(skfleet_readiness.subprocess, "run", _fake_systemctl(_all_responses()))
    monkeypatch.setenv("SKFLEET_NODE", "node-test")
    _matching_host(home)

    drifts = detect_drift(_manifest(), home, REPO_ROOT)

    assert [d for d in drifts if d.kind == "enablement_mismatch"] == []


# ---------------------------------------------------------------------------
# A FAILED unit is unambiguous drift, even when its enablement is
# self-consistent (the chiap08 incident: skfleet-niobe-shadow.service sat
# FAILED for weeks. Its ActiveState was never "active", so the enablement
# check above -- which only compares "active but not enabled" or "enabled
# but not active" -- saw a self-consistent inactive/disabled unit and
# emitted nothing).
# ---------------------------------------------------------------------------


def test_failed_unit_with_correct_enablement_is_reported(home: Path, monkeypatch):
    """A unit whose enablement is self-consistent (disabled, not active) but
    whose ActiveState is literally "failed" must still be reported: a
    crashed unit is unambiguous drift regardless of what its enable state
    says.
    """
    responses = {
        (ATLAS_SERVICE, "ActiveState"): "failed",
    }
    responses.update(_in_scope_atlas_responses())  # the paired timer stays healthy
    responses.update(_in_scope_rotate_responses())
    monkeypatch.setattr(skfleet_readiness.subprocess, "run", _fake_systemctl(responses))
    monkeypatch.setenv("SKFLEET_NODE", "node-test")
    _matching_host(home)

    drifts = detect_drift(_manifest(), home, REPO_ROOT)

    failed = [d for d in drifts if d.kind == "failed"]
    assert len(failed) == 1
    drift = failed[0]
    assert drift.artifact == f"unit_failed:{ATLAS_SERVICE}"
    assert drift.expected == "not failed"
    assert drift.found == "failed"
    assert drift.host == "node-test"
    # And the enablement check (a different, still-healthy signal on the
    # paired timer) must not also fire for the same underlying unit.
    assert [d for d in drifts if d.kind == "enablement_mismatch"] == []


def test_healthy_unit_reports_no_failed_drift(home: Path, monkeypatch):
    monkeypatch.setattr(skfleet_readiness.subprocess, "run", _fake_systemctl(_all_responses()))
    monkeypatch.setenv("SKFLEET_NODE", "node-test")
    _matching_host(home)

    drifts = detect_drift(_manifest(), home, REPO_ROOT)

    assert [d for d in drifts if d.kind == "failed"] == []


# ---------------------------------------------------------------------------
# git_sha
# ---------------------------------------------------------------------------


def test_git_sha_mismatch_is_reported(home: Path, monkeypatch):
    monkeypatch.setattr(skfleet_readiness.subprocess, "run", _fake_systemctl(_all_responses()))
    monkeypatch.setenv("SKFLEET_NODE", "node-test")
    _matching_host(home, git_sha="deadbeef")

    drifts = detect_drift(_manifest(git_sha="c0ffee00"), home, REPO_ROOT)

    git_sha_drifts = [d for d in drifts if d.artifact == "git_sha"]
    assert len(git_sha_drifts) == 1
    assert git_sha_drifts[0].kind == "changed"
    assert git_sha_drifts[0].expected == "c0ffee00"
    assert git_sha_drifts[0].found == "deadbeef"


def test_git_sha_missing_when_no_dist_info_is_reported_as_missing_not_changed(
    home: Path, monkeypatch
):
    monkeypatch.setattr(skfleet_readiness.subprocess, "run", _fake_systemctl(_all_responses()))
    monkeypatch.setenv("SKFLEET_NODE", "node-test")
    _install_unit(home, CORE_SERVICE)
    _install_unit(home, ATLAS_SERVICE)
    _install_dispatcher(home)
    # no .skenv dist-info installed at all

    drifts = detect_drift(_manifest(), home, REPO_ROOT)

    git_sha_drifts = [d for d in drifts if d.artifact == "git_sha"]
    assert len(git_sha_drifts) == 1
    assert git_sha_drifts[0].kind == "missing"
    assert git_sha_drifts[0].found is None


# ---------------------------------------------------------------------------
# Hand-installed critical units: never shipped, but the enablement check
# still has to see them. skfleet-rotate.service/.timer are in NEITHER
# shipped tree (see ALLOWED_UNSHIPPED_UNITS in rollout_drift.py) but are
# real, live, hand-installed units, and this is the exact incident that
# motivated the enablement check in the first place: measured on all three
# rotate hosts, skfleet-rotate.timer active=active, enabled=disabled, for
# at least 7 weeks, with no shipped-only loop ever able to see it.
# ---------------------------------------------------------------------------


def test_hand_installed_dispatcher_timer_active_but_not_enabled_is_reported(
    home: Path, monkeypatch
):
    responses = _in_scope_atlas_responses()
    responses.update(
        {
            ("skfleet-rotate.timer", "LoadState"): "loaded",
            ("skfleet-rotate.timer", "ActiveState"): "active",
            ("skfleet-rotate.timer", "UnitFileState"): "disabled",
        }
    )
    monkeypatch.setattr(skfleet_readiness.subprocess, "run", _fake_systemctl(responses))
    monkeypatch.setenv("SKFLEET_NODE", "node-test")
    _matching_host(home)

    drifts = detect_drift(_manifest(), home, REPO_ROOT)

    mismatches = [
        d for d in drifts if d.kind == "enablement_mismatch" and "skfleet-rotate" in d.artifact
    ]
    assert len(mismatches) == 1
    drift = mismatches[0]
    assert drift.artifact == "unit_enablement:skfleet-rotate.timer"
    assert drift.expected == "enabled"
    assert drift.found == "disabled"
    assert drift.host == "node-test"


def test_hand_installed_dispatcher_timer_active_and_enabled_reports_no_enablement_drift(
    home: Path, monkeypatch
):
    monkeypatch.setattr(skfleet_readiness.subprocess, "run", _fake_systemctl(_all_responses()))
    monkeypatch.setenv("SKFLEET_NODE", "node-test")
    _matching_host(home)

    drifts = detect_drift(_manifest(), home, REPO_ROOT)

    assert [
        d for d in drifts if "skfleet-rotate" in d.artifact and d.kind == "enablement_mismatch"
    ] == []


def test_hand_installed_dispatcher_out_of_scope_enablement_is_not_checked(home: Path, monkeypatch):
    """The chiap08 shape: rotate timer loaded but inactive and disabled, so
    out of scope. A host that deliberately does not run this role must not
    be held to "is it enabled" any more than it is held to "is the script
    installed" (test_dispatcher_absent_on_a_seat_only_host_is_not_drift,
    above, covers that half).
    """
    responses = _in_scope_atlas_responses()
    responses.update(
        {
            ("skfleet-rotate.timer", "LoadState"): "loaded",
            ("skfleet-rotate.timer", "ActiveState"): "inactive",
            ("skfleet-rotate.timer", "UnitFileState"): "disabled",
        }
    )
    monkeypatch.setattr(skfleet_readiness.subprocess, "run", _fake_systemctl(responses))
    monkeypatch.setenv("SKFLEET_NODE", "node-test")
    _install_unit(home, CORE_SERVICE)
    _install_unit(home, ATLAS_SERVICE)
    _install_dist_info(home, "deadbeef")
    # The pip-installed script-files copy of skfleet-rotate.py (unlike the
    # hand-installed ~/.local/bin dispatcher this test is actually about)
    # ships with the package on every host regardless of role, so it must
    # be present here or its own "missing" finding would be indistinguishable
    # from the enablement question this test exists to isolate.
    _install_all_script_files(home)

    drifts = detect_drift(_manifest(), home, REPO_ROOT)

    assert [d for d in drifts if "skfleet-rotate" in d.artifact] == []


# ---------------------------------------------------------------------------
# pyproject.toml script-files: the skmail incident, generalised. skmail is
# declared in script-files now, but the detector must catch the NEXT
# divergent copy the same way, for any entry in that list, not just skmail.
# ---------------------------------------------------------------------------


def test_script_files_list_includes_skmail():
    """skmail is declared in pyproject.toml's script-files (the specific bug
    already fixed); this pins that so a regression there fails loudly here
    too, not only in a packaging test far away.
    """
    assert "scripts/fleet/skmail" in rollout_drift._script_files(REPO_ROOT)


def test_divergent_script_file_binary_is_reported(home: Path, monkeypatch):
    """A script-files entry installed with different content than the repo
    ships -- the exact shape of the skmail incident (three different
    binaries across five hosts, none matching the repo) -- must be reported
    for ANY entry in the list, not just skmail by name.
    """
    monkeypatch.setattr(skfleet_readiness.subprocess, "run", _fake_systemctl(_all_responses()))
    monkeypatch.setenv("SKFLEET_NODE", "node-test")
    _matching_host(home)
    installed = home / ".skenv" / "bin" / "skmail"
    installed.write_bytes(installed.read_bytes() + b"\n# a divergent binary on this host\n")

    drifts = detect_drift(_manifest(), home, REPO_ROOT)

    changed = [d for d in drifts if d.artifact == "script:skmail"]
    assert len(changed) == 1
    assert changed[0].kind == "changed"
    assert changed[0].found is not None
    assert changed[0].expected != changed[0].found


def test_missing_script_file_is_reported_distinctly_from_changed(home: Path, monkeypatch):
    monkeypatch.setattr(skfleet_readiness.subprocess, "run", _fake_systemctl(_all_responses()))
    monkeypatch.setenv("SKFLEET_NODE", "node-test")
    _matching_host(home)
    (home / ".skenv" / "bin" / "skmail").unlink()

    drifts = detect_drift(_manifest(), home, REPO_ROOT)

    missing = [d for d in drifts if d.artifact == "script:skmail"]
    assert len(missing) == 1
    assert missing[0].kind == "missing"
    assert missing[0].found is None


def test_matching_script_files_report_no_drift(home: Path, monkeypatch):
    """A host with every script-files entry correctly installed (this
    checkout's own fixture-built 'healthy host') shows no script drift --
    proving the check is quiet on a genuinely matching host, not just loud
    on a broken one.
    """
    monkeypatch.setattr(skfleet_readiness.subprocess, "run", _fake_systemctl(_all_responses()))
    monkeypatch.setenv("SKFLEET_NODE", "node-test")
    _matching_host(home)

    drifts = detect_drift(_manifest(), home, REPO_ROOT)

    assert [d for d in drifts if d.artifact.startswith("script:")] == []


def test_pip_rewritten_shebang_alone_is_not_reported_as_drift(home: Path, monkeypatch):
    """Measured live on chiap01: pip rewrites a Python script-files entry's
    shebang line (#!/usr/bin/env python3 -> #!<venv>/bin/python) on every
    install. That is universal and harmless, never a fact about this host
    being wrong, so a script whose ONLY difference from the repo is its
    shebang line must not be reported.
    """
    monkeypatch.setattr(skfleet_readiness.subprocess, "run", _fake_systemctl(_all_responses()))
    monkeypatch.setenv("SKFLEET_NODE", "node-test")
    _matching_host(home)
    installed = home / ".skenv" / "bin" / "skfleet_readiness.py"
    body = installed.read_bytes().split(b"\n", 1)[1]
    installed.write_bytes(b"#!/home/other-user/.skenv/bin/python3\n" + body)

    drifts = detect_drift(_manifest(), home, REPO_ROOT)

    assert [d for d in drifts if d.artifact == "script:skfleet_readiness.py"] == []
