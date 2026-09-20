"""Seraph must resolve the dispatcher where it is actually DEPLOYED.

The estate home and the user home are different directories on every live
host: the estate home is a subdirectory of the user home, and `.local/bin`
hangs off the user home. `deployed_artifact_path` joins `.local/bin` to
whatever base it is handed, so handing it an estate home builds a path with
the estate directory wedged in the middle, which no rollout has ever
written.

That is what shipped. `seraph_operation` and `role_dispatch_operation`
passed their estate home as the base, so on chi they looked for
`~/.skcapstone/.local/bin/skfleet-rotate.py` while the rollout deploys
`~/.local/bin/skfleet-rotate.py`. Both guard with `is_file()`, so the
dispatch reported `seraph_dispatcher_missing` with `suppressed: 1` and the
independent-review lane launched nothing for 19 hours while
`awaiting_review` climbed past 400.

The reason it survived review is that the tests collapsed the two homes
into one `tmp_path`, where the correct and the incorrect base name the same
file. These tests keep them apart on purpose, so passing the estate home
again is observable rather than invisible.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import skcapstone.seat_cycle_entrypoint as seat_entrypoint
from skcapstone.fleet.deployment_manifest import (
    DISPATCHER_RELATIVE_PATH,
    PER_HOST_BIN_RELATIVE_DIR,
    deployed_artifact_path,
)

DISPATCHER_NAME = DISPATCHER_RELATIVE_PATH.name

#: A dispatcher that launches nothing and reports why, so these tests
#: exercise the REAL resolve-and-execute path instead of monkeypatching the
#: launch out of it. A test that stubs the subprocess cannot tell a
#: correctly resolved dispatcher from a stub that never needed one.
_NOOP_DISPATCHER = """#!/usr/bin/env python3
print("NOOP_RECEIPT|chiap08|reason=no_eligible_work|seat={seat}")
"""


def _deploy(base: Path, *, seat: str = "seraph") -> Path:
    dispatcher = base / PER_HOST_BIN_RELATIVE_DIR / DISPATCHER_NAME
    dispatcher.parent.mkdir(parents=True, exist_ok=True)
    dispatcher.write_text(_NOOP_DISPATCHER.format(seat=seat), encoding="utf-8")
    dispatcher.chmod(0o755)
    return dispatcher


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """A user home and an estate home that are NOT the same directory.

    Shaped like a live host: the estate home sits INSIDE the user home, so
    a base mix-up yields a plausible-looking path rather than an obviously
    absurd one -- which is precisely why the mix-up was not spotted.
    """
    user_home = tmp_path / "user"
    estate_home = user_home / "estate"
    estate_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(user_home))
    return user_home, estate_home


def test_the_default_base_is_the_user_home(homes) -> None:
    """No argument means "the artifact deployed on this machine"."""
    user_home, _ = homes
    assert deployed_artifact_path(DISPATCHER_NAME) == (
        user_home / PER_HOST_BIN_RELATIVE_DIR / DISPATCHER_NAME
    )


def test_an_estate_home_base_names_a_path_no_rollout_writes(homes) -> None:
    """The two bases must not resolve to the same file.

    If they ever do, every behavioural test below is vacuous, so this
    asserts the precondition those tests depend on.
    """
    user_home, estate_home = homes
    deployed = _deploy(user_home)
    wrong = deployed_artifact_path(DISPATCHER_NAME, estate_home)

    assert wrong != deployed
    assert not wrong.exists()
    assert estate_home.name in wrong.parts


def test_seraph_dispatches_when_the_dispatcher_is_under_the_user_home(homes) -> None:
    user_home, estate_home = homes
    _deploy(user_home)

    result = seat_entrypoint.seraph_operation(estate_home)

    assert result["reason"] == "seraph_no_eligible_work"
    assert result["suppressed"] == 0


def test_seraph_reports_missing_when_only_the_estate_home_has_a_dispatcher(homes) -> None:
    """The production defect, reproduced exactly.

    A dispatcher deployed under the ESTATE home is not the deployed
    dispatcher. If a future change passes `home` as the artifact base
    again, this file is the one it finds, and the assertion below is what
    turns that into a red test instead of a silent 19-hour outage.
    """
    _, estate_home = homes
    _deploy(estate_home)
    assert not deployed_artifact_path(DISPATCHER_NAME).exists()

    result = seat_entrypoint.seraph_operation(estate_home)

    assert result["reason"] == "seraph_dispatcher_missing"
    assert result["suppressed"] == 1


def test_role_dispatch_dispatches_when_the_dispatcher_is_under_the_user_home(
    homes, monkeypatch
) -> None:
    user_home, estate_home = homes
    _deploy(user_home, seat="atlas")
    monkeypatch.setenv("SKFLEET_ATLAS_BATCH_SIZE", "1")

    result = seat_entrypoint.role_dispatch_operation(estate_home, "atlas")

    assert result["reason"] == "atlas_no_eligible_work"


def test_role_dispatch_reports_missing_when_only_the_estate_home_has_a_dispatcher(
    homes, monkeypatch
) -> None:
    """The second call site carried the identical defect."""
    _, estate_home = homes
    _deploy(estate_home, seat="atlas")
    monkeypatch.setenv("SKFLEET_ATLAS_BATCH_SIZE", "1")

    result = seat_entrypoint.role_dispatch_operation(estate_home, "atlas")

    assert result["reason"] == "atlas_dispatcher_missing"
    assert result["suppressed"] == 1


def test_an_unreadable_deployment_still_fails_closed(homes) -> None:
    """Resolving the right path is not the same as being able to run it.

    The guard is `is_file() and os.access(X_OK)`; this pins the second half
    so a non-executable deploy cannot be mistaken for a healthy one.
    """
    user_home, estate_home = homes
    dispatcher = _deploy(user_home)
    dispatcher.chmod(0o644)
    assert not os.access(dispatcher, os.X_OK)

    result = seat_entrypoint.seraph_operation(estate_home)

    assert result["reason"] == "seraph_dispatcher_missing"
