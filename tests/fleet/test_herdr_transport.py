"""Bounded Herdr command transport without touching the live multiplexer."""

import json
import socket
import subprocess
from pathlib import Path

import pytest

from skcapstone.fleet.herdr_transport import HerdrTransport, HerdrTransportError


def observation(**changes):
    return {
        "agent": "pi",
        "name": "helper-one",
        "agent_status": "idle",
        "cwd": "/tmp/helper",
        "pane_id": "wA:p1",
        "workspace_id": "wA",
        "tab_id": "wA:t1",
        "terminal_id": "terminal-one",
        "revision": 12,
        "state_change_seq": 8,
        **changes,
    }


@pytest.fixture
def context_env(tmp_path):
    path = tmp_path / "herdr.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    yield {"HERDR_ENV": "1", "HERDR_SOCKET_PATH": str(path)}
    listener.close()


def response(kind="agent_info", **changes):
    return subprocess.CompletedProcess(
        [], 0, json.dumps({"result": {"type": kind, "agent": observation(**changes)}}), ""
    )


def test_inspect_normalizes_live_agent_without_requiring_hooks(context_env):
    calls = []

    def run(argv, timeout):
        calls.append((argv, timeout))
        return response()

    transport = HerdrTransport(runner=run, environ=context_env)
    result = transport.inspect("helper-one")
    assert result["agent_name"] == "helper-one"
    assert result["agent_kind"] == "pi"
    assert result["state"] == "idle"
    assert result["agent_session"] is None
    assert calls == [(["herdr", "agent", "get", "helper-one"], 10)]
    assert transport.context()["socket_path"] == context_env["HERDR_SOCKET_PATH"]


def test_prompt_is_one_bounded_submission_without_completion_wait(context_env):
    calls = []

    def run(argv, timeout):
        calls.append((argv, timeout))
        return response("agent_prompted", agent_status="working", state_change_seq=9)

    report = HerdrTransport(runner=run, environ=context_env).prompt(
        "helper-one", "Exact task\npacket"
    )
    assert report["delivery"] == "submitted"
    assert report["agent"]["state"] == "working"
    assert calls == [(["herdr", "agent", "prompt", "helper-one", "Exact task\npacket"], 15)]


@pytest.mark.parametrize(
    "failure", ["timeout", "error", "invalid", "oversized", "wrong-kind", "unicode"]
)
def test_ambiguous_submission_is_unknown_and_not_retried(context_env, failure):
    calls = []

    def run(argv, timeout):
        calls.append(argv)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, timeout, output="sensitive transcript")
        if failure == "unicode":
            raise UnicodeDecodeError("utf-8", b"sensitive\xff", 9, 10, "invalid byte")
        if failure == "error":
            return subprocess.CompletedProcess(argv, 1, "", "sensitive transcript")
        if failure == "invalid":
            return subprocess.CompletedProcess(argv, 0, "not JSON", "")
        if failure == "oversized":
            return subprocess.CompletedProcess(argv, 0, " " * 131073, "")
        return response("agent_started")

    report = HerdrTransport(runner=run, environ=context_env).prompt("helper-one", "task")
    assert report["delivery"] == "unknown"
    assert len(calls) == 1
    assert "sensitive" not in json.dumps(report)


def test_inspection_decoder_error_is_normalized_without_raw_output(context_env):
    def run(*args):
        raise UnicodeDecodeError("utf-8", b"sensitive\xff", 9, 10, "invalid byte")

    with pytest.raises(HerdrTransportError, match="inspection is unavailable") as caught:
        HerdrTransport(runner=run, environ=context_env).inspect("helper-one")
    assert "sensitive" not in str(caught.value)


@pytest.mark.parametrize("change", [{"HERDR_ENV": "0"}, {"HERDR_SOCKET_PATH": "relative"}])
def test_unqualified_context_never_calls_herdr(context_env, change):
    def forbidden(*args):
        pytest.fail("Herdr must not be called")

    with pytest.raises(HerdrTransportError):
        HerdrTransport(runner=forbidden, environ=context_env | change).inspect("helper-one")


def test_replaced_socket_refuses_the_next_command(context_env):
    calls = []
    transport = HerdrTransport(runner=lambda *args: calls.append(args), environ=context_env)
    transport.context()
    path = Path(context_env["HERDR_SOCKET_PATH"])
    path.unlink()
    other = socket.socket(socket.AF_UNIX)
    try:
        other.bind(str(path))
        with pytest.raises(HerdrTransportError, match="changed"):
            transport.inspect("helper-one")
        assert calls == []
    finally:
        other.close()


@pytest.mark.parametrize("target", ["--help", "", "name;evil", "x" * 33])
def test_target_cannot_be_an_option_or_unbounded_input(context_env, target):
    with pytest.raises(HerdrTransportError):
        HerdrTransport(environ=context_env).inspect(target)


@pytest.mark.parametrize(
    "changes",
    [
        {"state_change_seq": True},
        {"revision": -1},
        {"agent_status": "success"},
        {"cwd": "relative"},
        {"agent": None},
        {"pane_id": "--help"},
    ],
)
def test_malformed_observation_is_refused(context_env, changes):
    with pytest.raises(HerdrTransportError):
        HerdrTransport(runner=lambda *args: response(**changes), environ=context_env).inspect(
            "helper-one"
        )


def test_session_is_fingerprinted_without_persisting_resume_context(context_env):
    session = {"source": "hook", "agent": "pi", "kind": "id", "value": "private-resume-id"}
    report = HerdrTransport(
        runner=lambda *args: response(agent_session=session), environ=context_env
    ).inspect("helper-one")
    assert len(report["agent_session"]) == 64
    assert "private-resume-id" not in json.dumps(report)


def test_foreground_cwd_takes_precedence_over_stale_pane_cwd(context_env):
    report = HerdrTransport(
        runner=lambda *args: response(foreground_cwd="/tmp/current"), environ=context_env
    ).inspect("helper-one")
    assert report["cwd"] == "/tmp/current"
