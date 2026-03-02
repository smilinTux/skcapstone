"""
tests/test_e2e_automated.py — Automated multi-agent E2E test via subprocess.

Starts the real skcapstone daemon, injects a message into the inbox,
and verifies that a response appears within the timeout window.

Marks are applied so the test is automatically skipped in unit-test
environments where the CLI is not installed or system requirements
are not met.

Run manually:
    pytest tests/test_e2e_automated.py -v -s --timeout=360
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Skip guards
# ---------------------------------------------------------------------------

pytestmark = [
    pytest.mark.skipif(
        not shutil.which("skcapstone"),
        reason="skcapstone CLI not installed — skipping live E2E",
    ),
    pytest.mark.e2e,
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DAEMON_PORT = int(os.environ.get("E2E_PORT", "17777"))  # offset to avoid collision
STARTUP_WAIT = int(os.environ.get("E2E_STARTUP_WAIT", "10"))
POLL_TIMEOUT = int(os.environ.get("E2E_POLL_TIMEOUT", "300"))
PEER = os.environ.get("E2E_PEER", "test-peer")
AGENT_HOME = Path(
    os.environ.get("SKCAPSTONE_ROOT", os.environ.get("SKCAPSTONE_HOME", "~/.skcapstone"))
).expanduser()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_test_message(inbox_dir: Path, peer: str) -> tuple[Path, str]:
    """Write a test .skc.json message to inbox_dir and return (path, msg_id)."""
    ts = int(time.time())
    msg_id = f"e2e-auto-{ts}"
    msg = {
        "sender": peer,
        "recipient": "Opus",
        "payload": {
            "content": f"Ping test — automated pytest E2E at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
            "content_type": "text",
        },
        "message_id": msg_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
    }
    path = inbox_dir / f"{msg_id}.skc.json"
    path.write_text(json.dumps(msg))
    return path, msg_id


def _poll_for_response(
    outbox_dir: Path,
    conv_file: Path,
    inbox_msg_path: Path,
    timeout_secs: int,
) -> bool:
    """
    Return True if a response is detected within timeout_secs.

    Detection strategy (either satisfies the check):
    1. A new .skc.json appears in outbox_dir AFTER inbox_msg_path's mtime.
    2. conv_file is created/updated AFTER inbox_msg_path's mtime.
    """
    outbox_dir.mkdir(parents=True, exist_ok=True)
    ref_mtime = inbox_msg_path.stat().st_mtime

    deadline = time.monotonic() + timeout_secs
    poll_interval = 2.0
    last_log = time.monotonic()

    while time.monotonic() < deadline:
        # Check outbox for new envelope
        for skc in outbox_dir.glob("*.skc.json"):
            if skc.stat().st_mtime > ref_mtime:
                return True

        # Check conversations file (passthrough / no-SKComm fallback)
        if conv_file.exists() and conv_file.stat().st_mtime > ref_mtime:
            return True

        now = time.monotonic()
        if now - last_log >= 30:
            elapsed = timeout_secs - (deadline - now)
            print(f"\n  [e2e] still waiting… {elapsed:.0f}s elapsed / {timeout_secs}s timeout")
            last_log = now

        time.sleep(poll_interval)

    return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def daemon_process():
    """
    Start the skcapstone daemon in the background for the duration of the module.

    Yields the subprocess.Popen handle; tears down on module exit.
    """
    log_fd, log_path = tempfile.mkstemp(prefix="skcapstone-e2e-", suffix=".log")

    env = os.environ.copy()
    env.setdefault("SKCAPSTONE_ROOT", str(AGENT_HOME))

    proc = subprocess.Popen(
        [
            "skcapstone",
            "daemon",
            "start",
            "--foreground",
            "--port",
            str(DAEMON_PORT),
        ],
        stdout=log_fd,
        stderr=log_fd,
        env=env,
        preexec_fn=os.setsid,  # separate process group for clean teardown
    )
    os.close(log_fd)

    print(f"\n  [e2e] Daemon started (PID {proc.pid}) — log: {log_path}")
    print(f"  [e2e] Waiting {STARTUP_WAIT}s for startup…")
    time.sleep(STARTUP_WAIT)

    if proc.poll() is not None:
        with open(log_path) as fh:
            tail = fh.read()[-2000:]
        pytest.fail(
            f"Daemon exited prematurely (rc={proc.returncode}).\n"
            f"Log tail:\n{tail}"
        )

    yield proc, log_path

    # Teardown — send SIGTERM to the whole process group
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
    print(f"\n  [e2e] Daemon stopped. Log: {log_path}")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDaemonStartup:
    """Verify the daemon starts and exposes its HTTP API."""

    def test_daemon_is_running(self, daemon_process):
        """The daemon subprocess must still be alive after startup wait."""
        proc, _ = daemon_process
        assert proc.poll() is None, "Daemon exited before tests ran"

    def test_consciousness_endpoint_responds(self, daemon_process):
        """GET /consciousness must return valid JSON."""
        import urllib.request
        import urllib.error

        url = f"http://127.0.0.1:{DAEMON_PORT}/consciousness"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                body = resp.read().decode()
                data = json.loads(body)
        except urllib.error.URLError as exc:
            pytest.fail(f"/consciousness unreachable on port {DAEMON_PORT}: {exc}")

        assert isinstance(data, dict), f"Expected JSON object, got: {body[:200]}"
        # The endpoint should include some status indicator
        assert data, "Response JSON is empty"

    def test_consciousness_status_active(self, daemon_process):
        """The /consciousness endpoint should report an active/running status."""
        import urllib.request

        url = f"http://127.0.0.1:{DAEMON_PORT}/consciousness"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())

        status = str(data.get("status", "")).lower()
        # Accept various status strings that indicate the loop is running
        active_statuses = {"active", "ok", "running", "started", "conscious"}
        assert status in active_statuses or data.get("conscious") is True, (
            f"Expected active status, got: {data}"
        )


class TestMessageRoundTrip:
    """End-to-end: inject message → daemon processes → response appears."""

    @pytest.fixture(autouse=True)
    def _setup_dirs(self):
        """Ensure inbox and outbox directories exist before each test."""
        inbox = AGENT_HOME / "sync" / "comms" / "inbox" / PEER
        outbox = AGENT_HOME / "sync" / "comms" / "outbox" / PEER
        inbox.mkdir(parents=True, exist_ok=True)
        outbox.mkdir(parents=True, exist_ok=True)

    def test_inbox_message_is_processed(self, daemon_process):
        """
        Writing a .skc.json to the inbox triggers the consciousness loop
        and produces a response in the outbox OR updates conversations/.
        """
        inbox_dir = AGENT_HOME / "sync" / "comms" / "inbox" / PEER
        outbox_dir = AGENT_HOME / "sync" / "comms" / "outbox" / PEER
        conv_file = AGENT_HOME / "conversations" / f"{PEER}.json"

        msg_path, msg_id = _write_test_message(inbox_dir, PEER)
        print(f"\n  [e2e] Message written: {msg_path} (id={msg_id})")

        found = _poll_for_response(
            outbox_dir=outbox_dir,
            conv_file=conv_file,
            inbox_msg_path=msg_path,
            timeout_secs=POLL_TIMEOUT,
        )

        assert found, (
            f"No response detected within {POLL_TIMEOUT}s.\n"
            f"  inbox_dir:  {inbox_dir}\n"
            f"  outbox_dir: {outbox_dir}\n"
            f"  conv_file:  {conv_file}"
        )

    def test_conversations_file_updated(self, daemon_process):
        """
        After a message is processed, ~/.skcapstone/conversations/<peer>.json
        must exist and contain valid JSON with the peer's conversation history.
        """
        inbox_dir = AGENT_HOME / "sync" / "comms" / "inbox" / PEER
        outbox_dir = AGENT_HOME / "sync" / "comms" / "outbox" / PEER
        conv_file = AGENT_HOME / "conversations" / f"{PEER}.json"

        msg_path, msg_id = _write_test_message(inbox_dir, PEER)
        print(f"\n  [e2e] Message written: {msg_path} (id={msg_id})")

        # Wait for conversations file to appear
        deadline = time.monotonic() + POLL_TIMEOUT
        ref_mtime = msg_path.stat().st_mtime
        while time.monotonic() < deadline:
            if conv_file.exists() and conv_file.stat().st_mtime >= ref_mtime:
                break
            time.sleep(2)

        assert conv_file.exists(), (
            f"conversations/{PEER}.json not found after {POLL_TIMEOUT}s"
        )

        content = conv_file.read_text()
        data = json.loads(content)  # raises if invalid JSON
        assert isinstance(data, (dict, list)), (
            f"Unexpected conversations format: {content[:200]}"
        )
