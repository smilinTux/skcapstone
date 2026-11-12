from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from skcapstone.fleet import install_backends, installer, timer_enablement
from skcapstone.fleet.profile_doctor import DriftReport

CONTROL_PROFILE = (
    Path(__file__).resolve().parents[2] / "deploy" / "fleet-objects" / "profile" / "control.json"
)


class Systemd:
    def __init__(self, fragment: Path, *, enabled: bool = False, active: bool = False, fail=False):
        self.fragment = fragment
        self.enabled = enabled
        self.active = active
        self.fail = fail
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        verb = command[2]
        if verb == "show":
            output = (
                "LoadState=loaded\n"
                f"UnitFileState={'enabled' if self.enabled else 'disabled'}\n"
                f"ActiveState={'active' if self.active else 'inactive'}\n"
                f"SubState={'waiting' if self.active else 'dead'}\n"
                "Job=\n"
                f"FragmentPath={self.fragment}\n"
            )
            return CompletedProcess(command, 0, output, "")
        if verb == "enable":
            if self.fail:
                return CompletedProcess(command, 1, "", "failed")
            self.enabled = True
            link = self.fragment.parent / "timers.target.wants" / command[-1]
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(self.fragment)
        elif verb == "start":
            self.active = True
        elif verb == "stop":
            self.active = False
        elif verb == "disable":
            self.enabled = False
            if "--now" in command:
                self.active = False
        return CompletedProcess(command, 0, "", "")


class TransitionSystemd:
    """Model timer and service states independently for migration fences."""

    def __init__(
        self,
        *,
        timer_load="loaded",
        timer_file="disabled",
        timer_active="inactive",
        timer_substate=None,
        service_load="loaded",
        service_active="inactive",
        timer_job="",
        service_job="",
        launch_service_on_start=False,
        show_failure=False,
        disable_failure=False,
        stop_failure=False,
        config_home=None,
    ):
        self.timer_load = timer_load
        self.timer_file = timer_file
        self.timer_active = timer_active
        self.timer_substate = timer_substate
        self.service_load = service_load
        self.service_active = service_active
        self.timer_job = timer_job
        self.service_job = service_job
        self.launch_service_on_start = launch_service_on_start
        self.show_failure = show_failure
        self.disable_failure = disable_failure
        self.stop_failure = stop_failure
        self.config_home = config_home
        self.calls = []

    def __call__(self, command, **_kwargs):
        self.calls.append(command)
        verb = command[2]
        unit = command[3] if verb == "show" else command[-1]
        is_timer = unit.endswith(".timer")
        if verb == "show":
            if self.show_failure:
                return CompletedProcess(command, 1, "", "show failed")
            active = self.timer_active if is_timer else self.service_active
            substate = self.timer_substate if is_timer else None
            if substate is None:
                substate = "waiting" if is_timer and active == "active" else "dead"
            output = (
                f"LoadState={self.timer_load if is_timer else self.service_load}\n"
                f"UnitFileState={self.timer_file if is_timer else 'static'}\n"
                f"ActiveState={active}\n"
                f"SubState={substate}\n"
                f"Job={self.timer_job if is_timer else self.service_job}\n"
                "FragmentPath=/unit\n"
            )
            load = self.timer_load if is_timer else self.service_load
            return CompletedProcess(command, 4 if load == "not-found" else 0, output, "")
        if verb == "disable":
            if self.disable_failure:
                return CompletedProcess(command, 1, "", "unit not found")
            self.timer_file = "disabled"
            self.timer_active = "inactive"
        elif verb == "enable":
            self.timer_file = "enabled"
            if self.config_home is not None:
                link = self.config_home / "systemd/user/timers.target.wants" / unit
                link.parent.mkdir(parents=True, exist_ok=True)
                if not link.exists():
                    link.symlink_to("/unit")
        elif verb == "start":
            self.timer_active = "active"
            if self.launch_service_on_start:
                self.timer_substate = "running"
        elif verb == "stop":
            if self.stop_failure:
                return CompletedProcess(command, 1, "", "stop failed")
            self.service_active = "inactive"
        return CompletedProcess(command, 0, "", "")


def profile(required=True):
    timers = ["skfleet-link.timer"] if required else []
    return {"units": {"required": timers, "allowed": timers + ["skfleet-niobe-live.timer"]}}


def layout(tmp_path):
    config = tmp_path / ".config"
    fragment = config / "systemd" / "user" / "skfleet-link.timer"
    fragment.parent.mkdir(parents=True)
    fragment.write_text("[Timer]\n")
    return config, fragment


def test_repairs_missing_link_and_becomes_active_waiting(tmp_path):
    config, fragment = layout(tmp_path)
    systemd = Systemd(fragment)
    evidence = tmp_path / "evidence.jsonl"
    rows = timer_enablement.converge_required_timers(
        profile(),
        runner=systemd,
        config_home=config,
        evidence_path=evidence,
        actor="jarvis",
        source_revision="abc123",
    )
    assert rows == [{**rows[0], "enabled": True, "active_waiting": True, "drift": False}]
    assert [call[2] for call in systemd.calls].count("enable") == 1
    assert [call[2] for call in systemd.calls].count("start") == 1
    event = json.loads(evidence.read_text())
    assert event | {"timestamp": "ignored"} == {
        "actor": "jarvis",
        "prior_state": "disabled",
        "requested_state": "enabled",
        "result": "ok",
        "result_detail": "",
        "source_revision": "abc123",
        "timestamp": "ignored",
        "unit": "skfleet-link.timer",
    }


def test_second_run_is_noop_without_duplicate_evidence(tmp_path):
    config, fragment = layout(tmp_path)
    systemd = Systemd(fragment)
    evidence = tmp_path / "evidence.jsonl"
    kwargs = dict(runner=systemd, config_home=config, evidence_path=evidence, actor="jarvis")
    timer_enablement.converge_required_timers(profile(), **kwargs)
    first = evidence.read_text()
    timer_enablement.converge_required_timers(profile(), **kwargs)
    assert evidence.read_text() == first
    assert [call[2] for call in systemd.calls].count("enable") == 1


def test_running_timer_is_healthy_without_redundant_start(tmp_path):
    config, fragment = layout(tmp_path)
    link = config / "systemd/user/timers.target.wants/skfleet-link.timer"
    link.parent.mkdir(parents=True)
    link.symlink_to("/unit")
    systemd = TransitionSystemd(
        timer_file="enabled",
        timer_active="active",
        timer_substate="running",
    )

    rows = timer_enablement.converge_required_timers(
        profile(),
        runner=systemd,
        config_home=config,
        evidence_path=tmp_path / "evidence.jsonl",
        actor="jarvis",
    )

    assert rows[0]["converged"] is True
    assert rows[0]["active_waiting"] is True
    assert rows[0]["drift"] is False
    assert not any(call[2] == "start" for call in systemd.calls)


def test_waiting_timer_with_pending_job_fails_closed(tmp_path):
    config, fragment = layout(tmp_path)
    link = config / "systemd/user/timers.target.wants/skfleet-link.timer"
    link.parent.mkdir(parents=True)
    link.symlink_to("/unit")

    row = timer_enablement.audit_timer(
        "skfleet-link.timer",
        runner=TransitionSystemd(timer_file="enabled", timer_active="active", timer_job="41"),
        config_home=config,
    )

    assert row["active_waiting"] is False
    assert row["drift"] is True


def test_allowed_policy_gated_timer_is_never_mutated(tmp_path):
    config, fragment = layout(tmp_path)
    systemd = Systemd(fragment)
    evidence = tmp_path / "evidence.jsonl"
    assert (
        timer_enablement.converge_required_timers(
            profile(False),
            runner=systemd,
            config_home=config,
            evidence_path=evidence,
            actor="jarvis",
        )
        == []
    )
    assert systemd.calls == []
    assert not evidence.exists()


def test_required_timer_enable_and_start_flags_are_independent(tmp_path):
    config, fragment = layout(tmp_path)
    enable_only = Systemd(fragment)
    rows = timer_enablement.converge_required_timers(
        profile(),
        runner=enable_only,
        config_home=config,
        evidence_path=tmp_path / "enable.jsonl",
        actor="jarvis",
        enable=True,
        start=False,
    )
    assert rows[0]["converged"] is True
    assert "enable" in [call[2] for call in enable_only.calls]
    assert "start" not in [call[2] for call in enable_only.calls]

    config2, fragment2 = layout(tmp_path / "start")
    start_only = Systemd(fragment2)
    rows = timer_enablement.converge_required_timers(
        profile(),
        runner=start_only,
        config_home=config2,
        evidence_path=tmp_path / "start.jsonl",
        actor="jarvis",
        enable=False,
        start=True,
    )
    assert rows[0]["converged"] is True
    assert "enable" not in [call[2] for call in start_only.calls]
    assert "start" in [call[2] for call in start_only.calls]


def test_forbidden_pre_enabled_timers_stop_before_orchestrator_starts(tmp_path):
    """Rollout closes legacy recurrence before enabling the orchestrator."""

    config, fragment = layout(tmp_path)
    systemd = Systemd(fragment, enabled=True, active=True)
    evidence = tmp_path / "evidence.jsonl"
    policy = {
        "units": {
            "required": ["skfleet-seat-cycle.timer"],
            "mustNot": ["skfleet-tank.timer"],
        }
    }

    rows = timer_enablement.converge_forbidden_timers(
        policy,
        runner=systemd,
        config_home=config,
        evidence_path=evidence,
        actor="jarvis",
        source_revision="repair",
    )

    assert rows[0]["safe"] is True
    timer_enablement.converge_required_timers(
        policy,
        runner=systemd,
        config_home=config,
        evidence_path=evidence,
        actor="jarvis",
        source_revision="repair",
    )
    assert [call[2:] for call in systemd.calls if call[2] == "disable"] == [
        ["disable", "--now", "skfleet-tank.timer"]
    ]
    verbs = [call[2] for call in systemd.calls]
    assert (
        verbs.index("disable") < verbs.index("stop") < verbs.index("enable") < verbs.index("start")
    )
    assert [call[-1] for call in systemd.calls if call[2] == "stop"] == ["skfleet-tank.service"]
    event = json.loads(evidence.read_text().splitlines()[0])
    assert event["actor"] == "jarvis"
    assert event["requested_state"] == "disabled_inactive"


def test_control_profile_converges_all_serialized_seat_timers_inactive(tmp_path):
    """The shipped seat-cycle policy fences every independently scheduled seat."""

    profile = json.loads(CONTROL_PROFILE.read_text(encoding="utf-8"))["spec"]
    serialized = {
        "skfleet-atlas.timer",
        "skfleet-seraph.timer",
        "skfleet-niobe.timer",
        "skfleet-niobe-live.timer",
    }
    paused = {
        "skfleet-seat-cycle.timer",
        "skfleet-rotate.timer",
        "skfleet-estate-glm.timer",
        "skfleet-seraph-review-publisher.timer",
    }
    assert "skfleet-seat-cycle.timer" not in timer_enablement.required_timers(profile)
    assert (serialized | paused) <= set(timer_enablement.forbidden_timers(profile))

    systemd = TransitionSystemd(
        timer_file="enabled",
        timer_active="active",
        service_active="active",
    )
    rows = timer_enablement.converge_forbidden_timers(
        profile,
        runner=systemd,
        config_home=tmp_path,
        evidence_path=tmp_path / "evidence.jsonl",
        actor="jarvis",
        source_revision="a71a5d20",
    )

    assert serialized <= {row["unit"] for row in rows}
    assert serialized <= {call[-1] for call in systemd.calls if call[2] == "disable"}
    assert {timer.removesuffix(".timer") + ".service" for timer in serialized} <= {
        call[-1] for call in systemd.calls if call[2] == "stop"
    }


@pytest.mark.parametrize("service_state", ["active", "activating", "deactivating"])
def test_forbidden_service_is_stopped_even_when_timer_already_disabled(tmp_path, service_state):
    systemd = TransitionSystemd(service_active=service_state)
    rows = timer_enablement.converge_forbidden_timers(
        {"units": {"mustNot": ["skfleet-tank.timer"]}},
        runner=systemd,
        config_home=tmp_path,
        evidence_path=tmp_path / "evidence.jsonl",
        actor="jarvis",
    )
    assert [call[-1] for call in systemd.calls if call[2] == "stop"] == ["skfleet-tank.service"]
    assert rows[0]["safe"] is True


@pytest.mark.parametrize(
    ("systemd", "expected_safe"),
    [
        (TransitionSystemd(show_failure=True), False),
        (TransitionSystemd(service_active="active", stop_failure=True), False),
    ],
)
def test_forbidden_transition_fails_closed_on_unknown_or_failed_stop(
    tmp_path, systemd, expected_safe
):
    rows = timer_enablement.converge_forbidden_timers(
        {"units": {"mustNot": ["skfleet-tank.timer"]}},
        runner=systemd,
        config_home=tmp_path,
        evidence_path=tmp_path / "evidence.jsonl",
        actor="jarvis",
    )
    assert rows[0]["safe"] is expected_safe


def test_forbidden_enabled_runtime_timer_is_explicitly_disabled(tmp_path):
    systemd = TransitionSystemd(timer_file="enabled-runtime", timer_active="active")
    rows = timer_enablement.converge_forbidden_timers(
        {"units": {"mustNot": ["skfleet-tank.timer"]}},
        runner=systemd,
        config_home=tmp_path,
        evidence_path=tmp_path / "evidence.jsonl",
        actor="jarvis",
    )
    assert rows[0]["safe"] is True
    assert [call[2:] for call in systemd.calls if call[2] == "disable"] == [
        ["disable", "--now", "skfleet-tank.timer"]
    ]


def test_absent_forbidden_timer_is_safe_only_with_proven_inactive_service(tmp_path):
    """Fresh estates omit the legacy live timer but still prove its service stopped."""

    systemd = TransitionSystemd(
        timer_load="not-found",
        timer_file="",
        disable_failure=True,
        service_active="inactive",
    )
    rows = timer_enablement.converge_forbidden_timers(
        {"units": {"mustNot": ["skfleet-niobe-live.timer"]}},
        runner=systemd,
        config_home=tmp_path,
        evidence_path=tmp_path / "evidence.jsonl",
        actor="jarvis",
    )

    assert rows[0]["safe"] is True
    assert rows[0]["loaded"] is False
    assert [call[-1] for call in systemd.calls if call[2] == "stop"] == [
        "skfleet-niobe-live.service"
    ]


def test_absent_and_unknown_forbidden_timer_states_are_distinct(tmp_path):
    unknown = TransitionSystemd(show_failure=True, disable_failure=True)
    rows = timer_enablement.converge_forbidden_timers(
        {"units": {"mustNot": ["skfleet-niobe-live.timer"]}},
        runner=unknown,
        config_home=tmp_path,
        evidence_path=tmp_path / "evidence.jsonl",
        actor="jarvis",
    )

    assert rows[0]["safe"] is False


def test_authoritative_post_state_allows_absent_pair_and_benign_command_failure(tmp_path):
    systemd = TransitionSystemd(
        timer_load="not-found",
        timer_file="",
        service_load="not-found",
        disable_failure=True,
        stop_failure=True,
    )
    rows = timer_enablement.converge_forbidden_timers(
        {"units": {"mustNot": ["skfleet-seat-cycle.timer"]}},
        runner=systemd,
        config_home=tmp_path,
        evidence_path=tmp_path / "evidence.jsonl",
        actor="jarvis",
    )
    assert rows[0]["safe"] is True
    evidence = json.loads((tmp_path / "evidence.jsonl").read_text())
    assert evidence["disable_result"] == "failed"
    assert evidence["stop_result"] == "failed"


def test_authoritative_disabled_post_state_wins_over_disable_return_code(tmp_path):
    systemd = TransitionSystemd(disable_failure=True)
    rows = timer_enablement.converge_forbidden_timers(
        {"units": {"mustNot": ["skfleet-tank.timer"]}},
        runner=systemd,
        config_home=tmp_path,
        evidence_path=tmp_path / "evidence.jsonl",
        actor="jarvis",
    )
    assert rows[0]["safe"] is True


@pytest.mark.parametrize("state", ["active", "activating", "deactivating"])
def test_read_only_forbidden_audit_fails_closed_on_service_runtime_state(tmp_path, state):
    row = timer_enablement.audit_forbidden_timer(
        "skfleet-tank.timer", runner=TransitionSystemd(service_active=state)
    )
    assert row["safe"] is False


@pytest.mark.parametrize("state", ["active", "activating", "deactivating"])
def test_read_only_forbidden_audit_fails_closed_on_timer_runtime_state(tmp_path, state):
    row = timer_enablement.audit_forbidden_timer(
        "skfleet-tank.timer", runner=TransitionSystemd(timer_active=state)
    )
    assert row["safe"] is False


def test_read_only_forbidden_audit_distinguishes_absent_from_unknown(tmp_path):
    absent = TransitionSystemd(timer_load="not-found", service_load="not-found")
    assert (
        timer_enablement.audit_forbidden_timer("skfleet-seat-cycle.timer", runner=absent)["safe"]
        is True
    )
    assert (
        timer_enablement.audit_forbidden_timer(
            "skfleet-seat-cycle.timer", runner=TransitionSystemd(show_failure=True)
        )["safe"]
        is False
    )


@pytest.mark.parametrize(
    "systemd",
    [TransitionSystemd(timer_job="99"), TransitionSystemd(service_job="99")],
)
def test_forward_cutover_rejects_queued_forbidden_work(tmp_path, systemd):
    rows = timer_enablement.converge_forbidden_timers(
        {"units": {"mustNot": ["skfleet-tank.timer"]}},
        runner=systemd,
        config_home=tmp_path,
        evidence_path=tmp_path / "evidence.jsonl",
        actor="jarvis",
    )

    assert rows[0]["safe"] is False


def test_failed_mutation_is_recorded(tmp_path):
    config, fragment = layout(tmp_path)
    systemd = Systemd(fragment, fail=True)
    evidence = tmp_path / "evidence.jsonl"
    timer_enablement.converge_required_timers(
        profile(), runner=systemd, config_home=config, evidence_path=evidence, actor="jarvis"
    )
    assert json.loads(evidence.read_text())["result"] == "failed"


def test_disable_evidence_is_append_only_and_replayable(tmp_path):
    config, fragment = layout(tmp_path)
    systemd = Systemd(fragment, enabled=True)
    evidence = tmp_path / "evidence.jsonl"

    def fixed():
        return datetime(2026, 9, 13, tzinfo=timezone.utc)

    assert timer_enablement.mutate(
        "skfleet-link.timer",
        enabled=False,
        runner=systemd,
        evidence_path=evidence,
        actor="tank",
        source_revision="rev1",
        prior_state="enabled",
        now=fixed,
    )
    assert timer_enablement.mutate(
        "skfleet-link.timer",
        enabled=True,
        runner=systemd,
        evidence_path=evidence,
        actor="tank",
        source_revision="rev2",
        prior_state="disabled",
        now=fixed,
    )
    rows = [json.loads(line) for line in evidence.read_text().splitlines()]
    assert [row["requested_state"] for row in rows] == ["disabled", "enabled"]
    assert [row["source_revision"] for row in rows] == ["rev1", "rev2"]


def test_audit_reports_absent_or_wrong_wants_link_as_drift(tmp_path):
    config, fragment = layout(tmp_path)
    systemd = Systemd(fragment, enabled=True, active=True)
    assert (
        timer_enablement.audit_timer("skfleet-link.timer", runner=systemd, config_home=config)[
            "drift"
        ]
        is True
    )
    link = config / "systemd" / "user" / "timers.target.wants" / "skfleet-link.timer"
    link.parent.mkdir(parents=True)
    wrong = tmp_path / "wrong.timer"
    wrong.write_text("")
    link.symlink_to(wrong)
    assert (
        timer_enablement.audit_timer("skfleet-link.timer", runner=systemd, config_home=config)[
            "wants_link_ok"
        ]
        is False
    )


def test_installer_check_reports_missing_link_even_when_inventory_is_clean(tmp_path, monkeypatch):
    config, fragment = layout(tmp_path)
    systemd = Systemd(fragment, enabled=True, active=True)
    paths = type("Paths", (), {"root": tmp_path / "fleet"})()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(installer, "load_drift", lambda *args, **kwargs: DriftReport())
    monkeypatch.setattr(installer, "_profile_spec", lambda *args: profile())
    result = installer.run_install(
        paths,
        "control",
        node="node",
        mode="check",
        dry_run=False,
        enable=False,
        start=False,
        only=None,
        backends={},
        timer_runner=systemd,
    )
    assert result["ok"] is False
    assert result["results"] == [
        {
            "grade": "warn",
            "category": "missing_required_timer_enablement",
            "name": "skfleet-link.timer",
        }
    ]


def test_installer_check_reads_forbidden_timer_and_service_runtime_state(tmp_path, monkeypatch):
    paths = type("Paths", (), {"root": tmp_path / "fleet"})()
    policy = {"units": {"required": [], "mustNot": ["skfleet-tank.timer"]}}
    monkeypatch.setattr(installer, "load_drift", lambda *args, **kwargs: DriftReport())
    monkeypatch.setattr(installer, "_profile_spec", lambda *args: policy)

    result = installer.run_install(
        paths,
        "control",
        node="node",
        mode="check",
        dry_run=False,
        enable=False,
        start=False,
        only=None,
        backends={},
        timer_runner=TransitionSystemd(service_active="activating"),
    )

    assert result["ok"] is False
    assert result["results"] == [
        {
            "grade": "forbidden",
            "category": "unsafe_forbidden_timer_or_service",
            "name": "skfleet-tank.timer",
        }
    ]


def test_apply_refreshes_required_core_unit_bytes_even_without_inventory_drift(
    tmp_path, monkeypatch
):
    paths = type("Paths", (), {"root": tmp_path / "fleet"})()
    policy = {"units": {"required": ["skfleet-link.timer"], "mustNot": []}}
    monkeypatch.setattr(installer.store, "is_frozen", lambda paths: False)
    monkeypatch.setattr(installer.converge, "actuation_enabled", lambda paths, node: True)
    monkeypatch.setattr(installer, "load_drift", lambda *args, **kwargs: DriftReport())
    monkeypatch.setattr(installer, "_profile_spec", lambda *args: policy)
    calls = []

    def core(names, **kwargs):
        """Record the exact required core units selected for installation."""

        calls.append((names, kwargs))
        return "ok", ""

    result = installer.run_install(
        paths,
        "control",
        node="node",
        mode="apply",
        dry_run=False,
        enable=False,
        start=False,
        only=None,
        backends={"core": core},
    )

    assert result["ok"] is True
    assert calls == [(["skfleet-link.timer"], {"dry_run": False, "enable": False, "start": False})]


def test_full_control_apply_never_enables_or_installs_paused_dispatcher_units(
    tmp_path, monkeypatch
):
    """The shipped control profile no longer requires seat-cycle or its seats."""

    paths = type("Paths", (), {"root": tmp_path / "fleet"})()
    policy = json.loads(CONTROL_PROFILE.read_text(encoding="utf-8"))["spec"]
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(installer.store, "is_frozen", lambda paths: False)
    monkeypatch.setattr(installer.converge, "actuation_enabled", lambda paths, node: True)
    monkeypatch.setattr(installer, "load_drift", lambda *args, **kwargs: DriftReport())
    monkeypatch.setattr(installer, "_profile_spec", lambda *args: policy)
    events = []

    def core_runner(command, **_kwargs):
        events.append(("copy-or-activate", command))
        return CompletedProcess(command, 0, "", "")

    systemd = TransitionSystemd(config_home=tmp_path / "config")

    def timer_runner(command, **kwargs):
        events.append(("scheduler", command))
        return systemd(command, **kwargs)

    result = installer.run_install(
        paths,
        "control",
        node="node",
        mode="apply",
        dry_run=False,
        enable=True,
        start=True,
        only=None,
        backends=install_backends.default_backends(runner=core_runner),
        timer_runner=timer_runner,
    )

    assert result["ok"] is True
    paused = {
        "skfleet-atlas.service",
        "skfleet-atlas.timer",
        "skfleet-seat-cycle.timer",
        "skfleet-seat-cycle.service",
        "skfleet-rotate.timer",
        "skfleet-estate-glm.timer",
        "skfleet-seraph-review-publisher.timer",
    }
    assert not any(
        command[:3] == ["systemctl", "--user", verb] and command[-1] in paused
        for _phase, command in events
        for verb in ("enable", "start")
    )
    atlas_source = str(install_backends._packaged_core_unit("skfleet-atlas.service"))
    assert not any(command[4:5] == [atlas_source] for _phase, command in events)


def test_apply_preserves_activation_for_services_but_not_timers(tmp_path, monkeypatch):
    paths = type("Paths", (), {"root": tmp_path / "fleet"})()
    policy = {
        "units": {
            "required": ["skcapstone.service", "skfleet-link.timer"],
            "mustNot": [],
        }
    }
    monkeypatch.setattr(installer.store, "is_frozen", lambda paths: False)
    monkeypatch.setattr(installer.converge, "actuation_enabled", lambda paths, node: True)
    monkeypatch.setattr(installer, "load_drift", lambda *args, **kwargs: DriftReport())
    monkeypatch.setattr(installer, "_profile_spec", lambda *args: policy)
    calls = []

    def core(names, **kwargs):
        calls.append((names, kwargs))
        return "ok", ""

    installer.run_install(
        paths,
        "control",
        node="node",
        mode="apply",
        dry_run=False,
        enable=True,
        start=True,
        only=None,
        backends={"core": core},
        timer_runner=TransitionSystemd(config_home=tmp_path / "config"),
    )
    assert calls == [
        (["skcapstone.service"], {"dry_run": False, "enable": True, "start": True}),
        (["skfleet-link.timer"], {"dry_run": False, "enable": False, "start": False}),
    ]


def test_only_unrelated_service_does_not_touch_scheduler_fence(tmp_path, monkeypatch):
    paths = type("Paths", (), {"root": tmp_path / "fleet"})()
    policy = {
        "units": {
            "required": ["skgateway.service", "skfleet-seat-cycle.timer"],
            "mustNot": ["skfleet-tank.timer"],
        }
    }
    monkeypatch.setattr(installer.store, "is_frozen", lambda paths: False)
    monkeypatch.setattr(installer.converge, "actuation_enabled", lambda paths, node: True)
    monkeypatch.setattr(installer, "load_drift", lambda *args, **kwargs: DriftReport())
    monkeypatch.setattr(installer, "_profile_spec", lambda *args: policy)
    systemd = TransitionSystemd(show_failure=True)

    result = installer.run_install(
        paths,
        "control",
        node="node",
        mode="apply",
        dry_run=False,
        enable=True,
        start=True,
        only=["skgateway.service"],
        backends={"core": lambda names, **kwargs: ("ok", "")},
        timer_runner=systemd,
    )

    assert result["ok"] is True
    assert systemd.calls == []


def test_scheduler_dry_run_reports_complete_cutover_without_systemd_calls(tmp_path, monkeypatch):
    paths = type("Paths", (), {"root": tmp_path / "fleet"})()
    policy = {
        "units": {
            "required": ["skfleet-seat-cycle.timer"],
            "mustNot": [
                "skfleet-tank.timer",
                "skfleet-seraph.timer",
                "skfleet-niobe.timer",
                "skfleet-niobe-live.timer",
            ],
        }
    }
    monkeypatch.setattr(installer.store, "is_frozen", lambda paths: False)
    monkeypatch.setattr(installer.converge, "actuation_enabled", lambda paths, node: True)
    monkeypatch.setattr(installer, "load_drift", lambda *args, **kwargs: DriftReport())
    monkeypatch.setattr(installer, "_profile_spec", lambda *args: policy)
    calls = []
    result = installer.run_install(
        paths,
        "control",
        node="node",
        mode="apply",
        dry_run=True,
        enable=True,
        start=True,
        only=None,
        backends={"core": lambda names, **kwargs: ("would-write", "copy units")},
        timer_runner=lambda *args, **kwargs: calls.append(args),
    )
    details = "\n".join(row["detail"] for row in result["results"])
    for timer in policy["units"]["mustNot"]:
        assert f"disable --now {timer}" in details
        assert f"stop {timer.removesuffix('.timer')}.service" in details
    assert "enable skfleet-seat-cycle.timer" in details
    assert "start skfleet-seat-cycle.timer" in details
    assert calls == []


def test_check_rejects_mutation_flags_and_apply_rejects_unknown_only(tmp_path, monkeypatch):
    paths = type("Paths", (), {"root": tmp_path / "fleet"})()
    policy = {"units": {"required": ["skgateway.service"], "mustNot": []}}
    monkeypatch.setattr(installer, "_profile_spec", lambda *args: policy)
    with pytest.raises(ValueError, match="check mode"):
        installer.run_install(
            paths,
            "control",
            node="node",
            mode="check",
            dry_run=False,
            enable=True,
            start=False,
            only=None,
            backends={},
        )
    monkeypatch.setattr(installer.store, "is_frozen", lambda paths: False)
    monkeypatch.setattr(installer.converge, "actuation_enabled", lambda paths, node: True)
    with pytest.raises(ValueError, match="unknown --only"):
        installer.run_install(
            paths,
            "control",
            node="node",
            mode="apply",
            dry_run=False,
            enable=False,
            start=False,
            only=["not-a-profile-unit.service"],
            backends={},
        )


def test_installer_apply_repairs_enablement_without_backend_enable(tmp_path, monkeypatch):
    config, fragment = layout(tmp_path)
    systemd = Systemd(fragment)
    paths = type("Paths", (), {"root": tmp_path / "fleet"})()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(installer.store, "is_frozen", lambda paths: False)
    monkeypatch.setattr(installer.converge, "actuation_enabled", lambda paths, node: True)
    monkeypatch.setattr(
        installer,
        "load_drift",
        lambda *args, **kwargs: DriftReport(missing_required_units=["skfleet-link.timer"]),
    )
    monkeypatch.setattr(installer, "_profile_spec", lambda *args: profile())
    backend_calls = []

    def backend(names, **kwargs):
        backend_calls.append((names, kwargs))
        return "ok", ""

    result = installer.run_install(
        paths,
        "control",
        node="node",
        mode="apply",
        dry_run=False,
        enable=True,
        start=True,
        only=None,
        backends={"core": backend},
        timer_runner=systemd,
    )
    assert result["ok"] is True
    assert backend_calls[0][1]["enable"] is False
    assert [call[2] for call in systemd.calls].count("enable") == 1
    assert (tmp_path / "evidence" / "timer-enablement.jsonl").is_file()


def test_installer_accepts_timer_that_immediately_launches_oneshot(tmp_path, monkeypatch):
    config = tmp_path / "config"
    systemd = TransitionSystemd(config_home=config, launch_service_on_start=True)
    paths = type("Paths", (), {"root": tmp_path / "fleet"})()
    policy = {"units": {"required": ["skfleet-seat-cycle.timer"], "mustNot": []}}
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setattr(installer.store, "is_frozen", lambda paths: False)
    monkeypatch.setattr(installer.converge, "actuation_enabled", lambda paths, node: True)
    monkeypatch.setattr(installer, "load_drift", lambda *args, **kwargs: DriftReport())
    monkeypatch.setattr(installer, "_profile_spec", lambda *args: policy)

    result = installer.run_install(
        paths,
        "control",
        node="node",
        mode="apply",
        dry_run=False,
        enable=True,
        start=True,
        only=None,
        backends={"core": lambda names, **kwargs: ("ok", "")},
        timer_runner=systemd,
    )

    assert result["ok"] is True
    assert [call[2] for call in systemd.calls].count("start") == 1
