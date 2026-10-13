import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone.niobe_live_entrypoint import run_live

#: The estate these tests run as. The wrapper reads the operator and the
#: product scope from the estate's own synced record, so the fixture below
#: writes one instead of the module carrying a hardcoded operator.
ESTATE = {
    "schema": "sk.estate-authority/v1",
    "operator": "casey",
    "realm": "skworld.io",
    "product_scope": ["skcapstone", "skdashboard", "skworld"],
}


@pytest.fixture(autouse=True)
def codex_only_environment(monkeypatch) -> None:
    for key, value in {
        "SKFLEET_TARGET": "3",
        "SKFLEET_QWEN_TARGET": "0",
        "SKFLEET_GLM_TARGET": "0",
        "SKFLEET_KIMI_TARGET": "0",
    }.items():
        monkeypatch.setenv(key, value)


@pytest.fixture(autouse=True)
def estate_record(tmp_path: Path) -> None:
    """Give every case an estate that names its operator and product scope."""
    path = tmp_path / "home/config/estate.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ESTATE))


def approval_card(tmp_path: Path, card_id: str = "c4e7a9b2") -> str:
    """Create the approval card the activation fences against."""
    core = tmp_path / "home/cards" / card_id / "core.json"
    core.parent.mkdir(parents=True, exist_ok=True)
    core.write_text('{"id":"c4e7a9b2"}\n')
    return hashlib.sha256(core.read_bytes()).hexdigest()


def activation(card_revision: str = "a" * 64) -> dict:
    return {
        "schema": "skfleet.niobe-activation/v1",
        "state": "active",
        "seat": "niobe",
        "decision_id": "casey-c4e7a9b2-20260906",
        "authorized_by": "casey",
        "card_id": "c4e7a9b2",
        "card_revision": card_revision,
        "host": "chiap08",
        "live_unit": "skfleet-niobe-live.timer",
        "product_scope": ["skcapstone", "skdashboard", "skworld"],
        "card_label": "seat-niobe",
        "allowed_actions": ["claim", "release", "launch", "stop", "reassign"],
        "denied_actions": ["merge", "deploy", "application_actuation", "external_dispatch"],
        "expires_at": "2099-10-06T22:00:00+00:00",
        "rollback": {
            "owner": "casey",
            "action": "disable_skfleet-niobe-live.timer_enable_skfleet-niobe-shadow.timer",
        },
    }


def test_live_wrapper_validates_then_runs_exact_dispatcher(tmp_path: Path, monkeypatch) -> None:
    revision = approval_card(tmp_path)
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation(revision)))
    dispatcher = tmp_path / "skfleet-rotate.py"
    dispatcher.write_text("#!/bin/sh\n")
    calls = []
    monkeypatch.setattr("skcapstone.niobe_live_entrypoint.startup_hello", lambda *a, **k: True)
    mailbox = SimpleNamespace(as_dict=lambda: {"mailbox_ok": True})
    monkeypatch.setattr("skcapstone.niobe_live_entrypoint.poll_mail", lambda *a, **k: mailbox)

    def run_dispatcher(command, capture_output, text, check, env, timeout):
        calls.append((command, capture_output, text, check, env, timeout))
        evidence = (
            tmp_path
            / "home/evidence/fleet-rotation"
            / ("chiap08-" + env["SKFLEET_ROTATION_ID"])
            / "actions.log"
        )
        evidence.parent.mkdir(parents=True)
        evidence.write_text(
            "SLOTS|chiap08|codex=2/3 glm=0/0|total_free=1\n"
            "LAUNCHED|chiap08|session|1234abcd|lane=codex|model=sk-codex-mid\n"
            "CYCLE_RECEIPT|chiap08|seat=niobe|launched=1|attempted=1|receipts=1\n"
        )
        return SimpleNamespace(returncode=0)

    rc = run_live(
        activation_path=path,
        dispatcher=dispatcher,
        local_host="chiap08",
        runner=run_dispatcher,
    )
    assert rc == 0
    assert calls[0][:4] == ([sys.executable, str(dispatcher), "--go"], True, True, False)
    assert calls[0][4]["SKFLEET_NIOBE_ACTIVATION"] == str(path.resolve())
    assert calls[0][5] == 270
    receipt = json.loads(
        (tmp_path / "home/coordination/seat-cycles/niobe.health.jsonl").read_text()
    )
    assert receipt["result"] == "launch"
    assert receipt["dispatcher_returncode"] == 0
    assert receipt["activation_decision"] == "casey-c4e7a9b2-20260906"
    assert receipt["mailbox_ok"] is True
    assert receipt["launches"] == 1
    assert receipt["slot_summary"].startswith("SLOTS|chiap08|")
    assert len(receipt["rotation_evidence_sha256"]) == 64


def test_live_wrapper_refuses_wrong_host(tmp_path: Path) -> None:
    """An activation minted for one host does not authorize another."""
    revision = approval_card(tmp_path)
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation(revision)))
    dispatcher = tmp_path / "skfleet-rotate.py"
    dispatcher.write_text("#!/bin/sh\n")
    with pytest.raises(ValueError, match="this machine is chiap01"):
        run_live(activation_path=path, dispatcher=dispatcher, local_host="chiap01")


def test_live_wrapper_refuses_an_operator_the_estate_does_not_name(tmp_path: Path) -> None:
    revision = approval_card(tmp_path)
    record = activation(revision)
    record["authorized_by"] = "jarvis"
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record))
    dispatcher = tmp_path / "skfleet-rotate.py"
    dispatcher.write_text("#!/bin/sh\n")
    with pytest.raises(ValueError, match="requires casey authorization"):
        run_live(activation_path=path, dispatcher=dispatcher, local_host="chiap08")


def test_live_wrapper_refuses_non_codex_effective_environment(tmp_path: Path, monkeypatch) -> None:
    revision = approval_card(tmp_path)
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation(revision)))
    monkeypatch.setenv("SKFLEET_GLM_TARGET", "2")
    with pytest.raises(ValueError, match="Codex-only target 3"):
        run_live(activation_path=path, dispatcher=tmp_path, local_host="chiap08")


def test_live_wrapper_records_dispatch_failure(tmp_path: Path, monkeypatch) -> None:
    revision = approval_card(tmp_path)
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation(revision)))
    dispatcher = tmp_path / "skfleet-rotate.py"
    dispatcher.write_text("#!/bin/sh\n")
    mailbox = SimpleNamespace(as_dict=lambda: {"mailbox_ok": True})
    monkeypatch.setattr("skcapstone.niobe_live_entrypoint.startup_hello", lambda *a, **k: True)
    monkeypatch.setattr("skcapstone.niobe_live_entrypoint.poll_mail", lambda *a, **k: mailbox)

    rc = run_live(
        activation_path=path,
        dispatcher=dispatcher,
        local_host="chiap08",
        runner=lambda *a, **k: SimpleNamespace(returncode=17),
    )

    assert rc == 17
    receipt = json.loads(
        (tmp_path / "home/coordination/seat-cycles/niobe.health.jsonl").read_text()
    )
    assert receipt["result"] == "dispatch_failed"
    assert receipt["reason"] == "dispatcher_exit_17"
    assert receipt["dispatcher_returncode"] == 17


def test_live_wrapper_deduplicates_one_systemd_invocation(tmp_path: Path, monkeypatch) -> None:
    revision = approval_card(tmp_path)
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation(revision)))
    dispatcher = tmp_path / "skfleet-rotate.py"
    dispatcher.write_text("#!/bin/sh\n")
    mailbox = SimpleNamespace(as_dict=lambda: {"mailbox_ok": True})
    monkeypatch.setenv("INVOCATION_ID", "1" * 32)
    monkeypatch.setattr("skcapstone.niobe_live_entrypoint.startup_hello", lambda *a, **k: True)
    monkeypatch.setattr("skcapstone.niobe_live_entrypoint.poll_mail", lambda *a, **k: mailbox)

    def runner(*args, **kwargs):
        evidence = (
            tmp_path / "home/evidence/fleet-rotation" / ("chiap08-" + "1" * 32) / "actions.log"
        )
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text("NOOP_RECEIPT|chiap08|reason=rotation_overlap|seat=niobe\n")
        return SimpleNamespace(returncode=0)

    for _ in range(2):
        assert (
            run_live(
                activation_path=path,
                dispatcher=dispatcher,
                local_host="chiap08",
                runner=runner,
            )
            == 0
        )

    receipts = (
        (tmp_path / "home/coordination/seat-cycles/niobe.health.jsonl").read_text().splitlines()
    )
    assert len(receipts) == 1


def test_live_wrapper_rejects_zero_exit_without_rotation_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    revision = approval_card(tmp_path)
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation(revision)))
    dispatcher = tmp_path / "skfleet-rotate.py"
    dispatcher.write_text("#!/bin/sh\n")
    mailbox = SimpleNamespace(as_dict=lambda: {"mailbox_ok": True})
    monkeypatch.setattr("skcapstone.niobe_live_entrypoint.startup_hello", lambda *a, **k: True)
    monkeypatch.setattr("skcapstone.niobe_live_entrypoint.poll_mail", lambda *a, **k: mailbox)

    rc = run_live(
        activation_path=path,
        dispatcher=dispatcher,
        local_host="chiap08",
        runner=lambda *a, **k: SimpleNamespace(returncode=0),
    )

    assert rc == 70
    receipt = json.loads(
        (tmp_path / "home/coordination/seat-cycles/niobe.health.jsonl").read_text()
    )
    assert receipt["result"] == "dispatch_failed"
    assert receipt["dispatcher_returncode"] == 70


def test_live_wrapper_rejects_unterminated_rotation_evidence(tmp_path: Path, monkeypatch) -> None:
    revision = approval_card(tmp_path)
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation(revision)))
    dispatcher = tmp_path / "skfleet-rotate.py"
    dispatcher.write_text("#!/bin/sh\n")
    mailbox = SimpleNamespace(as_dict=lambda: {"mailbox_ok": True})
    monkeypatch.setattr("skcapstone.niobe_live_entrypoint.startup_hello", lambda *a, **k: True)
    monkeypatch.setattr("skcapstone.niobe_live_entrypoint.poll_mail", lambda *a, **k: mailbox)

    def unterminated(*args, **kwargs):
        cycle_id = kwargs["env"]["SKFLEET_ROTATION_ID"]
        evidence = (
            tmp_path / "home/evidence/fleet-rotation" / ("chiap08-" + cycle_id) / "actions.log"
        )
        evidence.parent.mkdir(parents=True)
        evidence.write_text(
            "NOOP_RECEIPT|chiap08|reason=rotation_overlap|seat=niobe\n"
            "UNTERMINATED_WORK|chiap08\n"
        )
        return SimpleNamespace(returncode=0)

    assert (
        run_live(
            activation_path=path,
            dispatcher=dispatcher,
            local_host="chiap08",
            runner=unterminated,
        )
        == 70
    )


@pytest.mark.parametrize(
    "error",
    [OSError("unavailable")],
)
def test_live_wrapper_records_dispatch_exception(
    tmp_path: Path, monkeypatch, error: Exception
) -> None:
    revision = approval_card(tmp_path)
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation(revision)))
    dispatcher = tmp_path / "skfleet-rotate.py"
    dispatcher.write_text("#!/bin/sh\n")
    mailbox = SimpleNamespace(as_dict=lambda: {"mailbox_ok": True})
    monkeypatch.setattr("skcapstone.niobe_live_entrypoint.startup_hello", lambda *a, **k: True)
    monkeypatch.setattr("skcapstone.niobe_live_entrypoint.poll_mail", lambda *a, **k: mailbox)

    calls = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise error

    with pytest.raises(type(error)):
        run_live(
            activation_path=path,
            dispatcher=dispatcher,
            local_host="chiap08",
            runner=fail,
        )

    receipt = json.loads(
        (tmp_path / "home/coordination/seat-cycles/niobe.health.jsonl").read_text()
    )
    assert calls[0][1]["timeout"] == 270
    assert receipt["result"] == "dispatch_failed"
    assert receipt["exception_type"] == type(error).__name__


def test_live_wrapper_returns_auditable_timeout_with_diagnostics(
    tmp_path: Path, monkeypatch
) -> None:
    revision = approval_card(tmp_path)
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation(revision)))
    dispatcher = tmp_path / "skfleet-rotate.py"
    dispatcher.write_text("#!/bin/sh\n")
    mailbox = SimpleNamespace(as_dict=lambda: {"mailbox_ok": True})
    monkeypatch.setattr("skcapstone.niobe_live_entrypoint.startup_hello", lambda *a, **k: True)
    monkeypatch.setattr("skcapstone.niobe_live_entrypoint.poll_mail", lambda *a, **k: mailbox)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            args[0], kwargs["timeout"], output=b"POOL|ready=1\n", stderr=b"deadline\n"
        )

    assert (
        run_live(
            activation_path=path,
            dispatcher=dispatcher,
            local_host="chiap08",
            runner=timeout,
        )
        == 70
    )
    receipt = json.loads(
        (tmp_path / "home/coordination/seat-cycles/niobe.health.jsonl").read_text()
    )
    assert receipt["result"] == "dispatch_failed"
    assert receipt["reason"] == "dispatcher_exit_70"
    assert receipt["exception_type"] == "TimeoutExpired"
    assert receipt["dispatcher_stdout"] == "POOL|ready=1\n"
    assert receipt["dispatcher_stderr"] == "deadline\n"


def test_live_timeout_terminates_and_reaps_dispatcher_process_group(
    tmp_path: Path, monkeypatch
) -> None:
    revision = approval_card(tmp_path)
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation(revision)))
    child_pid = tmp_path / "child.pid"
    dispatcher = tmp_path / "dispatcher.py"
    dispatcher.write_text(
        "import pathlib, subprocess, time\n"
        f"child = subprocess.Popen(['sleep', '30'])\n"
        f"pathlib.Path({str(child_pid)!r}).write_text(str(child.pid))\n"
        "print('partial stdout', flush=True)\n"
        "time.sleep(30)\n"
    )
    mailbox = SimpleNamespace(as_dict=lambda: {"mailbox_ok": True})
    monkeypatch.setattr("skcapstone.niobe_live_entrypoint.startup_hello", lambda *a, **k: True)
    monkeypatch.setattr("skcapstone.niobe_live_entrypoint.poll_mail", lambda *a, **k: mailbox)
    monkeypatch.setattr("skcapstone.niobe_live_entrypoint._DISPATCH_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr("skcapstone.niobe_live_entrypoint._TERMINATE_GRACE_SECONDS", 0.2)

    assert run_live(activation_path=path, dispatcher=dispatcher, local_host="chiap08") == 70

    pid = int(child_pid.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    receipt = json.loads(
        (tmp_path / "home/coordination/seat-cycles/niobe.health.jsonl").read_text()
    )
    assert receipt["exception_type"] == "TimeoutExpired"
    assert receipt["dispatcher_stdout"] == "partial stdout\n"


def test_live_wrapper_refuses_stale_well_formed_card_revision(tmp_path: Path) -> None:
    approval_card(tmp_path)
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation("b" * 64)))
    dispatcher = tmp_path / "skfleet-rotate.py"
    dispatcher.write_text("#!/bin/sh\n")
    with pytest.raises(ValueError, match="revision is stale"):
        run_live(activation_path=path, dispatcher=dispatcher, local_host="chiap08")


def _timeout_setup(tmp_path: Path, monkeypatch):
    revision = approval_card(tmp_path)
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation(revision)))
    dispatcher = tmp_path / "skfleet-rotate.py"
    dispatcher.write_text("#!/bin/sh\n")
    mailbox = SimpleNamespace(as_dict=lambda: {"mailbox_ok": True})
    monkeypatch.setattr("skcapstone.niobe_live_entrypoint.startup_hello", lambda *a, **k: True)
    monkeypatch.setattr("skcapstone.niobe_live_entrypoint.poll_mail", lambda *a, **k: mailbox)
    return path, dispatcher


def test_timeout_records_phase_in_progress_and_last_phase_timings(
    tmp_path: Path, monkeypatch
) -> None:
    path, dispatcher = _timeout_setup(tmp_path, monkeypatch)
    seen = {}

    def timeout(*args, **kwargs):
        seen["timeout_env"] = kwargs["env"]["SKFLEET_DISPATCH_TIMEOUT_S"]
        evidence = (
            tmp_path
            / "home/evidence/fleet-rotation"
            / f"chiap08-{kwargs['env']['SKFLEET_ROTATION_ID']}/actions.log"
        )
        evidence.parent.mkdir(parents=True)
        evidence.write_text(
            "SLOTS|chiap08|x\n"
            "PHASE_TIMING|chiap08|phase=startup_slots|elapsed_s=3.0|phase_s=3.0\n"
            "PHASE_TIMING|chiap08|phase=startup_sweeps|elapsed_s=20.5|phase_s=17.5\n"
            "PHASE_DEADLINE|chiap08|pool_fold|done=900|remaining=40|elapsed_s=160.0|limit_s=162.0\n"
            "PHASE_BUDGET_WARN|chiap08|startup_sweeps|phase_s=17.5|share=0.07|warn_share=0.04|budget_s=250\n"
            "PHASE_TIMING|chiap08|phase=review_sweeps|elapsed_s=40.0|phase_s=19.5\n"
        )
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"], output=b"", stderr=b"x\n")

    assert (
        run_live(activation_path=path, dispatcher=dispatcher, local_host="chiap08", runner=timeout)
        == 70
    )
    receipt = json.loads(
        (tmp_path / "home/coordination/seat-cycles/niobe.health.jsonl").read_text()
    )
    assert seen["timeout_env"] == "270"
    assert receipt["timeout_phase"] == "pool_fold"
    assert [t["phase"] for t in receipt["last_phase_timings"]] == [
        "startup_slots",
        "startup_sweeps",
        "review_sweeps",
    ]
    assert receipt["last_phase_timings"][1]["phase_s"] == 17.5
    assert len(receipt["phase_alerts"]) == 2
    assert receipt["phase_alerts"][0].startswith("PHASE_DEADLINE|chiap08|pool_fold")


def test_timeout_without_actions_log_still_names_the_gap(tmp_path: Path, monkeypatch) -> None:
    path, dispatcher = _timeout_setup(tmp_path, monkeypatch)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"], output=b"", stderr=b"")

    assert (
        run_live(activation_path=path, dispatcher=dispatcher, local_host="chiap08", runner=timeout)
        == 70
    )
    receipt = json.loads(
        (tmp_path / "home/coordination/seat-cycles/niobe.health.jsonl").read_text()
    )
    assert receipt["timeout_phase"] == "unknown_no_actions_log"
    assert receipt["last_phase_timings"] == []


def test_timeout_after_launch_phase_reports_final_receipt(tmp_path: Path) -> None:
    from skcapstone.niobe_live_entrypoint import timeout_diagnostics

    log = tmp_path / "actions.log"
    log.write_text("PHASE_TIMING|h|phase=lane_launch|elapsed_s=240.0|phase_s=30.0\n")
    assert timeout_diagnostics(log)["timeout_phase"] == "final_receipt"
    log.write_text("garbage\n")
    assert timeout_diagnostics(log)["timeout_phase"] == "before_startup_slots"


def test_timeout_keeps_last_stderr_after_deduplicating_repeated_noise(
    tmp_path: Path, monkeypatch
) -> None:
    path, dispatcher = _timeout_setup(tmp_path, monkeypatch)
    noise = "".join(f"WARN fold card {i % 3}\nWARN other {i % 2}\n" for i in range(3000))
    stderr = noise + "Traceback (most recent call last):\nsubprocess.TimeoutExpired in pool_fold\n"

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            args[0], kwargs["timeout"], output=b"", stderr=stderr.encode()
        )

    assert (
        run_live(activation_path=path, dispatcher=dispatcher, local_host="chiap08", runner=timeout)
        == 70
    )
    receipt = json.loads(
        (tmp_path / "home/coordination/seat-cycles/niobe.health.jsonl").read_text()
    )
    text = receipt["dispatcher_stderr"]
    assert text.endswith("subprocess.TimeoutExpired in pool_fold\n")
    assert "[deduplicated 6002 lines to 7]" in text
    assert "WARN fold card 0  [x1000]" in text
