"""``deploy`` restarts must be proven by a NEW start, and smokes judge only that start.

2026-11-15, hermes-gateway on noroc2027: ``try-restart`` timed out while the old
process was still stopping, deploy returned, and the unit smoke grepped log lines
from the PREVIOUS start, so a gateway with 0 MCP servers passed. A restart now
counts only when the unit is active under a different InvocationID, and each unit
smoke gets that start's InvocationID and ActiveEnterTimestamp.
"""

from __future__ import annotations

import subprocess

from skcapstone.deploy import rollout
from skcapstone.deploy.manifest import Manifest


class Unit:
    """A fake systemd user unit: ``starts_after`` show-polls before the new start is visible."""

    def __init__(
        self,
        before="inv-old",
        after="inv-new",
        starts_after=2,
        timeout_restart=False,
        final_state="active",
    ):
        self.inv, self.after, self.left = before, after, None
        self.starts_after, self.timeout_restart, self.final_state = (
            starts_after,
            timeout_restart,
            final_state,
        )
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        out = ""
        if cmd[:3] == ["systemctl", "--user", "try-restart"]:
            self.left = self.starts_after
            if self.timeout_restart:
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 0))
        elif cmd[:3] == ["systemctl", "--user", "show"]:
            state = "active"
            if self.left is not None:
                if self.left > 0:
                    self.left -= 1
                    state = "deactivating"
                else:
                    self.inv, state = self.after, self.final_state
            out = f"ActiveState={state}\nInvocationID={self.inv}\nActiveEnterTimestamp=Sun 2026-11-15 01:04:18 EST\n"
        elif cmd[:3] == ["systemctl", "--user", "is-active"]:
            out = (
                "active\n"
                if self.left is None or self.left == 0 and self.final_state == "active"
                else "deactivating\n"
            )
        return subprocess.CompletedProcess(cmd, 0, out, "")


def _fast(monkeypatch):
    monkeypatch.setattr(rollout.time, "sleep", lambda s: None)


def test_restart_waits_for_the_new_invocation(monkeypatch):
    _fast(monkeypatch)
    unit, started = Unit(starts_after=3), {}
    assert rollout.restart_units(["g.service"], unit, started=started) == []
    assert started["g.service"]["invocation"] == "inv-new"
    assert started["g.service"]["since"] == "Sun 2026-11-15 01:04:18 EST"


def test_restart_timeout_still_waits_for_the_new_start(monkeypatch):
    _fast(monkeypatch)
    unit, started = Unit(starts_after=5, timeout_restart=True), {}
    assert rollout.restart_units(["g.service"], unit, started=started) == []
    assert started["g.service"]["invocation"] == "inv-new"


def test_restart_that_never_starts_anew_fails(monkeypatch):
    _fast(monkeypatch)
    clock = iter(range(0, 10_000, 5))
    monkeypatch.setattr(rollout.time, "monotonic", lambda: next(clock))
    unit = Unit(
        after="inv-old", starts_after=0
    )  # same invocation: the old process never went away
    problems = rollout.restart_units(["g.service"], unit, wait=60)
    assert problems and "did not start anew" in problems[0]


def test_restart_into_failed_state_fails(monkeypatch):
    _fast(monkeypatch)
    clock = iter(range(0, 10_000, 5))
    monkeypatch.setattr(rollout.time, "monotonic", lambda: next(clock))
    problems = rollout.restart_units(
        ["g.service"], Unit(final_state="failed", starts_after=0), wait=60
    )
    assert problems and "g.service" in problems[0]


def test_unit_smoke_gets_the_new_start(monkeypatch):
    _fast(monkeypatch)
    manifest = Manifest.model_validate(
        {"packages": {}, "services": {"g.service": {"smoke": "check-it"}}}
    )
    seen = {}

    def runner(cmd, **kw):
        if cmd[:2] == ["bash", "-c"]:
            seen.update({k: v for k, v in kw["env"].items() if k.startswith("SK_DEPLOY_")})
        return subprocess.CompletedProcess(cmd, 0, "", "")

    started = {"g.service": {"invocation": "inv-new", "since": "Sun 2026-11-15 01:04:18 EST"}}
    from pathlib import Path

    assert (
        rollout.smoke(manifest, ["g.service"], Path("/nonexistent"), runner, started=started) == []
    )
    assert seen == {
        "SK_DEPLOY_UNIT": "g.service",
        "SK_DEPLOY_INVOCATION_ID": "inv-new",
        "SK_DEPLOY_SINCE": "Sun 2026-11-15 01:04:18 EST",
    }


def test_unit_without_invocation_id_keeps_the_is_active_check(monkeypatch):
    _fast(monkeypatch)

    def runner(cmd, **kw):
        out = "active\n" if "is-active" in cmd else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")

    assert rollout.restart_units(["g.service"], runner) == []
