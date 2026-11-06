"""``scripts/sk-cron-run``: the estate's scheduled-job observability wrapper.

A scheduled job routed through it produces evidence instead of silence: a
run-ledger record, and on failure a GTD item plus a realtime sk-alert, with
the wrapped command's exit code returned unchanged.

These tests moved here with the script on 2026-11-06. It was written in skos,
but the units that most need wrapping are skcapstone's repo-shipped skfleet
seats, which run on fleet nodes where skos is not installed (probed: chiap01
and chiap08 carry skcapstone and sk-alert, neither carries skos). A runner
shipped only by skos could never wrap them, and an ExecStart naming an absent
binary fails the unit at activation rather than degrading, which is worse than
the silence the script exists to end. Same reasoning that moved sk-alert out
of the Hermes runtime on 2026-10-30.

Two defects are pinned here, both found in production:

1. ``sk-alert`` was guarded by ``command -v sk-alert``. It lives in
   ``~/.skenv/bin``, which is on an interactive shell's PATH and on NEITHER
   environment that runs scheduled jobs: a systemd user unit (probed on
   noroc2027: not reachable) or cron, whose PATH is ``/usr/bin:/bin`` and
   whose crontab sets no PATH. The guard silently evaluated false every time,
   so the realtime alert never fired for any scheduled job. The GTD capture
   survived only because it goes through an absolute ``$SKOS_BIN``. Same class
   as the 2026-08-13 watchdog incident, where cron's PATH lacked ``/usr/sbin``
   so ``qm`` exited 127 while the ``kill`` builtin worked fine.

2. The alert was ``sk-alert -k cron-<job>`` with no ``-t``. sk-alert documents
   a key without a ttl as "once EVER", so every job alerted on its first
   failure in history and never again: nor's lumina sync last alerted
   2026-09-19 while failing every 3h for weeks. sk-alert was right; the caller
   asked for the wrong thing. The incident is now owned by sk-cron-run.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CRON_RUN = REPO / "scripts" / "sk-cron-run"
FAIL = ["/bin/sh", "-c", "echo boom >&2; exit 3"]
OK = ["/bin/sh", "-c", "echo fine"]


def _base_env(tmp_path, home):
    """A SCHEDULER's environment: no ``~/.skenv/bin`` anywhere on PATH."""
    return dict(
        os.environ,
        HOME=str(home),
        SK_GTD_DIR=str(tmp_path / "gtd"),
        SKOS_BIN="/nonexistent/skos",
        PATH="/usr/bin:/bin",
        PY="/usr/bin/python3",
        SK_CRON_STATE_DIR=str(tmp_path / "state"),
        SK_CRON_HOST="testhost",
    )


@pytest.fixture
def rig(tmp_path):
    """HOME with a recording sk-alert whose exit code is settable."""
    home = tmp_path / "home"
    skenv = home / ".skenv" / "bin"
    skenv.mkdir(parents=True)
    receipt = tmp_path / "alerts.txt"
    rcfile = tmp_path / "alert-rc"
    rcfile.write_text("0")
    alert = skenv / "sk-alert"
    # Records ARGUMENTS only, deliberately: the real sk-alert takes its message
    # as an argument and rejects an empty one ("sk_alert: empty message"), so a
    # stub that also drained stdin would happily pass a wrapper that pipes.
    # Exits with whatever rcfile says, to model a failed send.
    alert.write_text(
        "#!/bin/sh\n" f'printf "%s\\n" "$*" >> "{receipt}"\n' f'exit "$(cat "{rcfile}")"\n',
        encoding="utf-8",
    )
    alert.chmod(0o755)
    env = _base_env(tmp_path, home)

    def run(cmd, job="flaky", **extra):
        e = dict(env, **extra)
        return subprocess.run(
            [str(CRON_RUN), job, *cmd], env=e, capture_output=True, text=True, timeout=60
        )

    def alerts():
        return receipt.read_text().splitlines() if receipt.exists() else []

    return run, alerts, rcfile, tmp_path / "state"


# ── the wrapped command's exit code, and the ledger ─────────────────────────


def test_the_wrapped_exit_code_passes_through_unchanged(rig):
    run, _, _, _ = rig
    assert run(FAIL).returncode == 3
    assert run(OK, job="happy").returncode == 0
    assert run(["/bin/sh", "-c", "exit 7"], job="seven").returncode == 7


def test_every_run_appends_one_ledger_record(rig, tmp_path):
    run, _, _, _ = rig
    run(FAIL)
    run(OK)
    ledger = tmp_path / "home" / ".skcapstone" / "logs" / "cron-ledger.jsonl"
    records = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(records) == 2, records
    bad, good = records
    assert bad["job"] == "flaky" and bad["host"] == "testhost"
    assert bad["exit"] == 3 and bad["ok"] is False and "boom" in bad["tail"]
    assert isinstance(bad["dur_s"], int) and bad["start"]
    assert good["exit"] == 0 and good["ok"] is True


def test_the_wrapped_commands_output_is_still_printed(rig):
    run, _, _, _ = rig
    assert "fine" in run(OK).stdout


# ── the alert must reach sk-alert from a bare scheduler PATH ────────────────


def test_alert_fires_on_failure_from_a_bare_scheduler_path(rig):
    """Fail-before: the guard `command -v sk-alert` was false, so nothing fired."""
    run, alerts, _, _ = rig
    assert run(FAIL, job="canary-job").returncode == 3
    got = alerts()
    assert got, "sk-alert was never invoked from a bare scheduler PATH"
    assert "canary-job" in got[0]
    assert "crit" in got[0]
    # The message must arrive as an ARGUMENT. sk-alert does not read stdin: it
    # exits 2 with "sk_alert: empty message", which `|| true` then swallowed,
    # so a piped message was an alert that looked sent and never was.
    assert "cron FAILED" in got[0], "the message was not passed as an argument"
    assert "boom" in got[0], "the failing job's output never reached the alert"


def test_no_alert_on_success(rig):
    run, alerts, _, _ = rig
    assert run(OK).returncode == 0
    assert alerts() == []


def test_a_missing_sk_alert_does_not_break_the_wrapper(tmp_path):
    """No alerter installed is not a reason to change the job's exit code."""
    home = tmp_path / "empty-home"
    home.mkdir()
    proc = subprocess.run(
        [str(CRON_RUN), "no-alerter", "/bin/sh", "-c", "exit 7"],
        env=_base_env(tmp_path, home),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 7
    assert "no sk-alert binary found" in proc.stderr


# ── one alert per INCIDENT, not one per key forever ─────────────────────────


def test_streak_alerts_once_within_the_repeat_window(rig):
    run, alerts, _, _ = rig
    for _ in range(4):
        assert run(FAIL).returncode == 3
    got = alerts()
    assert len(got) == 1, got
    assert "cron FAILED on testhost: flaky (exit 3)" in got[0]
    assert "boom" in got[0] and "crit" in got[0]


def test_streak_re_alerts_once_the_interval_has_passed(rig):
    run, alerts, _, _ = rig
    run(FAIL, SK_CRON_ALERT_REPEAT_S="0")
    run(FAIL, SK_CRON_ALERT_REPEAT_S="0")
    got = alerts()
    assert len(got) == 2
    assert "STILL FAILING" in got[1] and "2 failed runs since" in got[1]


def test_recovery_is_announced_once_and_closes_the_incident(rig):
    run, alerts, _, state = rig
    run(FAIL)
    run(FAIL)
    assert run(OK).returncode == 0
    assert run(OK).returncode == 0
    got = alerts()
    assert len(got) == 2, got
    assert "cron RECOVERED on testhost: flaky ok again after 2 failed run(s)" in got[1]
    assert "info" in got[1]
    assert not list(state.glob("*.incident"))
    # A new failure after recovery is a NEW incident and alerts immediately.
    run(FAIL)
    assert len(alerts()) == 3 and "cron FAILED" in alerts()[2]


def test_success_without_an_incident_is_silent(rig):
    run, alerts, _, _ = rig
    assert run(OK).returncode == 0
    assert alerts() == []


def test_undelivered_alert_does_not_start_the_quiet_window(rig):
    run, alerts, rcfile, _ = rig
    rcfile.write_text("1")  # telegram down
    run(FAIL)
    rcfile.write_text("0")
    run(FAIL)  # still inside 6h of the first attempt, but nothing was delivered
    got = alerts()
    assert len(got) == 2 and "STILL FAILING" in got[1]
    run(FAIL)
    assert len(alerts()) == 2  # now delivered, so the window holds


def test_jobs_have_independent_incidents(rig):
    run, alerts, _, _ = rig
    run(FAIL, job="a")
    run(FAIL, job="b")
    run(FAIL, job="a")
    assert [line.split(": ")[1].split(" ")[0] for line in alerts()] == ["a", "b"]


def test_legacy_once_ever_key_is_not_passed(rig):
    run, alerts, _, _ = rig
    run(FAIL)
    assert "-k" not in alerts()[0].split()


# ── each incident is one Talk thread, and the recovery closes it ────────────


def _threaded(rig_alert: Path) -> None:
    """Make the fake sk-alert look like one that knows --thread/--resolve."""
    rig_alert.write_text(rig_alert.read_text() + "# case --resolve) --thread)\n", encoding="utf-8")


def test_old_sk_alert_gets_no_thread_flags(rig):
    """An sk-alert without --thread would reject it and drop the alert."""
    run, alerts, _, _ = rig
    run(FAIL)
    run(OK)
    got = alerts()
    assert len(got) == 2, got
    assert all("--thread" not in a and "--resolve" not in a for a in got), got


def test_incident_is_one_thread_and_recovery_resolves_it(rig, tmp_path):
    """Chef replies in the incident's thread; the recovery closes it."""
    run, alerts, _, _ = rig
    _threaded(tmp_path / "home" / ".skenv" / "bin" / "sk-alert")
    run(FAIL, SK_CRON_ALERT_REPEAT_S="0")
    run(FAIL, SK_CRON_ALERT_REPEAT_S="0")
    run(OK)
    got = alerts()
    assert len(got) == 3, got
    for a in got:
        assert "--thread cron/testhost/flaky" in a, a
        assert "--title cron flaky on testhost" in a, a
    assert "--resolve" not in got[0] and "--resolve" not in got[1]
    assert "--resolve" in got[2] and "RECOVERED" in got[2]


# ── the GTD capture is the one optional, skos-owned step ────────────────────


def test_the_gtd_store_is_never_written_directly():
    """Capture goes through skos's locked sink, never inline JSON surgery.

    The ledger append may json.dumps a record; nothing here may load or
    rewrite the GTD store files.
    """
    body = CRON_RUN.read_text(encoding="utf-8")
    assert "json.dump(" not in body
    assert "json.load(" not in body
    assert "GTD_INBOX" not in body
    assert "gtd capture" in body


def test_a_node_without_skos_still_gets_the_ledger_and_the_alert(rig, tmp_path):
    """skos absent is a supported configuration on fleet nodes, not a fault.

    Every skcapstone -> skos import in src/ is a guarded try/except and skos is
    not in deploy/fleet-constraints.txt, so the wrapper must degrade to
    "GTD capture skipped" rather than report a capture failure. The ledger
    record and the realtime alert are unaffected.
    """
    run, alerts, _, _ = rig
    # PY has no skos on its path, and SKOS_BIN does not exist.
    proc = run(FAIL, job="no-skos", PY="/usr/bin/python3")
    assert proc.returncode == 3
    assert "skos not installed, GTD capture skipped for cron:no-skos@" in proc.stderr
    assert "failed" not in proc.stderr.lower(), proc.stderr
    assert alerts() and "cron FAILED" in alerts()[0]
    ledger = tmp_path / "home" / ".skcapstone" / "logs" / "cron-ledger.jsonl"
    assert json.loads(ledger.read_text().splitlines()[0])["exit"] == 3


# ── the acting agent, declared once per node and derived here ───────────────
#
# cron reads neither environment.d nor .bashrc, so without this a crontab job
# arrives with no selector and skmemory's get_active_agent() returns None.


SELECTORS = ("SKAGENT", "SKCAPSTONE_AGENT", "SKMEMORY_AGENT", "SK_DEFAULT_AGENT")

PRINT_SELECTORS = [
    "/bin/sh",
    "-c",
    'for v in SKAGENT SKCAPSTONE_AGENT SKMEMORY_AGENT SK_DEFAULT_AGENT UNRELATED; do '
    'eval "printf \'%s=%s\\n\' \\"$v\\" \\"\\${$v:-<unset>}\\""; done',
]


@pytest.fixture
def agent_env(tmp_path):
    """A node's agent declaration, in environment.d's literal KEY=VALUE form."""
    path = tmp_path / "10-skagent.conf"
    path.write_text(
        "# the acting agent for this node\n"
        "SKAGENT=jarvis\n"
        "SKCAPSTONE_AGENT=jarvis\n"
        "SKMEMORY_AGENT=jarvis\n"
        "SK_DEFAULT_AGENT=jarvis\n"
        "UNRELATED=must-not-leak\n"
    )
    return path


def _selectors_from(proc):
    return dict(
        ln.split("=", 1) for ln in proc.stdout.splitlines() if "=" in ln
    )


def test_a_cron_job_with_no_selector_derives_the_declared_agent(rig, agent_env):
    run, _, _, _ = rig
    cleared = {name: "" for name in SELECTORS}
    proc = run(PRINT_SELECTORS, job="agent", SK_AGENT_ENV=str(agent_env), **cleared)
    got = _selectors_from(proc)
    assert [got[name] for name in SELECTORS] == ["jarvis"] * 4, proc.stdout


def test_only_the_four_selectors_are_taken_from_the_file(rig, agent_env):
    run, _, _, _ = rig
    cleared = {name: "" for name in SELECTORS}
    proc = run(PRINT_SELECTORS, job="agent", SK_AGENT_ENV=str(agent_env), **cleared)
    assert _selectors_from(proc)["UNRELATED"] == "<unset>", proc.stdout


def test_an_explicit_agent_overrides_the_whole_file_not_part_of_it(rig, agent_env):
    """Half a declaration resolves to None just as surely as none at all.

    get_active_agent() requires every selector that is set to agree, so deriving
    three values from the file beside a caller's one would manufacture exactly
    the conflict this is supposed to prevent. One set selector therefore leaves
    the file entirely untouched.
    """
    run, _, _, _ = rig
    cleared = {name: "" for name in SELECTORS}
    proc = run(
        PRINT_SELECTORS, job="agent", SK_AGENT_ENV=str(agent_env), **{**cleared, "SKAGENT": "lumina"}
    )
    got = _selectors_from(proc)
    assert got["SKAGENT"] == "lumina"
    assert [got[n] for n in SELECTORS[1:]] == ["<unset>"] * 3, proc.stdout


def test_a_partially_declared_environment_is_also_left_alone(rig, agent_env):
    run, _, _, _ = rig
    cleared = {name: "" for name in SELECTORS}
    proc = run(
        PRINT_SELECTORS,
        job="agent",
        SK_AGENT_ENV=str(agent_env),
        **{**cleared, "SKMEMORY_AGENT": "opus"},
    )
    got = _selectors_from(proc)
    assert got["SKMEMORY_AGENT"] == "opus"
    assert got["SKAGENT"] == "<unset>", proc.stdout


def test_a_node_with_no_declaration_file_still_runs_the_job(rig, tmp_path):
    run, _, _, _ = rig
    cleared = {name: "" for name in SELECTORS}
    proc = run(
        OK, job="no-decl", SK_AGENT_ENV=str(tmp_path / "absent.conf"), **cleared
    )
    assert proc.returncode == 0
    assert "error" not in proc.stderr.lower(), proc.stderr


def test_the_declaration_is_not_shell_parsed(rig, tmp_path):
    """environment.d is literal KEY=VALUE. The systemd generator does not shell-
    parse it, so neither may we: a value two readers expand differently drifts."""
    path = tmp_path / "10-skagent.conf"
    path.write_text("SKAGENT=$(touch /tmp/sk-cron-run-pwned)\n")
    run, _, _, _ = rig
    cleared = {name: "" for name in SELECTORS}
    proc = run(PRINT_SELECTORS, job="literal", SK_AGENT_ENV=str(path), **cleared)
    assert _selectors_from(proc)["SKAGENT"] == "$(touch /tmp/sk-cron-run-pwned)"
    assert not os.path.exists("/tmp/sk-cron-run-pwned")
