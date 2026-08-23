"""Tests for docker verbs + runtime dispatch (all runners faked)."""

from __future__ import annotations

from subprocess import CompletedProcess

from skcapstone.fleet import actuation, backoff, converge, events, store
from skcapstone.fleet.services import normalize_service_spec


class FakeRunner:
    def __init__(self, replies: dict[str, tuple[int, str, str]]) -> None:
        self.replies = dict(replies)
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> CompletedProcess:
        self.calls.append(cmd)
        code, out, err = self.replies.get(" ".join(cmd), (0, "", ""))
        return CompletedProcess(cmd, code, stdout=out, stderr=err)


INSPECT = "docker inspect -f " "{{.State.Status}}|{{.State.Pid}}|{{.State.StartedAt}} coturn"


def test_docker_state_running_exited_missing() -> None:
    runner = FakeRunner({INSPECT: (0, "running|314|2026-07-28T00:00:00Z\n", "")})
    st = actuation.docker_state("coturn", runner=runner)
    assert st.state == "active" and st.pid == 314
    assert st.since == "2026-07-28T00:00:00Z"
    runner = FakeRunner({INSPECT: (0, "exited|0|t\n", "")})
    assert actuation.docker_state("coturn", runner=runner).state == "failed"
    runner = FakeRunner({INSPECT: (0, "restarting|0|t\n", "")})
    assert actuation.docker_state("coturn", runner=runner).state == "activating"
    runner = FakeRunner({INSPECT: (1, "", "Error: No such object: coturn")})
    assert actuation.docker_state("coturn", runner=runner).state == "missing"
    runner = FakeRunner({INSPECT: (1, "", "Cannot connect to the Docker daemon")})
    assert actuation.docker_state("coturn", runner=runner).state == "unknown"


def test_docker_verbs_and_logs() -> None:
    runner = FakeRunner(
        {
            "docker start coturn": (0, "", ""),
            "docker restart coturn": (0, "", ""),
            "docker logs --tail 30 coturn": (0, "turn ready\n", ""),
        }
    )
    assert actuation.docker_start("coturn", runner=runner) is True
    assert actuation.docker_restart("coturn", runner=runner) is True
    assert actuation.docker_logs("coturn", runner=runner) == "turn ready"


def test_compose_up() -> None:
    runner = FakeRunner(
        {
            "docker compose -f /opt/coturn/compose.yml up -d coturn": (0, "", ""),
        }
    )
    assert actuation.compose_up("/opt/coturn/compose.yml", "coturn", runner=runner) is True


def test_dispatch_by_runtime() -> None:
    sysd = normalize_service_spec({"unit": "u.service"})
    dock = normalize_service_spec({"unit": "coturn", "runtime": "docker"})
    comp = normalize_service_spec(
        {
            "unit": "coturn",
            "runtime": "docker",
            "compose": {"file": "/opt/c.yml", "service": "coturn"},
        }
    )
    runner = FakeRunner(
        {
            "systemctl --user start u.service": (0, "", ""),
            "docker start coturn": (0, "", ""),
            "docker compose -f /opt/c.yml up -d coturn": (0, "", ""),
        }
    )
    assert actuation.start(sysd, runner=runner) is True
    assert actuation.start(dock, runner=runner) is True
    assert actuation.start(comp, runner=runner) is True
    assert [" ".join(c) for c in runner.calls] == [
        "systemctl --user start u.service",
        "docker start coturn",
        "docker compose -f /opt/c.yml up -d coturn",
    ]


def test_docker_service_converges_like_systemd(paths, operator, scheduler_writer) -> None:
    events.reset_dedupe()
    backoff.reset_trackers()
    store.write_spec(paths, "node", "node-41", {"actuate": True}, writer=operator)
    store.write_spec(
        paths, "service", "coturn", {"unit": "coturn", "runtime": "docker"}, writer=operator
    )
    store.write_placement(
        paths, "service", "coturn", node="node-41", reason="pinned", writer=scheduler_writer
    )
    runner = FakeRunner(
        {
            (
                "docker inspect -f {{.State.Status}}|{{.State.Pid}}|" "{{.State.StartedAt}} coturn"
            ): (0, "exited|0|t\n", ""),
            "docker logs --tail 30 coturn": (0, "bye\n", ""),
            "docker restart coturn": (0, "", ""),
        }
    )
    out = converge.converge_once(paths, "node-41", runner=runner, now=1000.0)
    assert out["services"]["coturn"]["acted"] == "healed"
    assert ["docker", "restart", "coturn"] in runner.calls
    st = store.read_status(paths, "service", "coturn", "node-41")
    assert st["status"]["runtime"] == "docker"
    events.reset_dedupe()
    backoff.reset_trackers()
