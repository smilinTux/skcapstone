"""sk-alert is the estate's alert primitive, so its refusals have to be loud.

It moved into skcapstone on 2026-10-30 because skcapstone's own ops scripts
(skcapstone-gfs-backup.sh, sync-conflict-guard.sh, skfleet-auto-rollout.sh) are
its biggest callers and every one of them guards with `[ -x "$bin/sk-alert" ]`.
While the primitive lived in ~/.hermes, a node without that runtime made every
such call a silent no-op: on 2026-10-30 all four chi nodes had skcapstone and
none had sk-alert, so the conflict-guard timer had been reporting Syncthing
conflicts to nobody.

These tests need no network. They cover the paths that decide whether an alert
is delivered at all.
"""

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sk-alert"


def run(home: Path, *args: str, env: dict | None = None):
    e = {
        "HOME": os.environ["HOME"],
        "PATH": os.environ["PATH"],
        "SKCAPSTONE_HOME": str(home),
        # Keep the legacy Hermes file out of it; a dev box may have one.
        "SKALERT_ENV_FILE": "/dev/null",
    }
    e.update(env or {})
    return subprocess.run(
        ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=e, timeout=60
    )


def test_script_is_shipped_and_executable():
    assert SCRIPT.is_file(), SCRIPT
    assert os.access(SCRIPT, os.X_OK), "sk-alert must stay executable"


def test_bash_syntax_is_valid():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_unconfigured_node_refuses_loudly(tmp_path):
    """The worst outcome is exit 0 with nothing delivered."""
    (tmp_path / "config").mkdir()
    r = run(tmp_path, "nothing is configured here")
    assert r.returncode == 1, r.stderr
    assert "no delivery channel configured" in r.stderr
    # and it must say where to fix it, naming this node's own config path
    assert "config/alert.env" in r.stderr


def test_sourceable_under_set_e(tmp_path):
    """Callers source this file; a stray non-zero at top level would abort them.

    skmem-health.sh does exactly this, so the guards that apply the caller's
    environment all end in `|| :`.
    """
    (tmp_path / "config").mkdir()
    r = subprocess.run(
        ["bash", "-c", f"set -euo pipefail; source {SCRIPT}; echo SOURCED_OK"],
        capture_output=True,
        text=True,
        env={
            "HOME": os.environ["HOME"],
            "PATH": os.environ["PATH"],
            "SKCAPSTONE_HOME": str(tmp_path),
            "SKALERT_ENV_FILE": "/dev/null",
        },
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "SOURCED_OK" in r.stdout


def test_no_talk_room_means_no_talk_post(tmp_path):
    """A node with no room configured must not post to Talk at all.

    This is the guard that keeps one estate's alerts out of another estate's
    skhub: a wrong room is a cross-estate leak, so absence of a room is a skip,
    never a fallback to some default room.
    """
    cfg = tmp_path / "config"
    cfg.mkdir()
    cfg.joinpath("alert.env").write_text(
        "SKALERT_TALK_DEFAULT=1\n"
        "NEXTCLOUD_TALK_SERVER_URL=https://example.invalid\n"
        "NEXTCLOUD_TALK_USERNAME=nobody\n"
    )
    r = run(tmp_path, "no room configured")
    assert r.returncode == 1, r.stderr
    log = (tmp_path / "logs" / "skalert.log").read_text()
    assert "no SKALERT_TALK_ROOM configured" in log
    assert "TALK ok" not in log


def test_no_talk_flag_overrides_estate_default(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    cfg.joinpath("alert.env").write_text("SKALERT_TALK_DEFAULT=1\nSKALERT_TALK_ROOM=sometoken\n")
    r = run(tmp_path, "--no-talk", "suppressed")
    assert r.returncode == 1, r.stderr
    assert "no delivery channel configured" in r.stderr


def test_empty_message_is_a_usage_error(tmp_path):
    (tmp_path / "config").mkdir()
    r = run(tmp_path, "")
    assert r.returncode == 2
    assert "empty message" in r.stderr


def test_secret_file_is_not_required_to_exist(tmp_path):
    """alert.secret is node-local and optional; absence must not crash."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    cfg.joinpath("alert.env").write_text("SKALERT_TALK_ROOM=tok\n")
    r = run(tmp_path, "secretless")
    # No credentials, so nothing can be delivered, but it must fail cleanly.
    assert r.returncode == 1
    assert "Traceback" not in r.stderr
