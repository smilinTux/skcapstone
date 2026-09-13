from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from subprocess import CompletedProcess

from skcapstone.fleet import installer, timer_enablement
from skcapstone.fleet.profile_doctor import DriftReport


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
        elif verb == "disable":
            self.enabled = False
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
