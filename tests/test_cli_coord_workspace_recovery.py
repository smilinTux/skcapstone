from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import click
from click.testing import CliRunner

from skcapstone.cli import coord_workspace_recovery as recovery


def _main() -> click.Group:
    @click.group()
    def coord():
        pass

    recovery.register_coord_workspace_recovery(coord)
    return coord


def _args(tmp_path: Path) -> list[str]:
    return [
        "workspace-quarantine-reissue",
        "deadbeef",
        "--agent",
        "jarvis",
        "--claim-revision",
        "a" * 32,
        "--workspace",
        str(tmp_path / "workspace"),
        "--repository",
        str(tmp_path / "repo"),
        "--branch",
        "card/deadbeef",
        "--head",
        "b" * 40,
        "--base-revision",
        "c" * 40,
        "--quarantine-root",
        str(tmp_path / "quarantine"),
        "--reissue-path",
        str(tmp_path / "reissue"),
        "--home",
        str(tmp_path / "home"),
    ]


def test_workspace_recovery_refuses_claim_owner_drift(tmp_path: Path, monkeypatch) -> None:
    card = SimpleNamespace(owner="other-agent", meta={"_claim_revision": "a" * 32})
    card.status = SimpleNamespace(value="doing")
    card.model_dump = lambda mode: {"id": "deadbeef", "owner": "other-agent"}
    monkeypatch.setattr(recovery, "CardStore", lambda _: SimpleNamespace(fold=lambda _: card))

    result = CliRunner().invoke(_main(), _args(tmp_path))

    assert result.exit_code != 0
    assert "card claim changed" in result.output


def test_workspace_recovery_binds_exact_claim_and_reports_plan(
    tmp_path: Path, monkeypatch
) -> None:
    card = SimpleNamespace(owner="jarvis", meta={"_claim_revision": "a" * 32})
    card.status = SimpleNamespace(value="doing")
    card.model_dump = lambda mode: {"id": "deadbeef", "owner": "jarvis"}
    monkeypatch.setattr(recovery, "CardStore", lambda _: SimpleNamespace(fold=lambda _: card))

    def fake_run(command, **kwargs):
        stdout = " M tracked\0?? fresh\0" if "status" in command else "b" * 40 + "\n"
        return SimpleNamespace(stdout=stdout)

    captured = {}

    def fake_quarantine(proof, **kwargs):
        captured["proof"] = proof
        captured["kwargs"] = kwargs
        return {"state": "READY", "executed": False}

    monkeypatch.setattr(recovery.subprocess, "run", fake_run)
    monkeypatch.setattr(recovery, "quarantine_and_reissue", fake_quarantine)

    result = CliRunner().invoke(_main(), _args(tmp_path))

    assert result.exit_code == 0, result.output
    assert '"state": "READY"' in result.output
    assert captured["proof"].card_id == "deadbeef"
    assert captured["proof"].claim_revision == "a" * 32
    assert captured["proof"].dirty_paths == 2
    assert captured["proof"].untracked_paths == 1
    assert captured["kwargs"]["execute"] is False


def test_workspace_recovery_allows_exact_unowned_card_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    import hashlib
    import json

    payload = {"id": "deadbeef", "owner": None, "status": "backlog"}
    card = SimpleNamespace(owner=None, meta={})
    card.status = SimpleNamespace(value="backlog")
    card.model_dump = lambda mode: payload
    monkeypatch.setattr(recovery, "CardStore", lambda _: SimpleNamespace(fold=lambda _: card))
    monkeypatch.setattr(
        recovery.subprocess,
        "run",
        lambda command, **kwargs: SimpleNamespace(
            stdout="" if "status" in command else "b" * 40 + "\n"
        ),
    )
    monkeypatch.setattr(
        recovery,
        "quarantine_and_reissue",
        lambda proof, **kwargs: {"state": "READY", "executed": False},
    )
    card_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    args = _args(tmp_path) + ["--expected-card-sha256", card_hash]

    result = CliRunner().invoke(_main(), args)

    assert result.exit_code == 0, result.output
    assert '"state": "READY"' in result.output
