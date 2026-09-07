"""Tests for recurring-seat SKMail presence and polling."""

from __future__ import annotations

from pathlib import Path

from skcapstone import seat_mail


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
