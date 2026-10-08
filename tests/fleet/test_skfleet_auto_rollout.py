"""skfleet-auto-rollout.sh: deploy merged main on a timer, halt on the first failed gate."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/fleet/skfleet-auto-rollout.sh"
UNITS = ROOT / "scripts/fleet/systemd"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _write_exe(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(0o755)


@pytest.fixture()
def fleet(tmp_path: Path):
    """An origin repo, a deploy clone of it, and fake fleet tools that record their calls."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "t@example.com")
    _git(origin, "config", "user.name", "t")
    (origin / "f").write_text("1")
    _git(origin, "add", "f")
    _git(origin, "commit", "-q", "-m", "first")
    deploy = tmp_path / "deploy"
    _git(tmp_path, "clone", "-q", str(origin), str(deploy))
    bins = tmp_path / "bin"
    bins.mkdir()
    calls = tmp_path / "calls.log"
    fail_host = tmp_path / "fail_host"
    _write_exe(
        bins / "skcapstone",
        f'echo "rollout $*" >> {calls}\n'
        'host=""; while [ $# -gt 0 ]; do [ "$1" = --node ] && host=$2; shift; done\n'
        f'if [ -f {fail_host} ] && [ "$host" = "$(cat {fail_host})" ]; then echo "{"{"}host{"}"}: gate failed"; else echo "$host: deployed and gate passed"; fi\n',
    )
    _write_exe(bins / "ssh", f'echo "ssh $*" >> {calls}\n')
    _write_exe(bins / "skmail", f'echo "skmail $*" >> {calls}\n')
    env = dict(
        os.environ,
        PATH=f"{bins}:{os.environ['PATH']}",
        SKFLEET_AUTO_ROLLOUT_REPO=str(deploy),
        SKFLEET_AUTO_ROLLOUT_HOSTS="h1 h2 h3",
        SKFLEET_AUTO_ROLLOUT_LOCK=str(tmp_path / "lock"),
        SKFLEET_AUTO_ROLLOUT_READY_WAIT="0",
        SKFLEET_AUTO_ROLLOUT_ATTEMPTS="2",
    )
    return dict(origin=origin, deploy=deploy, calls=calls, fail_host=fail_host, env=env)


def _advance_origin(origin: Path) -> str:
    (origin / "f").write_text("2")
    _git(origin, "commit", "-q", "-am", "second")
    return _git(origin, "rev-parse", "HEAD")


def _run(env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60
    )


def _calls(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def test_up_to_date_checkout_is_a_quiet_noop(fleet):
    result = _run(fleet["env"])
    assert result.returncode == 0
    assert _calls(fleet["calls"]) == []


def test_new_main_rolls_every_host_in_order_and_notifies(fleet):
    target = _advance_origin(fleet["origin"])
    result = _run(fleet["env"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(fleet["deploy"], "rev-parse", "HEAD") == target
    rollouts = [c for c in _calls(fleet["calls"]) if c.startswith("rollout")]
    assert [c.split("--node ")[1].split()[0] for c in rollouts] == ["h1", "h2", "h3"]
    assert any(
        c.startswith("skmail send skfleet-auto-rollout") and "ROLLED" in c
        for c in _calls(fleet["calls"])
    )
    # a second tick finds nothing new and touches nothing
    before = len(_calls(fleet["calls"]))
    assert _run(fleet["env"]).returncode == 0
    assert len(_calls(fleet["calls"])) == before


def test_failed_gate_halts_before_later_hosts_and_exits_nonzero(fleet):
    _advance_origin(fleet["origin"])
    fleet["fail_host"].write_text("h2")
    result = _run(fleet["env"])
    assert result.returncode == 1
    hosts = [
        c.split("--node ")[1].split()[0] for c in _calls(fleet["calls"]) if c.startswith("rollout")
    ]
    assert hosts == ["h1", "h2", "h2"]  # h2 retried, h3 never touched
    assert any("HALTED at h2" in c for c in _calls(fleet["calls"]))


def test_concurrent_run_skips_while_lock_is_held(fleet):
    _advance_origin(fleet["origin"])
    with open(fleet["env"]["SKFLEET_AUTO_ROLLOUT_LOCK"], "w") as handle:
        holder = subprocess.Popen(["flock", "-x", str(handle.name), "sleep", "5"])
        try:
            subprocess.run(["sleep", "0.3"], check=True)
            result = _run(fleet["env"])
        finally:
            holder.terminate()
            holder.wait()
    assert result.returncode == 0
    assert "skipping" in result.stdout
    assert [c for c in _calls(fleet["calls"]) if c.startswith("rollout")] == []


def test_diverged_checkout_is_refused(fleet):
    _advance_origin(fleet["origin"])
    (fleet["deploy"] / "f").write_text("local")
    _git(
        fleet["deploy"],
        "-c",
        "user.email=t@example.com",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "-am",
        "local hand edit",
    )
    result = _run(fleet["env"])
    assert result.returncode == 1
    assert "not an ancestor" in result.stdout
    assert [c for c in _calls(fleet["calls"]) if c.startswith("rollout")] == []


def test_units_install_a_two_minute_oneshot_with_failure_alert():
    service = (UNITS / "skfleet-auto-rollout.service").read_text()
    timer = (UNITS / "skfleet-auto-rollout.timer").read_text()
    assert "Type=oneshot" in service
    assert "OnFailure=skcapstone-alert@skfleet-auto-rollout.service" in service
    assert "scripts/fleet/skfleet-auto-rollout.sh" in service
    assert "OnUnitActiveSec=2min" in timer


def test_merge_during_run_supersedes_quietly_instead_of_halting(fleet):
    """A newer main landing mid-run is handed to the next tick, not reported as a HALT."""
    _advance_origin(fleet["origin"])
    fleet["fail_host"].write_text("h2")
    # The fake skcapstone fails h2's gate; simulate the merge that caused it.
    hook = fleet["deploy"].parent / "bin" / "skcapstone"
    hook.write_text(
        hook.read_text()
        + f'if [ "$host" = h2 ]; then cd {fleet["origin"]} && echo 3 > f && git commit -qam third; fi\n'
    )
    result = _run(fleet["env"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "superseded at h2" in result.stdout
    assert not any("HALTED" in c for c in _calls(fleet["calls"]))
    hosts = [
        c.split("--node ")[1].split()[0] for c in _calls(fleet["calls"]) if c.startswith("rollout")
    ]
    assert hosts == ["h1", "h2"]
