import hashlib
import json
import subprocess
import sys
import time
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
        "SKFLEET_ESC_TARGET": "0",
        "SKFLEET_CODEX_LANE_MODEL": "route-primary",
        "SKFLEET_CODEX_CAPACITY_DOMAINS": "capacity-primary",
    }.items():
        monkeypatch.setenv(key, value)


@pytest.fixture(autouse=True)
def estate_record(tmp_path: Path, monkeypatch) -> None:
    """Give every case an estate that names its operator and product scope."""
    path = tmp_path / "home/config/estate.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ESTATE))
    gateway_revision = write_lane_health(
        tmp_path, ("codex", "route-primary", ["capacity-primary"])
    )
    monkeypatch.setenv("SKFLEET_GATEWAY_URL", "http://gateway.test:18790")
    monkeypatch.setattr(
        "skcapstone.niobe_live_entrypoint.active_gateway_revision",
        lambda endpoint: gateway_revision,
    )


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


def write_lane_health(tmp_path: Path, *lanes: tuple[str, str, list[str]]) -> str:
    revision = "1" * 40
    path = tmp_path / "home/evidence/fleet-lane-health.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "cycle_id": "healthy-cycle",
                "observed_at": time.time(),
                "endpoint": "http://gateway.test:18790",
                "runtime_revision": revision,
                "errors": [],
                "lanes": [
                    {
                        "lane": lane,
                        "model": model,
                        "endpoint": "http://gateway.test:18790",
                        "capacity_domains": domains,
                        "domains": [
                            {"capacity_domain": domain, "state": "healthy", "max": 8}
                            for domain in domains
                        ],
                    }
                    for lane, model, domains in lanes
                ],
            }
        )
    )
    return revision


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

    def run_dispatcher(command, check, env):
        calls.append((command, check, env))
        evidence = (
            tmp_path / "home/evidence/fleet-rotation" / env["SKFLEET_ROTATION_ID"] / "actions.log"
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
    assert calls[0][:2] == ([sys.executable, str(dispatcher), "--go"], False)
    assert calls[0][2]["SKFLEET_NIOBE_ACTIVATION"] == str(path.resolve())
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


def test_live_wrapper_refuses_unhealthy_non_codex_lane(tmp_path: Path, monkeypatch) -> None:
    revision = approval_card(tmp_path)
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation(revision)))
    monkeypatch.setenv("SKFLEET_GLM_TARGET", "2")
    monkeypatch.setenv("SKFLEET_GLM_MODEL", "route-secondary")
    monkeypatch.setenv("SKFLEET_GLM_CAPACITY_DOMAINS", "capacity-secondary")
    with pytest.raises(ValueError, match="glm lane is not healthy: unknown"):
        run_live(activation_path=path, dispatcher=tmp_path, local_host="chiap08")


def test_live_wrapper_passes_healthy_mixed_lane_targets_unchanged(
    tmp_path: Path, monkeypatch
) -> None:
    revision = approval_card(tmp_path)
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation(revision)))
    dispatcher = tmp_path / "skfleet-rotate.py"
    dispatcher.write_text("#!/bin/sh\n")
    monkeypatch.setenv("SKFLEET_TARGET", "1")
    monkeypatch.setenv("SKFLEET_GLM_TARGET", "2")
    monkeypatch.setenv("SKFLEET_QWEN_TARGET", "1")
    monkeypatch.setenv("SKFLEET_GLM_MODEL", "route-secondary")
    monkeypatch.setenv("SKFLEET_GLM_CAPACITY_DOMAINS", "capacity-secondary")
    monkeypatch.setenv("SKFLEET_QWEN_MODEL", "route-local")
    monkeypatch.setenv("SKFLEET_QWEN_CAPACITY_DOMAINS", "capacity-local-a,capacity-local-b")
    monkeypatch.setenv("SKFLEET_GATEWAY_URL", "http://gateway.test:18790")
    gateway_revision = write_lane_health(
        tmp_path,
        ("codex", "route-primary", ["capacity-primary"]),
        ("glm", "route-secondary", ["capacity-secondary"]),
        ("qwen", "route-local", ["capacity-local-a", "capacity-local-b"]),
    )
    monkeypatch.setattr(
        "skcapstone.niobe_live_entrypoint.active_gateway_revision",
        lambda endpoint: gateway_revision,
        raising=False,
    )
    monkeypatch.setattr("skcapstone.niobe_live_entrypoint.startup_hello", lambda *a, **k: True)
    mailbox = SimpleNamespace(as_dict=lambda: {"mailbox_ok": True})
    monkeypatch.setattr("skcapstone.niobe_live_entrypoint.poll_mail", lambda *a, **k: mailbox)
    calls = []

    def run_dispatcher(command, check, env):
        calls.append(env)
        evidence = (
            tmp_path / "home/evidence/fleet-rotation" / env["SKFLEET_ROTATION_ID"] / "actions.log"
        )
        evidence.parent.mkdir(parents=True)
        evidence.write_text("NOOP_RECEIPT|chiap08|reason=rotation_overlap|seat=niobe\n")
        return SimpleNamespace(returncode=0)

    assert (
        run_live(
            activation_path=path,
            dispatcher=dispatcher,
            local_host="chiap08",
            runner=run_dispatcher,
        )
        == 0
    )
    assert calls[0]["SKFLEET_TARGET"] == "1"
    assert calls[0]["SKFLEET_GLM_TARGET"] == "2"
    assert calls[0]["SKFLEET_QWEN_TARGET"] == "1"


def test_live_wrapper_accepts_healthy_kimi_and_escalation_routes(
    tmp_path: Path, monkeypatch
) -> None:
    revision = approval_card(tmp_path)
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation(revision)))
    dispatcher = tmp_path / "skfleet-rotate.py"
    dispatcher.write_text("#!/bin/sh\n")
    monkeypatch.setenv("SKFLEET_TARGET", "0")
    monkeypatch.setenv("SKFLEET_KIMI_TARGET", "1")
    monkeypatch.setenv("SKFLEET_KIMI_MODEL", "route-economy")
    monkeypatch.setenv("SKFLEET_KIMI_CAPACITY_DOMAINS", "capacity-economy")
    monkeypatch.setenv("SKFLEET_ESC_TARGET", "1")
    monkeypatch.setenv("SKFLEET_ESC_MODEL", "route-escalation")
    monkeypatch.setenv("SKFLEET_ESC_CAPACITY_DOMAINS", "capacity-escalation")
    gateway_revision = write_lane_health(
        tmp_path,
        ("kimi", "route-economy", ["capacity-economy"]),
        ("escalate", "route-escalation", ["capacity-escalation"]),
    )
    monkeypatch.setattr(
        "skcapstone.niobe_live_entrypoint.active_gateway_revision",
        lambda endpoint: gateway_revision,
    )
    monkeypatch.setattr("skcapstone.niobe_live_entrypoint.startup_hello", lambda *a, **k: True)
    mailbox = SimpleNamespace(as_dict=lambda: {"mailbox_ok": True})
    monkeypatch.setattr("skcapstone.niobe_live_entrypoint.poll_mail", lambda *a, **k: mailbox)
    calls = []

    def run_dispatcher(command, check, env):
        calls.append(env)
        evidence = (
            tmp_path / "home/evidence/fleet-rotation" / env["SKFLEET_ROTATION_ID"] / "actions.log"
        )
        evidence.parent.mkdir(parents=True)
        evidence.write_text("NOOP_RECEIPT|test-host|reason=rotation_overlap|seat=niobe\n")
        return SimpleNamespace(returncode=0)

    assert (
        run_live(
            activation_path=path,
            dispatcher=dispatcher,
            local_host="chiap08",
            runner=run_dispatcher,
        )
        == 0
    )
    assert calls[0]["SKFLEET_KIMI_TARGET"] == "1"
    assert calls[0]["SKFLEET_ESC_TARGET"] == "1"


def test_live_wrapper_rejects_unhealthy_escalation_before_launch(
    tmp_path: Path, monkeypatch
) -> None:
    revision = approval_card(tmp_path)
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation(revision)))
    monkeypatch.setenv("SKFLEET_TARGET", "0")
    monkeypatch.setenv("SKFLEET_ESC_TARGET", "1")
    monkeypatch.setenv("SKFLEET_ESC_MODEL", "route-escalation")
    monkeypatch.setenv("SKFLEET_ESC_CAPACITY_DOMAINS", "capacity-escalation")
    calls = []

    with pytest.raises(ValueError, match="escalate lane is not healthy: unknown"):
        run_live(
            activation_path=path,
            dispatcher=tmp_path,
            local_host="chiap08",
            runner=lambda *args, **kwargs: calls.append((args, kwargs)),
        )
    assert calls == []


def test_live_wrapper_requires_active_lane_bindings(tmp_path: Path, monkeypatch) -> None:
    revision = approval_card(tmp_path)
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation(revision)))
    monkeypatch.delenv("SKFLEET_CODEX_LANE_MODEL")

    with pytest.raises(ValueError, match="codex model binding is missing"):
        run_live(activation_path=path, dispatcher=tmp_path, local_host="chiap08")


def test_live_wrapper_requires_active_lane_capacity_domains(tmp_path: Path, monkeypatch) -> None:
    revision = approval_card(tmp_path)
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation(revision)))
    monkeypatch.delenv("SKFLEET_CODEX_CAPACITY_DOMAINS")

    with pytest.raises(ValueError, match="codex capacity domains are missing"):
        run_live(activation_path=path, dispatcher=tmp_path, local_host="chiap08")


def test_live_wrapper_requires_configured_gateway_endpoint(tmp_path: Path, monkeypatch) -> None:
    revision = approval_card(tmp_path)
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation(revision)))
    monkeypatch.delenv("SKFLEET_GATEWAY_URL")

    with pytest.raises(ValueError, match="gateway endpoint is missing"):
        run_live(activation_path=path, dispatcher=tmp_path, local_host="chiap08")


@pytest.mark.parametrize("value", ["-1", "one", "1.0", "+1"])
def test_live_wrapper_rejects_invalid_targets(tmp_path: Path, monkeypatch, value: str) -> None:
    revision = approval_card(tmp_path)
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation(revision)))
    monkeypatch.setenv("SKFLEET_QWEN_TARGET", value)

    with pytest.raises(ValueError, match="qwen target must be a nonnegative integer"):
        run_live(activation_path=path, dispatcher=tmp_path, local_host="chiap08")


def test_live_wrapper_rejects_gateway_revision_mismatch(tmp_path: Path, monkeypatch) -> None:
    revision = approval_card(tmp_path)
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation(revision)))
    monkeypatch.setattr(
        "skcapstone.niobe_live_entrypoint.active_gateway_revision",
        lambda endpoint: "2" * 40,
    )

    with pytest.raises(ValueError, match="codex lane is not healthy: revision-mismatch"):
        run_live(activation_path=path, dispatcher=tmp_path, local_host="chiap08")


def test_live_wrapper_rejects_capacity_domain_mismatch(tmp_path: Path, monkeypatch) -> None:
    revision = approval_card(tmp_path)
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation(revision)))
    monkeypatch.setenv("SKFLEET_CODEX_CAPACITY_DOMAINS", "other")

    with pytest.raises(ValueError, match="codex lane is not healthy: capacity-mismatch"):
        run_live(activation_path=path, dispatcher=tmp_path, local_host="chiap08")


def test_live_wrapper_rejects_kimi_with_unknown_health_before_launch(
    tmp_path: Path, monkeypatch
) -> None:
    revision = approval_card(tmp_path)
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation(revision)))
    monkeypatch.setenv("SKFLEET_TARGET", "0")
    monkeypatch.setenv("SKFLEET_KIMI_TARGET", "1")
    monkeypatch.setenv("SKFLEET_KIMI_MODEL", "route-economy")
    monkeypatch.setenv("SKFLEET_KIMI_CAPACITY_DOMAINS", "capacity-economy")
    calls = []

    with pytest.raises(ValueError, match="kimi lane is not healthy: unknown"):
        run_live(
            activation_path=path,
            dispatcher=tmp_path,
            local_host="chiap08",
            runner=lambda *args, **kwargs: calls.append((args, kwargs)),
        )
    assert calls == []


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
        evidence = tmp_path / "home/evidence/fleet-rotation" / ("1" * 32) / "actions.log"
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
        evidence = tmp_path / "home/evidence/fleet-rotation" / cycle_id / "actions.log"
        evidence.parent.mkdir(parents=True)
        evidence.write_text(
            "NOOP_RECEIPT|chiap08|reason=rotation_overlap|seat=niobe\nUNTERMINATED_WORK|chiap08\n"
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
    [OSError("unavailable"), subprocess.TimeoutExpired("dispatcher", 1)],
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

    def fail(*args, **kwargs):
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
    assert receipt["result"] == "dispatch_failed"
    assert receipt["exception_type"] == type(error).__name__


def test_live_wrapper_refuses_stale_well_formed_card_revision(tmp_path: Path) -> None:
    approval_card(tmp_path)
    path = tmp_path / "home/coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation("b" * 64)))
    dispatcher = tmp_path / "skfleet-rotate.py"
    dispatcher.write_text("#!/bin/sh\n")
    with pytest.raises(ValueError, match="revision is stale"):
        run_live(activation_path=path, dispatcher=dispatcher, local_host="chiap08")
