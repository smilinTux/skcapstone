"""Tests for the sknoded verification gate (permissive-then-enforce)."""

from __future__ import annotations

import hashlib
from subprocess import CompletedProcess

import pytest

from skcapstone.fleet import backoff, converge, events, signing, store

NODE = "node-41"
SHOW = (
    "systemctl --user show skgateway.service "
    "--property=LoadState,ActiveState,MainPID,ActiveEnterTimestamp"
)
FAILED = (0, "LoadState=loaded\nActiveState=failed\nMainPID=0\nActiveEnterTimestamp=\n")


def fake_signer(data: bytes) -> str:
    return "sig:" + hashlib.sha256(data).hexdigest()


def fake_verifier(data: bytes, sig: str) -> bool:
    return sig == "sig:" + hashlib.sha256(data).hexdigest()


class FakeRunner:
    def __init__(self, replies) -> None:
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


@pytest.fixture(autouse=True)
def _fresh():
    events.reset_dedupe()
    backoff.reset_trackers()
    yield
    events.reset_dedupe()
    backoff.reset_trackers()


def _fleet(paths, operator, scheduler_writer, *, signed: bool) -> None:
    signer = fake_signer if signed else None
    store.write_spec(paths, "node", NODE, {"actuate": True}, writer=operator, signer=signer)
    store.write_spec(
        paths,
        "service",
        "skgateway",
        {"unit": "skgateway.service"},
        writer=operator,
        signer=signer,
    )
    store.write_placement(
        paths,
        "service",
        "skgateway",
        node=NODE,
        reason="pinned",
        writer=scheduler_writer,
        signer=signer,
    )


def _runner() -> FakeRunner:
    return FakeRunner(
        {
            SHOW: FAILED,
            "systemctl --user restart skgateway.service": (0, ""),
            "journalctl --user -u skgateway.service -n 30 --no-pager": (0, ""),
        }
    )


def _unverified_cond(paths):
    st = store.read_status(paths, "service", "skgateway", NODE)
    return {c["type"]: c for c in st["conditions"]}.get("SpecUnverified")


def test_mode_off_ignores_signatures(paths, operator, scheduler_writer, monkeypatch) -> None:
    monkeypatch.delenv(signing.SIGNING_ENV, raising=False)
    _fleet(paths, operator, scheduler_writer, signed=False)
    runner = _runner()
    converge.converge_once(paths, NODE, runner=runner, now=1000.0, verifier=fake_verifier)
    assert runner.verbs() == ["systemctl --user restart skgateway.service"]
    assert _unverified_cond(paths) is None  # no condition in off mode


def test_permissive_warns_but_actuates(paths, operator, scheduler_writer, monkeypatch) -> None:
    monkeypatch.setenv(signing.SIGNING_ENV, "permissive")
    _fleet(paths, operator, scheduler_writer, signed=False)
    runner = _runner()
    converge.converge_once(paths, NODE, runner=runner, now=1000.0, verifier=fake_verifier)
    assert runner.verbs() == ["systemctl --user restart skgateway.service"]
    cond = _unverified_cond(paths)
    assert cond["status"] == "True"
    logged = events.read(paths, NODE, kind="service", name="skgateway")
    assert any(e["reason"] == "SpecUnverified" for e in logged)


def test_enforce_refuses_unsigned_and_alerts(
    paths, operator, scheduler_writer, monkeypatch
) -> None:
    monkeypatch.setenv(signing.SIGNING_ENV, "enforce")
    alerted: list[str] = []
    monkeypatch.setattr(
        converge.alerts, "send_alert", lambda msg, **kw: alerted.append(msg) or True
    )
    _fleet(paths, operator, scheduler_writer, signed=False)
    runner = _runner()
    out = converge.converge_once(paths, NODE, runner=runner, now=1000.0, verifier=fake_verifier)
    assert runner.verbs() == []  # refused: no new actuation
    assert out["services"]["skgateway"]["acted"] == "unverified"
    assert _unverified_cond(paths)["status"] == "True"
    st = store.read_status(paths, "service", "skgateway", NODE)
    assert st["status"]["state"] == "failed"  # probing continues
    assert len(alerted) == 1  # one deduped alert
    converge.converge_once(paths, NODE, runner=runner, now=1030.0, verifier=fake_verifier)
    assert len(alerted) == 1  # dedupe window holds


def test_enforce_actuates_when_properly_signed(
    paths, operator, scheduler_writer, monkeypatch
) -> None:
    monkeypatch.setenv(signing.SIGNING_ENV, "enforce")
    _fleet(paths, operator, scheduler_writer, signed=True)
    runner = _runner()
    converge.converge_once(paths, NODE, runner=runner, now=1000.0, verifier=fake_verifier)
    assert runner.verbs() == ["systemctl --user restart skgateway.service"]
    assert _unverified_cond(paths)["status"] == "False"


def test_enforce_refuses_tampered_spec(paths, operator, scheduler_writer, monkeypatch) -> None:
    monkeypatch.setenv(signing.SIGNING_ENV, "enforce")
    _fleet(paths, operator, scheduler_writer, signed=True)
    signed = store.read_spec(paths, "service", "skgateway")
    signed["spec"]["unit"] = "evil.service"  # tamper AFTER signing
    import json

    paths.spec_path("service", "skgateway").write_text(json.dumps(signed))
    runner = FakeRunner({})
    out = converge.converge_once(paths, NODE, runner=runner, now=1000.0, verifier=fake_verifier)
    assert runner.verbs() == []  # tampered: refused
    assert out["services"]["skgateway"]["acted"] == "unverified"


def test_enforce_without_verifier_fails_safe(
    paths, operator, scheduler_writer, monkeypatch
) -> None:
    monkeypatch.setenv(signing.SIGNING_ENV, "enforce")
    monkeypatch.setattr(signing, "capauth_verifier", lambda: None)
    _fleet(paths, operator, scheduler_writer, signed=True)
    runner = _runner()
    out = converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    assert runner.verbs() == []  # no roster: refuse, not stop
    assert out["services"]["skgateway"]["acted"] == "unverified"


def test_flip_is_a_config_change_only(paths, operator, scheduler_writer, monkeypatch) -> None:
    _fleet(paths, operator, scheduler_writer, signed=True)
    runner = _runner()
    monkeypatch.setenv(signing.SIGNING_ENV, "permissive")
    converge.converge_once(paths, NODE, runner=runner, now=1000.0, verifier=fake_verifier)
    monkeypatch.setenv(signing.SIGNING_ENV, "enforce")
    converge.converge_once(paths, NODE, runner=runner, now=1030.0, verifier=fake_verifier)
    assert len(runner.verbs()) == 2  # signed set: both modes act
