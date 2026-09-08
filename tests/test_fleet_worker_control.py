"""Focused unit tests for claim-scoped fleet worker control."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from skcapstone.fleet_worker_control import (
    ControlError,
    WorkerIdentity,
    append_json_event,
    completion_race,
    parse_control,
    read_workspace_control,
    reconcile_graceful_cancel,
    redact_terminal,
)

NOW = dt.datetime(2026, 9, 4, 12, tzinfo=dt.timezone.utc)
IDENTITY = WorkerIdentity("f8c1a2d7", "worker-f8c1a2d7", "rev-9", "chiap04", "codex")


def payload(**changes: str) -> dict[str, str]:
    """Build one valid request with selected replacements."""
    value = {
        "command": "status",
        "card_id": IDENTITY.card_id,
        "owner": IDENTITY.owner,
        "claim_revision": IDENTITY.claim_revision,
        "host": IDENTITY.host,
        "lane": IDENTITY.lane,
        "request_id": "request-0001",
        "expires_at": "2026-09-04T12:01:00Z",
    }
    value.update(changes)
    return value


def encoded(**changes: str) -> bytes:
    """Serialize a test request rather than constructing JSON text."""
    return json.dumps(payload(**changes)).encode()


@pytest.mark.parametrize("command", ["status", "checkpoint", "graceful-cancel"])
def test_parser_accepts_only_control_allowlist(command: str) -> None:
    assert parse_control(encoded(command=command), IDENTITY, now=NOW).command == command


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"command": "shell"}, "not allowed"),
        ({"card_id": "aaaaaaaa"}, "identity mismatch"),
        ({"owner": "other"}, "identity mismatch"),
        ({"claim_revision": "rev-10"}, "identity mismatch"),
        ({"host": "chiap03"}, "identity mismatch"),
        ({"lane": "glm"}, "identity mismatch"),
        ({"expires_at": "2026-09-04T11:59:59Z"}, "expired"),
        ({"request_id": "api_key=abcdefgh"}, "invalid request"),
    ],
)
def test_parser_fails_closed_for_mismatch_expiry_and_material(
    changes: dict[str, str], message: str
) -> None:
    with pytest.raises(ControlError, match=message):
        parse_control(encoded(**changes), IDENTITY, now=NOW)


def test_parser_rejects_replay_malformed_extra_fields_and_shell_text() -> None:
    with pytest.raises(ControlError, match="replayed"):
        parse_control(encoded(), IDENTITY, {"request-0001"}, NOW)
    with pytest.raises(ControlError, match="schema"):
        bad = payload()
        bad["shell"] = "rm -rf /"
        parse_control(json.dumps(bad).encode(), IDENTITY, now=NOW)
    with pytest.raises(ControlError, match="invalid JSON"):
        parse_control(b"status; /bin/sh", IDENTITY, now=NOW)


def test_reader_uses_only_workspace_control_json_and_rejects_symlink(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "control.json"
    outside.write_bytes(encoded(command="graceful-cancel"))
    assert read_workspace_control(workspace, IDENTITY, now=NOW) is None

    path = workspace / "control.json"
    path.symlink_to(outside)
    with pytest.raises(ControlError, match="regular file"):
        read_workspace_control(workspace, IDENTITY, now=NOW)

    path.unlink()
    path.write_bytes(encoded(command="checkpoint"))
    assert read_workspace_control(workspace, IDENTITY, now=NOW).command == "checkpoint"


def test_redaction_and_bounded_zero_byte_event(tmp_path: Path) -> None:
    text = "authorization: Bearer hunter2\napi_key=sk-abcdefghijklmnop\nfinished"
    redacted = redact_terminal(text)
    assert "hunter2" not in redacted
    assert "sk-abcdefghijklmnop" not in redacted
    assert redacted.endswith("finished")

    path = tmp_path / "events.jsonl"
    append_json_event(
        path,
        {
            "kind": "terminal_evidence",
            "stdout_bytes": 0,
            "stdout_sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "terminal": "",
        },
    )
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["stdout_bytes"] == 0


def test_completion_wins_pending_cancel_and_both_outcomes_are_explicit() -> None:
    assert completion_race(True, True) == "completed_cancel_voided"
    assert completion_race(False, True) == "cancelled"
    assert completion_race(True, False) == "completed"


def test_cancel_requires_empty_exact_cgroup_and_fresh_matching_claim(tmp_path: Path) -> None:
    command = parse_control(encoded(command="graceful-cancel"), IDENTITY, now=NOW)
    releases: list[tuple[str, str, str]] = []
    outcome = reconcile_graceful_cancel(
        command,
        worker_completed=False,
        cgroup_is_empty=True,
        current_claim=IDENTITY,
        evidence_path=tmp_path / "evidence.jsonl",
        terminal="password=hunter2 stopped",
        release_matching_claim=lambda *args: releases.append(args),
    )
    assert outcome == "cancelled"
    assert releases == [(IDENTITY.card_id, IDENTITY.owner, IDENTITY.claim_revision)]
    evidence = json.loads((tmp_path / "evidence.jsonl").read_text())
    assert evidence["kind"] == "worker_control_evidence"
    assert "hunter2" not in evidence["terminal"]

    for empty, claim in [(False, IDENTITY), (True, None)]:
        with pytest.raises(ControlError):
            reconcile_graceful_cancel(
                command,
                worker_completed=False,
                cgroup_is_empty=empty,
                current_claim=claim,
                evidence_path=tmp_path / f"rejected-{empty}.jsonl",
                terminal="",
                release_matching_claim=lambda *_: pytest.fail("must not release"),
            )


def test_completion_voids_cancel_without_release(tmp_path: Path) -> None:
    command = parse_control(encoded(command="graceful-cancel"), IDENTITY, now=NOW)
    assert reconcile_graceful_cancel(
        command,
        worker_completed=True,
        cgroup_is_empty=False,
        current_claim=None,
        evidence_path=tmp_path / "race.jsonl",
        terminal="done",
        release_matching_claim=lambda *_: pytest.fail("must not release completed claim"),
    ) == "completed_cancel_voided"
    event = json.loads((tmp_path / "race.jsonl").read_text())
    assert event["outcome"] == "completed_cancel_voided"
