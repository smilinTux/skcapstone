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
    # Never touch the real user manager: services report inactive, timers active.
    _write_exe(
        bins / "systemctl",
        f'echo "systemctl $*" >> {calls}\n'
        'case "$*" in *is-active*.timer) echo active ;; *is-active*) echo inactive ;; esac\n',
    )
    env = dict(
        os.environ,
        PATH=f"{bins}:{os.environ['PATH']}",
        SKFLEET_AUTO_ROLLOUT_REPO=str(deploy),
        SKFLEET_AUTO_ROLLOUT_HOSTS="h1 h2 h3",
        SKFLEET_AUTO_ROLLOUT_LOCK=str(tmp_path / "lock"),
        SKFLEET_AUTO_ROLLOUT_STATE=str(tmp_path / "state" / "rolled-main"),
        SKFLEET_AUTO_ROLLOUT_READY_WAIT="0",
        SKFLEET_AUTO_ROLLOUT_ATTEMPTS="2",
        # Pin every tool the script resolves through *_BIN. A host that exports
        # the live SKCAPSTONE_BIN or SKMAIL_BIN would otherwise run the real CLI
        # and send real HALT alerts from a test (seen 2026-10-09 on chiap08).
        SKCAPSTONE_BIN=str(bins / "skcapstone"),
        SKMAIL_BIN=str(bins / "skmail"),
        SSH_BIN=str(bins / "ssh"),
        SYSTEMCTL_BIN=str(bins / "systemctl"),
        SKFLEET_AUTO_ROLLOUT_NOTIFY="jarvis lumina-nor",
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


def test_busy_dispatch_cycle_defers_the_rollout(fleet):
    """A deploy never lands while the local dispatch cycle runs."""
    _advance_origin(fleet["origin"])
    bins = fleet["deploy"].parent / "bin"
    _write_exe(bins / "systemctl", f'echo "systemctl $*" >> {fleet["calls"]}\necho active\n')
    env = dict(
        fleet["env"],
        SYSTEMCTL_BIN=str(bins / "systemctl"),
        SKFLEET_AUTO_ROLLOUT_QUIET_WAIT="0",
        SKFLEET_AUTO_ROLLOUT_QUIET_UNITS="skfleet-quiet-fixture.service",
    )
    result = _run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "deferring main" in result.stdout
    assert [c for c in _calls(fleet["calls"]) if c.startswith("rollout")] == []
    # the deploy checkout was not fast-forwarded either
    assert _git(fleet["deploy"], "rev-parse", "HEAD") != _git(fleet["origin"], "rev-parse", "HEAD")


def test_retired_dispatcher_units_are_not_waited_on_by_default(fleet):
    """P4b: QUIET_UNITS no longer names the retired seat-cycle or niobe-live units."""
    _advance_origin(fleet["origin"])
    bins = fleet["deploy"].parent / "bin"
    _write_exe(bins / "systemctl", f'echo "systemctl $*" >> {fleet["calls"]}\necho active\n')
    env = dict(fleet["env"], SYSTEMCTL_BIN=str(bins / "systemctl"))
    env.pop("SKFLEET_AUTO_ROLLOUT_QUIET_UNITS", None)
    result = _run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "deferring main" not in result.stdout
    assert not any("is-active" in c for c in _calls(fleet["calls"]))
    assert [
        c.split("--node ")[1].split()[0] for c in _calls(fleet["calls"]) if c.startswith("rollout")
    ] == ["h1", "h2", "h3"]


def test_idle_dispatch_cycle_lets_the_rollout_proceed(fleet):
    _advance_origin(fleet["origin"])
    bins = fleet["deploy"].parent / "bin"
    _write_exe(bins / "systemctl", "echo inactive\n")
    env = dict(fleet["env"], SYSTEMCTL_BIN=str(bins / "systemctl"))
    result = _run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [
        c.split("--node ")[1].split()[0] for c in _calls(fleet["calls"]) if c.startswith("rollout")
    ] == ["h1", "h2", "h3"]


def test_rollout_never_stops_the_cycle_timer(fleet):
    """The chiap08 drift gate requires skfleet-seat-cycle.timer active."""
    _advance_origin(fleet["origin"])
    result = _run(fleet["env"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert not any(
        c.startswith("systemctl --user stop") or c.startswith("systemctl --user start")
        for c in _calls(fleet["calls"])
    )


def test_fixture_never_inherits_live_tool_binaries(fleet, monkeypatch):
    """Live *_BIN exports must not leak into a run: it would page real agents."""
    for name in ("SKCAPSTONE_BIN", "SKMAIL_BIN", "SSH_BIN", "SYSTEMCTL_BIN"):
        assert fleet["env"][name].startswith(str(fleet["deploy"].parent / "bin"))


def test_rolls_when_checkout_head_moved_but_hosts_did_not(fleet):
    """chiap08's own step pulls main into the deploy checkout; a superseded run
    used to leave HEAD == origin/main so later ticks never rolled the builders."""
    old = _git(fleet["deploy"], "rev-parse", "HEAD")
    state = Path(fleet["env"]["SKFLEET_AUTO_ROLLOUT_STATE"])
    state.parent.mkdir(parents=True)
    state.write_text(old + "\n")
    new = _advance_origin(fleet["origin"])
    _git(fleet["deploy"], "fetch", "-q", "origin")
    _git(fleet["deploy"], "merge", "-q", "--ff-only", "origin/main")
    result = _run(fleet["env"])
    assert result.returncode == 0, result.stdout + result.stderr
    hosts = [
        c.split("--node ")[1].split()[0] for c in _calls(fleet["calls"]) if c.startswith("rollout")
    ]
    assert hosts == ["h1", "h2", "h3"]
    assert state.read_text().strip() == new


def test_superseded_run_does_not_record_the_rollout(fleet):
    _advance_origin(fleet["origin"])
    fleet["fail_host"].write_text("h2")
    hook = fleet["deploy"].parent / "bin" / "skcapstone"
    hook.write_text(
        hook.read_text()
        + f'if [ "$host" = h2 ]; then cd {fleet["origin"]} && echo 3 > f && git commit -qam third; fi\n'
    )
    _run(fleet["env"])
    state = Path(fleet["env"]["SKFLEET_AUTO_ROLLOUT_STATE"])
    assert not state.exists()


def test_repeated_halts_escalate_once_to_gtd_and_reset_on_success(fleet):
    """2026-10-25..27: chiap01 halted every tick for two days and nobody saw it,
    because the failure alert only reached the journal. After N halts in a row
    the script captures a GTD item (one source-ref per stuck episode) and sends
    one ESCALATE skmail; a successful rollout clears the streak."""
    env = dict(fleet["env"], SKFLEET_AUTO_ROLLOUT_HALT_ESCALATE="2")
    # Production always has rolled-main; without it a re-run trusts the checkout.
    state = Path(env["SKFLEET_AUTO_ROLLOUT_STATE"])
    state.parent.mkdir(parents=True)
    state.write_text(_git(fleet["deploy"], "rev-parse", "HEAD") + "\n")
    _advance_origin(fleet["origin"])
    fleet["fail_host"].write_text("h2")
    streak = Path(env["SKFLEET_AUTO_ROLLOUT_STATE"]).parent / "halt-streak"

    assert _run(env).returncode == 1
    assert streak.read_text().split()[0] == "1"
    assert not any("gtd capture" in c for c in _calls(fleet["calls"]))

    for _ in range(2):
        assert _run(env).returncode == 1
    captures = [c for c in _calls(fleet["calls"]) if "gtd capture" in c]
    assert len(captures) == 2  # halts 2 and 3; the store dedupes on source-ref
    refs = {c.split("--source-ref ")[1] for c in captures}
    assert len(refs) == 1 and refs.pop().startswith("skfleet-auto-rollout-halt:")
    assert sum("ESCALATE" in c and c.startswith("skmail") for c in _calls(fleet["calls"])) == 2
    # one ESCALATE per recipient (jarvis, lumina-nor), sent only at the threshold

    fleet["fail_host"].unlink()
    assert _run(env).returncode == 0
    assert not streak.exists()


def test_active_call_on_a_host_defers_without_touching_it(fleet, tmp_path):
    """Never restart services under a live skcall-join call."""
    _advance_origin(fleet["origin"])
    bins = Path(fleet["env"]["SSH_BIN"]).parent
    _write_exe(
        bins / "ssh",
        f'echo "ssh $*" >> {fleet["calls"]}\n'
        'case "$*" in *skcall-join*) [ "$3" = h2 ] && echo "skcall-join@room.service loaded active running x" ;; esac\n',
    )
    result = _run(fleet["env"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "h2 has an active call" in result.stdout
    hosts = [
        c.split("--node ")[1].split()[0] for c in _calls(fleet["calls"]) if c.startswith("rollout")
    ]
    assert hosts == ["h1"]
    assert not Path(fleet["env"]["SKFLEET_AUTO_ROLLOUT_STATE"]).exists()


def _fake_ssh_down(fleet, down: str) -> None:
    calls = fleet["calls"]
    _write_exe(
        Path(fleet["env"]["SSH_BIN"]),
        f'echo "ssh $*" >> {calls}\ncase "$*" in *ConnectTimeout*{down}*) exit 255 ;; esac\n',
    )


def test_unreachable_host_is_skipped_alerted_once_and_ring_continues(fleet, tmp_path):
    _advance_origin(fleet["origin"])
    _fake_ssh_down(fleet, "h2")
    _write_exe(tmp_path / "bin" / "sk-alert", f'echo "alert $*" >> {fleet["calls"]}\n')
    env = dict(fleet["env"], SK_ALERT_BIN=str(tmp_path / "bin" / "sk-alert"))
    assert _run(env).returncode == 0
    assert _run(env).returncode == 0
    calls = _calls(fleet["calls"])
    hosts = [c.split("--node ")[1].split()[0] for c in calls if c.startswith("rollout")]
    assert hosts == ["h1", "h3"]  # h2 skipped, h1/h3 not re-rolled on the second tick
    assert sum(c.startswith("alert") and "h2" in c for c in calls) == 1
    assert not (tmp_path / "state" / "rolled-main").exists()  # h2 still owes the main


def _fake_ssh_runs_busy_check_locally(fleet, home: Path, busy_host: str) -> None:
    """ssh records every call; for the busy-lock probe on `busy_host` it runs the
    real remote snippet locally against `home`, so the shell logic is tested."""
    _write_exe(
        Path(fleet["env"]["SSH_BIN"]),
        f'echo "ssh $*" >> {fleet["calls"]}\n'
        'case "$*" in *rollout-busy-locks*) '
        f'[ "$3" = {busy_host} ] && HOME={home} bash -c "${{@: -1}}" ;; esac\n',
    )


def test_host_with_a_held_busy_lock_is_skipped_and_ring_continues(fleet, tmp_path):
    """A release gate holding its lane lock must not get a pip install mid-run."""
    _advance_origin(fleet["origin"])
    home = tmp_path / "home"
    (home / ".config/skfleet").mkdir(parents=True)
    lock = home / "gate-lane1.lock"
    lock.touch()
    (home / ".config/skfleet/rollout-busy-locks").write_text(
        f"# gate lanes\n\n{home}/gate-*.lock\n{home}/absent-*.lock\n"
    )
    _fake_ssh_runs_busy_check_locally(fleet, home, "h2")
    holder = subprocess.Popen(["flock", str(lock), "sleep", "30"])
    try:
        for _ in range(50):  # wait until the holder really has the lock
            if subprocess.run(["flock", "-n", str(lock), "true"]).returncode != 0:
                break
            subprocess.run(["sleep", "0.1"])
        result = _run(fleet["env"])
    finally:
        holder.kill()
        holder.wait()
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"h2 is busy ({lock} held)" in result.stdout
    assert "unreachable or busy, retried next tick: h2" in result.stdout
    hosts = [
        c.split("--node ")[1].split()[0] for c in _calls(fleet["calls"]) if c.startswith("rollout")
    ]
    assert hosts == ["h1", "h3"]
    assert not Path(fleet["env"]["SKFLEET_AUTO_ROLLOUT_STATE"]).exists()


def test_declared_but_idle_busy_locks_do_not_block(fleet, tmp_path):
    _advance_origin(fleet["origin"])
    home = tmp_path / "home"
    (home / ".config/skfleet").mkdir(parents=True)
    (home / "gate-lane1.lock").touch()  # exists, nobody holds it
    (home / ".config/skfleet/rollout-busy-locks").write_text(f"{home}/gate-*.lock\n")
    _fake_ssh_runs_busy_check_locally(fleet, home, "h2")
    result = _run(fleet["env"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "is busy" not in result.stdout
    hosts = [
        c.split("--node ")[1].split()[0] for c in _calls(fleet["calls"]) if c.startswith("rollout")
    ]
    assert hosts == ["h1", "h2", "h3"]


def test_ziowk01_is_in_the_default_ring_last():
    unit = (UNITS / "skfleet-auto-rollout.service").read_text()
    hosts_line = next(line for line in unit.splitlines() if "SKFLEET_AUTO_ROLLOUT_HOSTS=" in line)
    assert hosts_line.rstrip('"').split()[-1] == "ziowk01"
    assert "chiap10 ziowk01}" in SCRIPT.read_text()
