"""Seat-agent filter and assess behavior for skfleet-working."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

PATH = Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet-working.py"


@pytest.fixture(autouse=True)
def monitor_settings(monkeypatch):
    """Keep timing tests independent of operator environment overrides."""
    monkeypatch.setenv("SKFLEET_UNIT_GRACE", "15")
    monkeypatch.setenv("SKFLEET_UNIT_SAMPLE_FRESHNESS", "60")


def load_monitor():
    loader = importlib.machinery.SourceFileLoader("skfleet_working", str(PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


def test_seat_agent_is_not_ephemeral_worker_name() -> None:
    monitor = load_monitor()
    assert monitor.is_ephemeral_worker_agent("pi-chiap08", "a1b2c3d4") is False
    assert monitor.is_ephemeral_worker_agent("pi-jarvis", "deadbeef") is False


def test_ephemeral_worker_name_matches_card_suffix() -> None:
    monitor = load_monitor()
    assert monitor.is_ephemeral_worker_agent("pi-codex-chiap08-a1b2c3d4", "a1b2c3d4") is True
    assert monitor.is_ephemeral_worker_agent("a1b2c3d4", "a1b2c3d4") is True


def test_remote_collector_keeps_seat_filter_in_sync() -> None:
    monitor = load_monitor()
    assert "Only ephemeral workers are named" in monitor.REMOTE
    assert "agent.endswith('-'+card) or agent == card" in monitor.REMOTE
    assert "is_ephemeral_worker_agent" in PATH.read_text(encoding="utf-8")


def test_seat_hold_without_unit_is_not_stale_projection_candidate() -> None:
    """Seat agents with hold/current_task must not enter the stale path."""
    monitor = load_monitor()
    # Without the filter, a seat projection would reach assess as unit=not-found.
    # The filter excludes them before emission; only ephemeral names qualify.
    assert monitor.is_ephemeral_worker_agent("pi-chiap08", "a1b2c3d4") is False
    ephemeral = monitor.Worker(
        host="chiap08",
        agent="pi-codex-chiap08-a1b2c3d4",
        card="a1b2c3d4",
        pid=0,
        elapsed=0,
        cpu=0,
        log_bytes=-1,
        log_age=-1,
        unit="not-found",
        tmux=False,
        claim_state="mismatch",
        card_status="doing",
        unit_missing_process=False,
        evidence_source="agent-projection+systemd+proc",
    )
    state, _ = monitor.assess(ephemeral, {}, now=100)
    assert state == "STALE PROJECTION"


def worker(monitor, **changes):
    """Build a synthetic observed Pi process or unit diagnostic."""
    values = dict(
        host="host-a",
        agent="pi-codex-host-a-deadbeef",
        card="deadbeef",
        pid=321,
        elapsed=90,
        cpu=1,
        log_bytes=-1,
        log_age=-1,
        unit="skfleet-worker-codex-deadbeef.service",
        tmux=False,
        claim_state="exact",
        card_status="doing",
        unit_missing_process=False,
        unit_load="loaded",
        unit_active="active",
        unit_sub="running",
    )
    values.update(changes)
    return monitor.Worker(**values)


def herdr_agent(card="deadbeef", **changes):
    """Build current Herdr evidence for one bounded coworktree."""
    values = {
        "name": f"repair-{card}-codex",
        "agent_status": "working",
        "cwd": f"/work/skcapstone-{card}-herdr",
    }
    values.update(changes)
    return values


def stale_projection(monitor, **changes):
    """Build a projection-only row eligible for Herdr corroboration."""
    values = dict(
        agent="pi-herdr-deadbeef",
        pid=0,
        unit="not-found",
        unit_load="not-found",
        unit_active="inactive",
        unit_sub="dead",
        claim_state="exact",
        projection_state="stale",
        evidence_source="agent-projection+systemd+proc",
    )
    values.update(changes)
    return worker(monitor, **values)


@pytest.mark.parametrize("projection_state", ["stale", "valid"])
def test_live_herdr_clears_only_exact_projection(monkeypatch, projection_state):
    """Current Herdr evidence supplements an exact owner and claim join."""
    monitor = load_monitor()
    monkeypatch.setattr(
        monitor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"result": {"agents": [herdr_agent()]}}),
        ),
    )

    joined = monitor.join_herdr_evidence(
        [stale_projection(monitor, projection_state=projection_state)]
    )

    assert joined[0].projection_state == "valid"
    assert joined[0].evidence_source.endswith("+herdr")
    assert monitor.assess(joined[0], {}, 100)[0] == "OK"


@pytest.mark.parametrize(
    ("row_changes", "agent_changes"),
    [
        ({"claim_state": "mismatch"}, {}),
        ({"card": "feedface"}, {}),
        ({}, {"cwd": "/work/skcapstone-feedface-herdr"}),
        ({}, {"agent_status": "done"}),
        ({}, {"agent_status": "unknown"}),
    ],
)
def test_herdr_mismatch_or_dead_evidence_remains_stale(monkeypatch, row_changes, agent_changes):
    """Owner/revision, card, coworktree, and live status all fail closed."""
    monitor = load_monitor()
    monkeypatch.setattr(
        monitor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"result": {"agents": [herdr_agent(**agent_changes)]}}),
        ),
    )

    assert (
        monitor.join_herdr_evidence([stale_projection(monitor, **row_changes)])[0].projection_state
        == "stale"
    )


def test_historical_pane_remains_stale(monkeypatch):
    """Pane history outside the current agent list never becomes liveness."""
    monitor = load_monitor()
    historical_only = {"result": {"agents": [], "panes": [herdr_agent()]}}
    monkeypatch.setattr(
        monitor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(historical_only)),
    )
    projection = stale_projection(monitor)

    assert monitor.join_herdr_evidence([projection])[0].projection_state == "stale"


@pytest.mark.parametrize(
    "contradiction", [{"pid": 321}, {"unit": "skfleet-worker-pi-deadbeef.service"}]
)
def test_authoritative_contradiction_remains_stale(monkeypatch, contradiction):
    """Observed systemd or proc evidence remains authoritative over Herdr."""
    monitor = load_monitor()
    monkeypatch.setattr(
        monitor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"result": {"agents": [herdr_agent()]}}),
        ),
    )
    authoritative = replace(stale_projection(monitor), **contradiction)

    assert monitor.join_herdr_evidence([authoritative])[0].projection_state == "stale"


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError(),
        subprocess.TimeoutExpired(["herdr"], 2),
        SimpleNamespace(returncode=1, stdout=""),
        SimpleNamespace(returncode=0, stdout="not-json"),
        SimpleNamespace(returncode=0, stdout=json.dumps({"result": {}})),
    ],
)
def test_herdr_unavailable_timeout_or_malformed_fails_closed(monkeypatch, failure):
    """Every unavailable or incomplete Herdr result preserves stale truth."""
    monitor = load_monitor()

    def run(*args, **kwargs):
        if isinstance(failure, BaseException):
            raise failure
        return failure

    monkeypatch.setattr(monitor.subprocess, "run", run)

    assert monitor.join_herdr_evidence([stale_projection(monitor)])[0].projection_state == "stale"


def run_report(monkeypatch, tmp_path, capsys, monitor, rows, samples=None, now=100):
    """Render a report using only supplied evidence and an in-memory cache."""
    saved = {}
    monkeypatch.setattr(monitor, "HOSTS", ("host-a", "host-b"))
    monkeypatch.setattr(
        monitor, "collect", lambda host: ([row for row in rows if row.host == host], {})
    )
    monkeypatch.setattr(monitor, "load_samples", lambda: dict(samples or {}))
    monkeypatch.setattr(monitor, "save_samples", saved.update)
    monkeypatch.setattr(monitor.time, "time", lambda: now)
    monkeypatch.setenv("SKGW_METRICS_DB", str(tmp_path / "absent.db"))
    result = monitor.main()
    return result, capsys.readouterr().out, saved


@pytest.mark.parametrize("live_count", [0, 1, 2])
def test_only_pi_processes_count_and_duplicate(monkeypatch, tmp_path, capsys, live_count):
    """A same-card unit diagnostic never adds a process or host to totals."""
    monitor = load_monitor()
    rows = [worker(monitor, host="host-b", unit_missing_process=True, elapsed=0)]
    rows += [worker(monitor, pid=400 + index) for index in range(live_count)]
    result, output, _ = run_report(monkeypatch, tmp_path, capsys, monitor, rows)
    assert f"fleet worker processes: {live_count} across {int(live_count > 0)} host(s)" in output
    diagnostic = next(line for line in output.splitlines() if "diagnostic unit without Pi" in line)
    assert diagnostic.split()[3] == "-"
    assert "Pi elapsed" in output
    assert output.count("DUPLICATE card process") == (2 if live_count == 2 else 0)
    assert result == int(live_count == 2)


@pytest.mark.parametrize("interval", [1, 5, 15])
def test_grace_uses_total_first_seen_duration(monkeypatch, interval):
    """Continuous short polling escalates at the exact grace boundary."""
    monitor = load_monitor()
    monkeypatch.setattr(monitor, "UNIT_GRACE", 15)
    monkeypatch.setattr(monitor, "UNIT_SAMPLE_FRESHNESS", 60)
    row = worker(monitor, unit_missing_process=True)
    samples = {}
    for now in range(100, 116, interval):
        expected = "ACTION REQUIRED" if now == 115 else "SETTLING"
        assert monitor.assess(row, samples, now) == (expected, 100)
    assert monitor.assess(row, samples, 120) == ("ACTION REQUIRED", 100)


@pytest.mark.parametrize(
    "gap, expected, first_seen",
    [
        (60, "ACTION REQUIRED", 100),
        (61, "SETTLING", 161),
        (-1, "SETTLING", 99),
    ],
)
def test_sample_freshness_boundary(monkeypatch, gap, expected, first_seen):
    """Stale or backward samples restart the observation window."""
    monitor = load_monitor()
    monkeypatch.setattr(monitor, "UNIT_GRACE", 15)
    monkeypatch.setattr(monitor, "UNIT_SAMPLE_FRESHNESS", 60)
    row = worker(monitor, unit_missing_process=True)
    samples = {}
    monitor.assess(row, samples, 100)
    assert monitor.assess(row, samples, 100 + gap) == (expected, first_seen)
    assert monitor.assess(row, samples, 115 + gap) == ("ACTION REQUIRED", first_seen)


@pytest.mark.parametrize(
    "claim, projection", [("exact", "valid"), ("mismatch", "valid"), ("exact", "missing")]
)
def test_process_recovery_clears_observation(claim, projection):
    """Recovery clears no-Pi history even when claim diagnostics remain."""
    monitor = load_monitor()
    missing = worker(monitor, unit_missing_process=True)
    samples = {}
    monitor.assess(missing, samples, 100)
    monitor.assess(worker(monitor, claim_state=claim, projection_state=projection), samples, 105)
    assert samples == {}
    assert monitor.assess(missing, samples, 110) == ("SETTLING", 110)


def test_failed_exact_claim_unit_alerts_immediately(monkeypatch, tmp_path, capsys):
    """A failed claimed unit remains an immediate visible diagnostic."""
    monitor = load_monitor()
    row = worker(monitor, unit_missing_process=True, unit_active="failed", unit_sub="failed")
    result, output, _ = run_report(monkeypatch, tmp_path, capsys, monitor, [row])
    assert result == 1
    assert "fleet worker processes: 0 across 0 host(s)" in output
    assert (
        "ACTION REQUIRED joined unit/process/claim evidence; diagnostic unit without Pi process"
        in output
    )


def test_main_resets_stale_samples_and_clears_recovery(monkeypatch, tmp_path, capsys):
    """Persisted stale samples and recovered units cannot retain old grace."""
    monitor = load_monitor()
    monkeypatch.setattr(monitor, "UNIT_SAMPLE_FRESHNESS", 60)
    row = worker(monitor, unit_missing_process=True)
    key = f"{row.host}/{row.unit}"
    result, output, saved = run_report(
        monkeypatch,
        tmp_path,
        capsys,
        monitor,
        [row],
        {key: {"first_seen": 1, "observed": 10}},
        now=100,
    )
    assert result == 0 and "SETTLING unit without Pi process" in output
    assert saved[key] == {"first_seen": 100, "observed": 100}
    _, _, recovered = run_report(
        monkeypatch,
        tmp_path,
        capsys,
        monitor,
        [worker(monitor)],
        saved,
        now=105,
    )
    assert recovered == {}


@pytest.mark.parametrize(
    "has_pi, broken_projection", [(False, False), (False, True), (True, False), (True, True)]
)
def test_remote_collector_unit_fallback(monkeypatch, capsys, has_pi, broken_projection):
    """Execute the real collector against synthetic proc, unit and card state."""
    monitor = load_monitor()
    unit = "skfleet-worker-codex-deadbeef.service"
    agent = "pi-codex-host-a-deadbeef"
    card_store = ModuleType("skcoord.card_store")

    def fold(card):
        """Return an exact synthetic claim or the collector's error fallback."""
        assert card == "deadbeef"
        if broken_projection:
            raise ValueError("synthetic invalid projection")
        return SimpleNamespace(
            owner=agent, meta={"_claim_revision": "r1"}, status=SimpleNamespace(value="doing")
        )

    card_store.CardStore = lambda path: SimpleNamespace(fold=fold)
    monkeypatch.setitem(sys.modules, "skcoord.card_store", card_store)
    monkeypatch.setattr(Path, "glob", lambda self, pattern: [])
    monkeypatch.setattr(monitor.os, "uname", lambda: SimpleNamespace(nodename="host-a"))
    monkeypatch.setattr(monitor.os, "sysconf", lambda name: 100)
    proc = {
        "/proc/321/comm": "pi\n",
        "/proc/321/environ": f"SKAGENT={agent}\0".encode(),
        "/proc/321/stat": " ".join(["0"] * 21 + ["1000"]),
        "/proc/uptime": "100 0",
        "/proc/321/cgroup": f"0::/user.slice/{unit}",
    }

    def open_proc(path, mode="r"):
        """Serve only explicitly enumerated synthetic process files."""
        value = proc[path]
        return io.BytesIO(value) if "b" in mode else io.StringIO(value)

    def run(command, **kwargs):
        """Reject unexpected subprocesses instead of contacting live units."""
        if command[:3] == ["systemctl", "--user", "list-units"]:
            output = f"{unit} loaded active running synthetic\n"
        elif command == ["systemctl", "--user", "show", unit, "-p", "MainPID", "--value"]:
            output = "999\n"
        elif command == ["tmux", "list-sessions", "-F", "#{session_name}"]:
            output = ""
        else:
            raise AssertionError(command)
        return SimpleNamespace(stdout=output, returncode=0)

    import glob

    monkeypatch.setattr(
        glob,
        "glob",
        lambda pattern: ["/proc/321/comm"] if has_pi and pattern == "/proc/[0-9]*/comm" else [],
    )
    monkeypatch.setattr(monitor.subprocess, "run", run)
    exec(compile(monitor.REMOTE, "<synthetic collector>", "exec"), {"open": open_proc})
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    rows = [
        monitor.Worker(**record) for record in records if "projection_diagnostics" not in record
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row.unit_missing_process is (not has_pi)
    assert row.elapsed == (90 if has_pi else -1)
    assert row.pid == (321 if has_pi else 999)
    assert row.projection_state == (
        "malformed" if broken_projection else "missing" if has_pi else "valid"
    )
    assert row.projection_error == ("ValueError" if broken_projection else "")


def test_direct_seat_active_requires_exact_process_projection_claim_join(
    monkeypatch, tmp_path, capsys
):
    """The c1841391 seat process is active only after every identity joins."""
    monitor = load_monitor()
    exact = worker(
        monitor,
        agent="pi-chiap08",
        card="deadbeef",
        unit="legacy",
        evidence_source="direct-seat-record+proc+cardstore",
        completion_state="running",
        heartbeat_at="1970-01-01T00:01:40+00:00",
        process_record="/home/test/.skcapstone/fleet/direct-seats/pi-chiap08.json",
    )
    result, output, _ = run_report(monkeypatch, tmp_path, capsys, monitor, [exact])
    assert result == 0
    assert "DIRECT SEAT ACTIVE" in output

    mismatched = worker(
        monitor,
        agent="pi-chiap08",
        card="deadbeef",
        unit="legacy",
        evidence_source="direct-seat-record+proc+cardstore",
        claim_state="mismatch",
        completion_state="running",
        heartbeat_at="1970-01-01T00:01:40+00:00",
    )
    result, output, _ = run_report(monkeypatch, tmp_path, capsys, monitor, [mismatched])
    assert result == 0
    assert "DIRECT SEAT ACTIVE" not in output
    assert "STALE PROJECTION" in output


def test_direct_seat_terminal_process_is_not_active(monkeypatch, tmp_path, capsys):
    """A bounded terminal record never presents as a live fleet worker."""
    monitor = load_monitor()
    row = worker(
        monitor,
        agent="pi-chiap08",
        card="deadbeef",
        unit="legacy",
        evidence_source="direct-seat-record+proc+cardstore",
        completion_state="completed",
        heartbeat_at="1970-01-01T00:01:40+00:00",
    )
    result, output, _ = run_report(monkeypatch, tmp_path, capsys, monitor, [row])
    assert result == 0
    assert "DIRECT SEAT ACTIVE" not in output


def test_direct_seat_stale_heartbeat_is_not_active(monkeypatch, tmp_path, capsys):
    monitor = load_monitor()
    row = worker(
        monitor,
        agent="pi-chiap08",
        unit="legacy",
        evidence_source="direct-seat-record+proc+cardstore",
        completion_state="running",
        heartbeat_at="1970-01-01T00:00:01+00:00",
        process_record="/bounded/record.json",
    )
    result, output, _ = run_report(monkeypatch, tmp_path, capsys, monitor, [row], now=1000)
    assert result == 0
    assert "DIRECT SEAT ACTIVE" not in output
    assert "STALE PROJECTION" in output


def test_collect_parses_unit_diagnostic_and_ignores_noise(monkeypatch):
    """The SSH wrapper preserves a unit fallback row and separate diagnostics."""
    monitor = load_monitor()
    row = worker(monitor, unit_missing_process=True, elapsed=-1)
    output = "\n".join(
        [
            "collector noise",
            "{}",
            json.dumps(vars(row)),
            json.dumps({"host": "host-a", "projection_diagnostics": {"idle_projections": 3}}),
        ]
    )
    monkeypatch.setattr(
        monitor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output),
    )
    rows, diagnostics = monitor.collect("host-a")
    assert rows == [row]
    assert diagnostics == {"host-a": {"idle_projections": 3}}
