"""skmail prints real help and rejects missing arguments without crashing."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

# Redirected to the installed console script by tests/skmail_installation.py.
SCRIPT = Path(__file__).parents[1] / "scripts" / "fleet" / "skmail"


def _run(coord: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["SKMAIL_DIR"] = str(coord)
    return subprocess.run(
        [str(SCRIPT), *args], capture_output=True, text=True, env=env, check=False
    )


@pytest.mark.parametrize(
    "args",
    [("--help",), ("-h",), ("help",), ("send", "--help"), ("read", "-h"), ("tail", "--help")],
)
def test_help_prints_examples_and_exits_zero(tmp_path: Path, args: tuple[str, ...]) -> None:
    coord = tmp_path / "coord"
    result = _run(coord, *args)
    assert result.returncode == 0, result.stderr
    assert "USAGE" in result.stdout and "EXAMPLES" in result.stdout
    assert "unbound variable" not in result.stderr
    assert not coord.exists(), "asking for help must not create the mailbox directory"


@pytest.mark.parametrize(
    "args", [("send",), ("send", "a"), ("send", "a", "b", "normal"), ("read",), ("ack",)]
)
def test_missing_arguments_exit_2_with_usage_not_unbound_variable(
    tmp_path: Path, args: tuple[str, ...]
) -> None:
    result = _run(tmp_path, *args)
    assert result.returncode == 2
    assert "unbound variable" not in result.stderr
    assert "USAGE" in result.stderr


def test_unknown_command_exits_2_with_usage(tmp_path: Path) -> None:
    result = _run(tmp_path, "bogus")
    assert result.returncode == 2
    assert "unknown command 'bogus'" in result.stderr


def test_bad_priority_still_rejected(tmp_path: Path) -> None:
    result = _run(tmp_path, "send", "alice", "bob", "loud", "re", "body")
    assert result.returncode == 2
    assert "priority must be" in result.stderr
    assert not list((tmp_path / "skmail.d").glob("*.jsonl"))


@pytest.mark.parametrize("text", ["--help", "-h", "--boxdir /etc", "-x foo"])
def test_dash_prefixed_subject_and_body_are_delivered_not_parsed_as_options(
    tmp_path: Path, text: str
) -> None:
    """Only the first argument after the command can ask for help, never the message.

    Before this change a body or subject starting with "-" was parsed by the writer as
    an option, nothing was written, and skmail still printed "sent to ...".
    """
    result = _run(tmp_path, "send", "alice", "bob", "normal", text, text)
    assert result.returncode == 0, result.stderr
    box = next((tmp_path / "skmail.d").glob("alice@*.jsonl"))
    record = json.loads(box.read_text().splitlines()[-1])
    assert record["body"] == text
    assert record["re"] == text


def test_normal_send_and_tail_unchanged(tmp_path: Path) -> None:
    assert _run(tmp_path, "send", "alice", "bob", "fyi", "hello", "world").returncode == 0
    tail = _run(tmp_path, "tail", "5")
    assert tail.returncode == 0
    assert "alice -> bob" in tail.stdout and "world" in tail.stdout
