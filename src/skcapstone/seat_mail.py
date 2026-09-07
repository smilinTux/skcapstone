"""Small SKMail presence and polling hooks for recurring lifecycle seats.

Mail is coordination only. A mailbox outage must never grant authority or
prevent a bounded Link or Mero observation cycle from failing closed normally.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import socket
import subprocess
import fcntl
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_NEW_COUNT = re.compile(r"\((\d+) new\)\s*$", re.MULTILINE)
_HELP_WORDS = ("help", "handoff", "dependency", "reviewer conflict")


@dataclass(frozen=True)
class MailPoll:
    polled_at: str
    ok: bool
    new_messages: int = 0
    help_or_handoff: int = 0
    digest: str | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "mailbox_poll_at": self.polled_at,
            "mailbox_ok": self.ok,
            "mailbox_new_messages": self.new_messages,
            "mailbox_help_or_handoff": self.help_or_handoff,
            "mailbox_digest": self.digest,
            "mailbox_error": self.error,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mail_command() -> str | None:
    configured = os.environ.get("SKMAIL_BIN")
    if configured:
        return configured
    found = shutil.which("skmail")
    if found:
        return found
    candidate = Path(__file__).resolve().parents[2] / "scripts" / "fleet" / "skmail"
    return str(candidate) if candidate.exists() else None


def _run(command: list[str], *, timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=os.environ.copy(),
    )


def startup_hello(home: Path, seat: str, *, host: str | None = None) -> bool:
    """Send one ordinary startup hello per seat and system boot.

    The marker is intentionally local to the active seat-cycle home. Removing
    it is harmless and causes the next startup to announce itself again.
    """

    command = _mail_command()
    if command is None:
        return False
    marker = Path(home) / "coordination" / "seat-cycles" / f"{seat}.startup.hello"
    marker.parent.mkdir(parents=True, exist_ok=True)
    boot_id = ""
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        boot_id = "unknown-boot"
    try:
        with marker.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            stream.seek(0)
            if stream.read().strip() == boot_id:
                return True
            stream.seek(0)
            stream.truncate()
            stream.write(boot_id)
            stream.flush()
            os.fsync(stream.fileno())
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        local_host = host or socket.gethostname()
        result = _run(
            [
                command,
                "send",
                seat,
                "all",
                "normal",
                f"SEAT-HELLO-{seat}",
                f"seat={seat} host={local_host} lifecycle_presence=started mailbox_poll=enabled",
            ]
        )
        if result.returncode != 0:
            marker.unlink(missing_ok=True)
            return False
        return True
    except (OSError, subprocess.SubprocessError):
        marker.unlink(missing_ok=True)
        return False


def poll_mail(seat: str) -> MailPoll:
    """Read the seat view, which includes direct and ``all`` traffic.

    Reading does not acknowledge anything. This avoids the documented
    all-or-nothing ack trap; a seat acknowledges only after it has acted on
    applicable mail.
    """

    polled_at = _now()
    command = _mail_command()
    if command is None:
        return MailPoll(polled_at, False, error="skmail_not_found")
    try:
        result = _run([command, "read", seat])
    except (OSError, subprocess.SubprocessError) as exc:
        return MailPoll(polled_at, False, error=type(exc).__name__)
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        return MailPoll(polled_at, False, error=output[-240:] or "skmail_read_failed")
    match = _NEW_COUNT.search(result.stdout or "")
    new_messages = int(match.group(1)) if match else 0
    lower = output.lower()
    help_or_handoff = sum(lower.count(word) for word in _HELP_WORDS)
    digest = hashlib.sha256(output.encode("utf-8")).hexdigest()
    return MailPoll(polled_at, True, new_messages, help_or_handoff, digest)
