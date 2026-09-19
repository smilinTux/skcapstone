from __future__ import annotations

import os
from pathlib import Path

from skcapstone.fleet_worker_liveness import observe


def runner_factory(active: str, failed: str, started: dict[str, str]):
    def run(argv):
        args = list(argv)
        if args[:3] == ["systemctl", "--user", "--state=active"]:
            return active
        if args[:3] == ["systemctl", "--user", "--state=failed"]:
            return failed
        if "show" in args:
            unit = args[3]
            if "ActiveEnterTimestamp" in args and "Monotonic" not in args:
                return started.get(unit, "")
        return ""
    return run


def test_active_session_mtime_is_signal_and_failed_unit_is_cruft(tmp_path: Path):
    root = tmp_path / "sessions" / "workspace-deadbeef"
    root.mkdir(parents=True)
    session = root / "run.jsonl"
    session.write_text('{"type":"message"}\n')
    os.utime(session, (2000, 2000))
    active = "skfleet-worker-codex-deadbeef.service loaded active running\n"
    failed = "skfleet-worker-qwen-baadf00d.service loaded failed failed\n"
    runner = runner_factory(active, failed, {"skfleet-worker-codex-deadbeef.service": "1900"})

    workers, cruft = observe(root.parent, runner)

    assert workers[0].live is True
    assert workers[0].session_mtime == 2000
    assert cruft[0].card_id == "baadf00d"
    assert all(w.unit != cruft[0].unit for w in workers)


def test_previous_run_session_is_refused_with_120_second_skew(tmp_path: Path):
    root = tmp_path / "sessions" / "workspace-deadbeef"
    root.mkdir(parents=True)
    session = root / "old.jsonl"
    session.write_text("{}\n")
    os.utime(session, (1779, 1779))
    active = "skfleet-worker-codex-deadbeef.service loaded active running\n"
    runner = runner_factory(active, "", {"skfleet-worker-codex-deadbeef.service": "1900"})

    workers, cruft = observe(root.parent, runner)

    assert workers[0].live is False
    assert workers[0].session_file is None
    assert cruft == []
