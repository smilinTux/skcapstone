"""Tests for recurring-seat SKMail presence and polling."""

from __future__ import annotations

import subprocess
from pathlib import Path

from skcapstone import seat_mail


def test_mail_command_prefers_active_interpreter_sibling(monkeypatch, tmp_path: Path) -> None:
    sibling = tmp_path / "skmail"
    sibling.write_text("#!/bin/sh\n")
    sibling.chmod(0o755)
    monkeypatch.setattr(seat_mail.sys, "executable", str(tmp_path / "python"))
    monkeypatch.setattr(seat_mail.Path, "resolve", lambda path: path)
    monkeypatch.setattr(seat_mail.shutil, "which", lambda name: "/path/skmail")
    assert seat_mail._mail_command() == str(sibling)


def test_mail_command_preserves_virtualenv_sibling_with_restricted_path(
    monkeypatch, tmp_path: Path
) -> None:
    interpreter = tmp_path / "python"
    interpreter.symlink_to("/usr/bin/python3")
    sibling = tmp_path / "skmail"
    sibling.write_text("#!/bin/sh\n")
    sibling.chmod(0o755)
    monkeypatch.setattr(seat_mail.sys, "executable", str(interpreter))
    monkeypatch.setattr(seat_mail.shutil, "which", lambda name: None)
    assert seat_mail._mail_command() == str(sibling)


def test_mail_command_uses_bounded_path_fallback(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(seat_mail.sys, "executable", str(tmp_path / "python"))
    monkeypatch.setattr(seat_mail.Path, "resolve", lambda path: path)
    monkeypatch.setattr(seat_mail.shutil, "which", lambda name: "/path/skmail")
    assert seat_mail._mail_command() == "/path/skmail"


def test_mail_command_absence_is_explicit(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(seat_mail.sys, "executable", str(tmp_path / "python"))
    monkeypatch.setattr(seat_mail.Path, "resolve", lambda path: path)
    monkeypatch.setattr(seat_mail.shutil, "which", lambda name: None)
    assert seat_mail._mail_command() is None


def test_startup_hello_is_once_and_retries_after_failure(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = "sent"
        stderr = ""

    monkeypatch.setattr(seat_mail, "_mail_command", lambda: "skmail")
    monkeypatch.setattr(
        seat_mail,
        "_run",
        lambda command, timeout=5.0: calls.append(command) or Result(),
    )
    assert seat_mail.startup_hello(tmp_path, "mero", host="chiap08") is True
    assert seat_mail.startup_hello(tmp_path, "mero", host="chiap08") is True
    assert len(calls) == 1


def test_poll_reads_direct_and_all_view_without_ack(monkeypatch) -> None:
    class Result:
        returncode = 0
        stdout = "[NORMAL] link -> all re HELP: handoff\n(2 new)"
        stderr = ""

    calls: list[list[str]] = []
    monkeypatch.setattr(seat_mail, "_mail_command", lambda: "skmail")
    monkeypatch.setattr(
        seat_mail,
        "_run",
        lambda command, timeout=5.0: calls.append(command) or Result(),
    )
    result = seat_mail.poll_mail("mero")
    assert result.ok is True
    assert result.new_messages == 2
    assert result.help_or_handoff >= 2
    assert calls == [["skmail", "read", "mero"]]


def test_poll_reports_bounded_nonzero_without_output(monkeypatch) -> None:
    class Result:
        returncode = 7
        stdout = ""
        stderr = "password=must-not-escape"

    monkeypatch.setattr(seat_mail, "_mail_command", lambda: "skmail")
    monkeypatch.setattr(seat_mail, "_run", lambda command, timeout=5.0: Result())
    result = seat_mail.poll_mail("mero")
    assert result.ok is False
    assert result.error == "skmail_exit_7"
    assert "password" not in result.error


def test_poll_reports_timeout_without_ack(monkeypatch) -> None:
    def timeout(command, timeout=5.0):
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(seat_mail, "_mail_command", lambda: "skmail")
    monkeypatch.setattr(seat_mail, "_run", timeout)
    result = seat_mail.poll_mail("mero")
    assert result.ok is False
    assert result.error == "TimeoutExpired"
