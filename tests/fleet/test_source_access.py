"""Forced SSH retains old read boundaries and admits no arbitrary command."""

import json
from types import SimpleNamespace

import pytest

from skcapstone.fleet import source_access as access


def command(owner="worker", card="24b00001"):
    return f"env SKAGENT={owner} SKCAPSTONE_AGENT={owner} {access.CLI} coord show {card} --json"


@pytest.mark.parametrize(
    "bad",
    [
        "true",
        "bash",
        "cat ~/.ssh/id_ed25519",
        access.ARTIFACT_COMMAND + " --help",
        access.ARTIFACT_COMMAND + "; true",
        "python -c 'print(1)'",
        command() + " extra",
        command().replace("SKCAPSTONE_AGENT=worker", "SKCAPSTONE_AGENT=other"),
    ],
)
def test_no_arbitrary_command_passthrough(monkeypatch, bad):
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", bad)
    monkeypatch.setattr(access, "serve", lambda: pytest.fail("must not enter artifact handler"))
    monkeypatch.setattr(
        access.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not execute")
    )
    assert access.main() == 77
    assert access.main(source_claim=True) == 126


def test_only_exact_artifact_command_enters_bounded_handler(monkeypatch):
    calls = []
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", access.ARTIFACT_COMMAND)
    monkeypatch.setattr(access, "serve", lambda: calls.append("bounded-handler"))
    monkeypatch.setattr(access.subprocess, "run", lambda *args, **kwargs: pytest.fail("no shell"))
    assert access.main(source_claim=True) == 0
    assert calls == ["bounded-handler"]


@pytest.mark.parametrize("changed", [None, "owner", "status", "archived", "labels", "claim"])
def test_strict_source_claim_read_semantics_retained(monkeypatch, capsys, changed):
    row = {
        "id": "24b00001",
        "owner": "worker",
        "status": "doing",
        "archived": False,
        "labels": ["source-only"],
        "meta": {"_claim_revision": "a" * 32},
    }
    if changed == "owner":
        row["owner"] = "other"
    elif changed == "status":
        row["status"] = "done"
    elif changed == "archived":
        row["archived"] = True
    elif changed == "labels":
        row["labels"] = []
    elif changed == "claim":
        row["meta"].clear()
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", command())

    def run(argv, **kwargs):
        assert argv == [access.CLI, "coord", "show", "24b00001", "--json"]
        assert kwargs["env"]["SKAGENT"] == "worker"
        assert "shell" not in kwargs
        return SimpleNamespace(returncode=0, stdout=json.dumps(row))

    monkeypatch.setattr(access.subprocess, "run", run)
    assert access.main(source_claim=True) == (0 if changed is None else 126)
    output = capsys.readouterr()
    if changed is not None:
        assert not output.out
    # The other existing keys allow the same exact card read without
    # silently gaining any command other than this new artifact protocol.
    assert access.main() == 0
