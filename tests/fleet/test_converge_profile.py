"""Tests for the converge-side profile gate (epic 3bbf39ea, card 57357411).

The gate answers one question: may a node of THIS role run THIS unit? It is
allowed to report, and under enforce it is allowed to stop HEALING. It is
never allowed to stop a unit, which is what most of this file pins down.
"""

from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from skcapstone.fleet import backoff, converge, events, profiles, store

NODE = "node-100"
SHOW = (
    "systemctl --user show skgateway.service "
    "--property=LoadState,ActiveState,MainPID,ActiveEnterTimestamp"
)
ACTIVE = (0, "LoadState=loaded\nActiveState=active\nMainPID=42\nActiveEnterTimestamp=t0\n")
FAILED = (0, "LoadState=loaded\nActiveState=failed\nMainPID=0\nActiveEnterTimestamp=\n")

MANIFESTS = Path(__file__).resolve().parents[2] / "deploy" / "fleet-objects" / "profile"

#: skgateway.service is in worker-gpu's units.mustNot and in control's
#: units.allowed, so the same unit flips the gate purely on role.
FORBIDDEN_ROLE = "worker-gpu"
PERMITTED_ROLE = "control"


class FakeRunner:
    """Records every command so a test can prove which verbs never ran."""

    def __init__(self, replies: dict[str, tuple[int, str]]) -> None:
        self.replies = dict(replies)
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> CompletedProcess:
        self.calls.append(cmd)
        code, out = self.replies.get(" ".join(cmd), (0, ""))
        return CompletedProcess(cmd, code, stdout=out, stderr="")

    def verbs(self) -> list[str]:
        return [
            " ".join(c)
            for c in self.calls
            if c[:2] == ["systemctl", "--user"] and c[2] in ("start", "restart")
        ]

    def stop_verbs(self) -> list[str]:
        """Any command that could take a running workload down, in any runtime."""
        return [
            " ".join(c)
            for c in self.calls
            if any(word in ("stop", "kill", "disable", "mask", "rm", "down") for word in c)
        ]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    events.reset_dedupe()
    backoff.reset_trackers()
    # Pin the manifest source so the gate cannot answer from a live fleet
    # tree that happens to exist on the machine running the tests.
    monkeypatch.setenv(profiles.MANIFEST_DIR_ENV, str(MANIFESTS))
    yield
    events.reset_dedupe()
    backoff.reset_trackers()


def _fleet(paths, operator, scheduler_writer, *, role: str) -> None:
    node_spec = {"cordoned": False, "taints": [], "actuate": True}
    if role:
        node_spec["role"] = role
    store.write_spec(paths, "node", NODE, node_spec, writer=operator)
    store.write_spec(paths, "service", "skgateway", {"unit": "skgateway.service"}, writer=operator)
    store.write_placement(
        paths, "service", "skgateway", node=NODE, reason="pinned for test", writer=scheduler_writer
    )


def _runner() -> FakeRunner:
    return FakeRunner(
        {
            SHOW: FAILED,
            "systemctl --user restart skgateway.service": (0, ""),
            "journalctl --user -u skgateway.service -n 30 --no-pager": (0, "boom\n"),
        }
    )


def _cond(paths, type_: str):
    st = store.read_status(paths, "service", "skgateway", NODE)
    return {c["type"]: c for c in st["conditions"]}.get(type_)


def _degrades(paths) -> list[dict]:
    return [
        e
        for e in events.read(paths, NODE, kind="service", name="skgateway")
        if e["reason"] == "OutsideProfile"
    ]


# --------------------------------------------------------------------------
# The three rollout modes
# --------------------------------------------------------------------------


def test_gate_off_is_the_default_and_asks_nothing(
    paths, operator, scheduler_writer, monkeypatch
) -> None:
    monkeypatch.delenv(profiles.PROFILE_GATE_ENV, raising=False)
    _fleet(paths, operator, scheduler_writer, role=FORBIDDEN_ROLE)
    runner = _runner()
    converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    assert runner.verbs() == ["systemctl --user restart skgateway.service"]
    assert _cond(paths, "OutsideProfile") is None
    assert _degrades(paths) == []


def test_an_unknown_gate_value_reads_as_off(monkeypatch) -> None:
    """A typo must never arm a gate, the same rule signing_mode() follows."""
    monkeypatch.setenv(profiles.PROFILE_GATE_ENV, "ENFROCE")
    assert profiles.gate_mode() == "off"


def test_shadow_reports_and_changes_nothing_else(
    paths, operator, scheduler_writer, monkeypatch
) -> None:
    monkeypatch.setenv(profiles.PROFILE_GATE_ENV, "shadow")
    _fleet(paths, operator, scheduler_writer, role=FORBIDDEN_ROLE)
    runner = _runner()
    out = converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    # The whole point of shadow: identical actuation to the off run above.
    assert runner.verbs() == ["systemctl --user restart skgateway.service"]
    assert out["services"]["skgateway"]["acted"] == "healed"
    cond = _cond(paths, "OutsideProfile")
    assert cond["status"] == "True" and cond["reason"] == "UnitForbiddenForRole"
    assert len(_degrades(paths)) == 1


def test_enforce_suppresses_healing(paths, operator, scheduler_writer, monkeypatch) -> None:
    monkeypatch.setenv(profiles.PROFILE_GATE_ENV, "enforce")
    _fleet(paths, operator, scheduler_writer, role=FORBIDDEN_ROLE)
    runner = _runner()
    out = converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    assert runner.verbs() == []
    assert out["services"]["skgateway"]["acted"] == "outside-profile"
    assert _cond(paths, "OutsideProfile")["status"] == "True"
    # Reporting continues: the status file still says the unit is down.
    assert _cond(paths, "Ready")["status"] == "False"


def test_enforce_never_issues_a_stop_verb(paths, operator, scheduler_writer, monkeypatch) -> None:
    """The load-bearing safety property of the whole card.

    Checked against a HEALTHY unit, which is the dangerous case: a running
    service that a manifest says should not be here is exactly what a
    naive gate would tear down.
    """
    monkeypatch.setenv(profiles.PROFILE_GATE_ENV, "enforce")
    _fleet(paths, operator, scheduler_writer, role=FORBIDDEN_ROLE)
    runner = FakeRunner({SHOW: ACTIVE})
    converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    assert runner.stop_verbs() == []
    assert runner.verbs() == []
    # Only the read-only observation call was made.
    assert runner.calls == [SHOW.split()]
    assert _cond(paths, "Ready")["status"] == "True"


def test_enforce_still_heals_a_unit_the_role_permits(
    paths, operator, scheduler_writer, monkeypatch
) -> None:
    monkeypatch.setenv(profiles.PROFILE_GATE_ENV, "enforce")
    _fleet(paths, operator, scheduler_writer, role=PERMITTED_ROLE)
    runner = _runner()
    converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    assert runner.verbs() == ["systemctl --user restart skgateway.service"]
    assert _cond(paths, "OutsideProfile")["status"] == "False"
    assert _degrades(paths) == []


def test_an_unbound_node_is_never_blocked(paths, operator, scheduler_writer, monkeypatch) -> None:
    """Most nodes carry no role until the backfill lands."""
    monkeypatch.setenv(profiles.PROFILE_GATE_ENV, "enforce")
    _fleet(paths, operator, scheduler_writer, role="")
    runner = _runner()
    converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    assert runner.verbs() == ["systemctl --user restart skgateway.service"]


def test_enforce_with_no_manifests_heals_normally(
    paths, operator, scheduler_writer, monkeypatch, tmp_path
) -> None:
    """An install that has not synced its manifests yet must not stall."""
    monkeypatch.setenv(profiles.PROFILE_GATE_ENV, "enforce")
    monkeypatch.setenv(profiles.MANIFEST_DIR_ENV, str(tmp_path / "gone"))
    _fleet(paths, operator, scheduler_writer, role=FORBIDDEN_ROLE)
    runner = _runner()
    converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    assert runner.verbs() == ["systemctl --user restart skgateway.service"]
    assert _cond(paths, "OutsideProfile")["status"] == "False"


# --------------------------------------------------------------------------
# profiles.profile_of
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"spec": {}}, {"spec": {"role": ""}}, {"spec": {"role": "  "}}, {"spec": None}],
    ids=["none", "empty", "no-role", "blank", "whitespace", "null-spec"],
)
def test_profile_of_returns_none_when_unbound(payload) -> None:
    assert profiles.profile_of(payload) is None


def test_profile_of_reads_the_role_off_the_spec_block() -> None:
    payload = {"name": NODE, "spec": {"role": "worker-gpu", "actuate": True}}
    assert profiles.profile_of(payload) == "worker-gpu"


# --------------------------------------------------------------------------
# profiles.unit_allowed against the real shipped manifests
# --------------------------------------------------------------------------


def test_the_same_unit_flips_on_role() -> None:
    """NOTE, deliberate deviation from the card text.

    Card 57357411 asks for `unit_allowed('builder-standby',
    'skchat-daemon.service') is False`. The shipped manifest disagrees:
    builder-standby lists skchat-daemon.service under units.ALLOWED, not
    mustNot, because the standby is the warm replica and may run the chat
    daemon. Editing the manifest to satisfy the assertion would break
    test_manifests_match_their_generator and, worse, would make a real box
    read as drift. The roles that actually forbid that unit are worker-gpu
    and observer, so the same-unit-flips-on-role property is pinned there.
    """
    daemon = "skchat-daemon.service"
    assert profiles.unit_allowed("worker-gpu", daemon, manifests=MANIFESTS) is False
    assert profiles.unit_allowed("observer", daemon, manifests=MANIFESTS) is False
    assert profiles.unit_allowed("control", "skchat-daemon.service", manifests=MANIFESTS) is True
    # The card's literal pair, recorded as the manifest actually reads it.
    assert (
        profiles.unit_allowed("builder-standby", "skchat-daemon.service", manifests=MANIFESTS)
        is True
    )
    manifest = json.loads((MANIFESTS / "builder-standby.json").read_text(encoding="utf-8"))
    assert "skchat-daemon.service" in manifest["spec"]["units"]["allowed"]


def test_every_role_forbids_its_own_mustnot_list() -> None:
    for path in sorted(MANIFESTS.glob("*.json")):
        spec = json.loads(path.read_text(encoding="utf-8"))["spec"]
        for unit in spec["units"]["mustNot"]:
            assert profiles.unit_allowed(path.stem, unit, manifests=MANIFESTS) is False


def test_a_unit_no_manifest_mentions_is_allowed() -> None:
    """Unexpected is the manifest lagging reality, never a reason to refuse."""
    for role in ("control", "builder-standby", "worker-gpu", "observer"):
        assert profiles.unit_allowed(role, "brand-new-thing.service", manifests=MANIFESTS) is True


@pytest.mark.parametrize("role", [None, "", "no-such-role", "../../etc/passwd", "Control"])
def test_an_unresolvable_role_allows_everything(role) -> None:
    assert profiles.unit_allowed(role, "skgateway.service", manifests=MANIFESTS) is True


# --------------------------------------------------------------------------
# Degrade-safe: unreadable manifests allow everything
# --------------------------------------------------------------------------


UNITS = ["skgateway.service", "skoperator.timer", "ollama.service", "", "anything"]
ROLES = ["control", "builder-standby", "worker-gpu", "observer", "unknown"]


def test_missing_manifests_allow_every_input(tmp_path) -> None:
    missing = tmp_path / "not-created"
    for role in ROLES:
        for unit in UNITS:
            assert profiles.unit_allowed(role, unit, manifests=missing) is True


def test_invalid_manifests_allow_every_input(tmp_path) -> None:
    """Bad JSON, a valid file with no spec, and a spec that fails validation."""
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "control.json").write_text("{not json", encoding="utf-8")
    (bad / "worker-gpu.json").write_text('{"kind": "profile"}', encoding="utf-8")
    (bad / "observer.json").write_text(
        json.dumps({"spec": {"units": {"mustNot": ["skgateway.service"]}}}), encoding="utf-8"
    )
    for role in ROLES:
        for unit in UNITS:
            assert profiles.unit_allowed(role, unit, manifests=bad) is True


def test_an_unreadable_override_means_no_manifests(monkeypatch, tmp_path) -> None:
    """The override is authoritative: it must not silently fall back to the
    shipped copy, or the gate would answer from a set nobody pointed it at."""
    monkeypatch.setenv(profiles.MANIFEST_DIR_ENV, str(tmp_path / "nope"))
    assert profiles.manifest_dir() is None
    assert profiles.unit_allowed("worker-gpu", "skgateway.service") is True


def test_the_fleet_tree_wins_over_the_shipped_copy(paths, monkeypatch) -> None:
    monkeypatch.delenv(profiles.MANIFEST_DIR_ENV, raising=False)
    tree_dir = paths.objects / "profile"
    tree_dir.mkdir(parents=True)
    assert profiles.manifest_dir(paths) == tree_dir
    # With no tree to consult, the source checkout is the fallback.
    assert profiles.manifest_dir(None) == MANIFESTS


# --------------------------------------------------------------------------
# Layering
# --------------------------------------------------------------------------


def test_profiles_stays_a_leaf_module() -> None:
    """profiles.py is imported BY converge; importing back would be a cycle,
    and would also let a validator reach an actuation verb."""
    source = (
        Path(profiles.__file__).read_text(encoding="utf-8")
        if profiles.__file__
        else ""  # pragma: no cover
    )
    for forbidden in ("converge", "scheduler", "service_controller", "store"):
        assert f"import {forbidden}" not in source, f"profiles.py must not import {forbidden}"
        assert f"from .{forbidden}" not in source, f"profiles.py must not import {forbidden}"
