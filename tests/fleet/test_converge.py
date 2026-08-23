"""Tests for the sknoded converge loop: gates, healing, degrade-safe."""

from __future__ import annotations

from subprocess import CompletedProcess

import pytest

from skcapstone.fleet import backoff, converge, events, store

NODE = "node-41"
SHOW = (
    "systemctl --user show skgateway.service "
    "--property=LoadState,ActiveState,MainPID,ActiveEnterTimestamp"
)
ACTIVE = (0, "LoadState=loaded\nActiveState=active\nMainPID=42\n" "ActiveEnterTimestamp=t0\n")
FAILED = (0, "LoadState=loaded\nActiveState=failed\nMainPID=0\n" "ActiveEnterTimestamp=\n")


class FakeRunner:
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


@pytest.fixture(autouse=True)
def _fresh():
    events.reset_dedupe()
    backoff.reset_trackers()
    yield
    events.reset_dedupe()
    backoff.reset_trackers()


def _fleet(paths, operator, scheduler_writer, *, actuate=True, spec=None) -> None:
    node_spec = {"cordoned": False, "taints": []}
    if actuate:
        node_spec["actuate"] = True
    store.write_spec(paths, "node", NODE, node_spec, writer=operator)
    store.write_spec(
        paths, "service", "skgateway", spec or {"unit": "skgateway.service"}, writer=operator
    )
    store.write_placement(
        paths, "service", "skgateway", node=NODE, reason="pinned for test", writer=scheduler_writer
    )


def test_healthy_service_writes_status_and_no_verbs(paths, operator, scheduler_writer) -> None:
    _fleet(paths, operator, scheduler_writer)
    runner = FakeRunner({SHOW: ACTIVE})
    out = converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    assert out["mode"] == "actuate"
    assert runner.verbs() == []  # in sync: no actuation
    st = store.read_status(paths, "service", "skgateway", NODE)
    assert st["status"]["state"] == "active" and st["status"]["pid"] == 42
    assert st["observedGeneration"] == 1
    conds = {c["type"]: c["status"] for c in st["conditions"]}
    assert conds["Ready"] == "True" and conds["CrashLooping"] == "False"


def test_failed_service_is_healed_with_logs_event(paths, operator, scheduler_writer) -> None:
    _fleet(paths, operator, scheduler_writer)
    runner = FakeRunner(
        {
            SHOW: FAILED,
            "systemctl --user restart skgateway.service": (0, ""),
            "journalctl --user -u skgateway.service -n 30 --no-pager": (0, "segv\n"),
        }
    )
    converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    assert runner.verbs() == ["systemctl --user restart skgateway.service"]
    logged = events.read(paths, NODE, kind="service", name="skgateway")
    reasons = [e["reason"] for e in logged]
    assert "FailureLogs" in reasons and "Restarted" in reasons
    assert any(e["message"] == "segv" for e in logged if e["reason"] == "FailureLogs")


def test_missing_unit_is_started(paths, operator, scheduler_writer) -> None:
    _fleet(paths, operator, scheduler_writer)
    runner = FakeRunner(
        {
            SHOW: (
                0,
                "LoadState=loaded\nActiveState=inactive\nMainPID=0\n" "ActiveEnterTimestamp=\n",
            ),
            "systemctl --user start skgateway.service": (0, ""),
        }
    )
    converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    assert runner.verbs() == ["systemctl --user start skgateway.service"]


def test_freeze_halts_all_actuation_but_not_reporting(paths, operator, scheduler_writer) -> None:
    _fleet(paths, operator, scheduler_writer)
    store.set_frozen(paths, True, writer=operator, reason="drill")
    runner = FakeRunner({SHOW: FAILED})
    out = converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    assert out["mode"] == "frozen"
    assert runner.verbs() == []  # kill-switch: zero verbs
    st = store.read_status(paths, "service", "skgateway", NODE)
    assert st["status"]["state"] == "failed"  # self-report continues


def test_report_only_without_opt_in(paths, operator, scheduler_writer) -> None:
    _fleet(paths, operator, scheduler_writer, actuate=False)
    runner = FakeRunner({SHOW: FAILED})
    out = converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    assert out["mode"] == "report-only"
    assert runner.verbs() == []
    assert store.read_status(paths, "service", "skgateway", NODE) is not None


def test_paused_spec_stops_healing(paths, operator, scheduler_writer) -> None:
    _fleet(paths, operator, scheduler_writer, spec={"unit": "skgateway.service", "paused": True})
    runner = FakeRunner({SHOW: FAILED})
    converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    assert runner.verbs() == []
    st = store.read_status(paths, "service", "skgateway", NODE)
    prog = {c["type"]: c for c in st["conditions"]}["Progressing"]
    assert prog["status"] == "False" and prog["reason"] == "Paused"


def test_unreadable_spec_never_touches_the_unit(paths, operator, scheduler_writer) -> None:
    _fleet(paths, operator, scheduler_writer)
    paths.spec_path("service", "skgateway").write_text("not json")
    runner = FakeRunner({SHOW: FAILED})
    out = converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    assert runner.verbs() == []  # degrade-safe: no verbs
    assert runner.calls == []  # not even a state probe
    assert out["services"]["skgateway"]["skipped"] == "spec unreadable"
    logged = events.read(paths, NODE, kind="service", name="skgateway")
    assert [e["reason"] for e in logged] == ["SpecUnreadable"]


def test_unreachable_tree_is_degraded_noop(paths, monkeypatch) -> None:
    def boom(*a, **k):
        raise OSError("syncthing tree gone")

    monkeypatch.setattr(converge.store, "list_placements", boom)
    out = converge.converge_once(paths, NODE, runner=FakeRunner({}), now=1000.0)
    assert out == {"mode": "degraded", "services": {}}


def test_crash_loop_backoff_and_condition(paths, operator, scheduler_writer, monkeypatch) -> None:
    _fleet(paths, operator, scheduler_writer)
    alerted: list[str] = []
    monkeypatch.setattr(
        converge.alerts, "send_alert", lambda msg, **kw: alerted.append(msg) or True
    )
    runner = FakeRunner(
        {
            SHOW: FAILED,
            "systemctl --user restart skgateway.service": (0, ""),
            "journalctl --user -u skgateway.service -n 30 --no-pager": (0, ""),
        }
    )
    now = 1000.0
    for i in range(backoff.CRASH_LOOP_AFTER):
        converge.converge_once(paths, NODE, runner=runner, now=now)
        now += backoff.next_delay(i + 1)
    heals = len(runner.verbs())
    assert heals == backoff.CRASH_LOOP_AFTER  # bounded attempt budget
    converge.converge_once(paths, NODE, runner=runner, now=now + 1.0)
    assert len(runner.verbs()) == heals  # looping: healing stopped
    st = store.read_status(paths, "service", "skgateway", NODE)
    conds = {c["type"]: c["status"] for c in st["conditions"]}
    assert conds["CrashLooping"] == "True"
    assert any("CrashLooping" in m for m in alerted)  # alerted exactly via event
    assert len(alerted) == 1  # dedupe window caps alerts


def test_backoff_window_skips_early_retry(paths, operator, scheduler_writer) -> None:
    _fleet(paths, operator, scheduler_writer)
    runner = FakeRunner(
        {
            SHOW: FAILED,
            "systemctl --user restart skgateway.service": (0, ""),
            "journalctl --user -u skgateway.service -n 30 --no-pager": (0, ""),
        }
    )
    converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    converge.converge_once(paths, NODE, runner=runner, now=1005.0)  # inside 10s
    assert len(runner.verbs()) == 1  # second pass waited


def test_health_probe_gates_ready(paths, operator, scheduler_writer) -> None:
    _fleet(
        paths,
        operator,
        scheduler_writer,
        spec={"unit": "skgateway.service", "healthCheck": {"port": 18780}},
    )
    runner = FakeRunner({SHOW: ACTIVE})
    converge.converge_once(paths, NODE, runner=runner, now=1000.0, prober=lambda check: False)
    st = store.read_status(paths, "service", "skgateway", NODE)
    ready = {c["type"]: c for c in st["conditions"]}["Ready"]
    assert ready["status"] == "False" and ready["reason"] == "ProbeFailed"


def test_placement_elsewhere_is_ignored(paths, operator, scheduler_writer) -> None:
    store.write_spec(paths, "node", NODE, {"actuate": True}, writer=operator)
    store.write_spec(paths, "service", "skgateway", {"unit": "u.service"}, writer=operator)
    store.write_placement(
        paths, "service", "skgateway", node="node-158", reason="r", writer=scheduler_writer
    )
    runner = FakeRunner({})
    out = converge.converge_once(paths, NODE, runner=runner, now=1000.0)
    assert out["services"] == {} and runner.calls == []


def test_main_loop_once_reports_and_converges(paths, monkeypatch) -> None:
    from skcapstone.fleet import sknoded

    ran: list[str] = []
    monkeypatch.setattr(sknoded, "run_once", lambda p, n: ran.append("report"))
    monkeypatch.setattr(
        "skcapstone.fleet.converge.converge_once",
        lambda p, n: ran.append("converge") or {"mode": "report-only", "services": {}},
    )
    sknoded.main_loop(paths, NODE, once=True)
    assert ran == ["report", "converge"]
