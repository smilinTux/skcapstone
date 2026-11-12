"""Home-scoped daemon PID records fenced to a Linux process incarnation."""

from __future__ import annotations

import fcntl
import json
import os
import signal
from dataclasses import asdict
from functools import wraps
from pathlib import Path

from .seat_cycle_guard import ProcessGeneration, _boot_id, _process_start_ticks


def _locked(function):
    @wraps(function)
    def guarded(home):
        if not home.exists():
            return function(home)
        with (home / ".daemon.pid.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return function(home)

    return guarded


def _generation(pid: int) -> ProcessGeneration:
    return ProcessGeneration(
        pid, _boot_id(Path("/proc")), _process_start_ticks(Path("/proc"), pid)
    )


def _atomic_write(path: Path, data: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(data, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@_locked
def write_daemon_pid(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    record = {**asdict(ProcessGeneration.current()), "home": str(home.resolve())}
    _atomic_write(home / "daemon.pid.identity.json", json.dumps(record))
    _atomic_write(home / "daemon.pid", str(record["pid"]))


def _legacy_home(pid: int) -> Path | None:
    """Prove the legacy CLI's actual home, or refuse an ambiguous process."""
    proc = Path("/proc") / str(pid)
    argv = [part.decode() for part in (proc / "cmdline").read_bytes().split(b"\0") if part]
    if not argv:
        raise RuntimeError("Empty process command line; refusing ambiguous daemon ownership")
    starts = [i for i, arg in enumerate(argv) if Path(arg).name == "skcapstone"]
    starts += [
        i for i, arg in enumerate(argv) if arg == "skcapstone" and i and argv[i - 1] == "-m"
    ]
    if not starts:
        return None
    start = starts[0]
    args = argv[start + 1 :]
    if args[:2] != ["daemon", "start"]:
        raise RuntimeError(
            "PID belongs to an ambiguous SKCapstone process; refusing lifecycle action"
        )
    env = dict(
        part.split(b"=", 1)
        for part in (proc / "environ").read_bytes().split(b"\0")
        if b"=" in part
    )
    user_home = Path(os.fsdecode(env.get(b"HOME", b"")))
    if not user_home.is_absolute():
        raise RuntimeError("Cannot prove legacy daemon HOME")
    root = Path(os.fsdecode(env.get(b"SKCAPSTONE_HOME", str(user_home / ".skcapstone").encode())))

    def option(name: str) -> str | None:
        values = [a.split("=", 1)[1] for a in args if a.startswith(name + "=")]
        values += [args[i + 1] for i, a in enumerate(args[:-1]) if a == name]
        if len(values) > 1 or args[-1:] == [name]:
            raise RuntimeError("Ambiguous legacy daemon options")
        return values[0] if values else None

    agent, explicit_home = option("--agent"), option("--home")
    agent_root = Path(os.fsdecode(env.get(b"SKCAPSTONE_ROOT", str(root).encode())))
    selected = (
        agent_root / "agents" / agent if agent else Path(explicit_home) if explicit_home else root
    )
    if str(selected).startswith("~/"):
        selected = user_home / str(selected)[2:]
    if not selected.is_absolute():
        selected = (proc / "cwd").resolve() / selected
    return selected.resolve()


def _read_daemon_pid(home: Path) -> int | None:
    pid_path = home / "daemon.pid"
    try:
        before = pid_path.stat()
        raw = pid_path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        pid = int(raw.strip())
        if pid <= 0:
            raise ValueError("nonpositive PID")
        generation = _generation(pid)
    except (ValueError, FileNotFoundError, ProcessLookupError):
        pid = None
    except OSError as exc:
        raise RuntimeError("Cannot establish daemon process ownership") from exc
    if pid is not None:
        identity = home / "daemon.pid.identity.json"
        try:
            record = json.loads(identity.read_text())
        except FileNotFoundError:
            try:
                owned = _legacy_home(pid) == home.resolve()
            except (OSError, UnicodeError, ValueError) as exc:
                raise RuntimeError("Cannot establish legacy daemon ownership") from exc
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                "Invalid daemon identity record; refusing lifecycle action"
            ) from exc
        else:
            if not isinstance(record, dict):
                raise RuntimeError("Invalid daemon identity record")
            if record.get("pid") != pid or record.get("home") != str(home.resolve()):
                raise RuntimeError("Daemon PID and identity records disagree")
            owned = record == {**asdict(generation), "home": str(home.resolve())}
            if not owned and _legacy_home(pid) == home.resolve():
                raise RuntimeError("Live legacy daemon conflicts with stale identity record")
        if owned:
            # A concurrent replacement invalidates this snapshot.
            if pid_path.stat().st_ino != before.st_ino or pid_path.read_bytes() != raw:
                raise RuntimeError("Daemon PID changed during ownership verification")
            return pid
    # Remove only the unchanged stale PID reference, never a newer writer's file.
    try:
        if pid_path.stat().st_ino == before.st_ino and pid_path.read_bytes() == raw:
            pid_path.unlink()
        else:
            raise RuntimeError("Daemon PID changed during stale record cleanup")
    except FileNotFoundError:
        pass
    return None


@_locked
def read_daemon_pid(home: Path) -> int | None:
    return _read_daemon_pid(home)


@_locked
def stop_daemon(home: Path) -> int | None:
    pid = _read_daemon_pid(home)
    if pid is None:
        return None
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("Safe daemon stop requires Linux pidfd support")
    generation = _generation(pid)
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return None
    try:
        if _generation(pid) != generation or _read_daemon_pid(home) != pid:
            raise RuntimeError("Daemon process changed before stop")
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
    finally:
        os.close(descriptor)
    return pid


@_locked
def remove_daemon_pid(home: Path) -> None:
    if _read_daemon_pid(home) == os.getpid():
        (home / "daemon.pid").unlink(missing_ok=True)
        (home / "daemon.pid.identity.json").unlink(missing_ok=True)
