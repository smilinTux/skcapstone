"""Supported coord routing for hash-pinned overlay recovery."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import click
from click.testing import CliRunner

from skcapstone.card import CardEvent
from skcapstone.card_store import CardCore, CardStore
from skcapstone.cli.coord import register_coord_commands
from skcapstone.doctor import _check_store_integrity


def _main() -> click.Group:
    @click.group()
    def main() -> None:
        pass

    register_coord_commands(main)
    return main


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _fixture(tmp_path: Path) -> dict[str, object]:
    home = tmp_path / "home"
    evidence = tmp_path / "evidence"
    home.mkdir()
    evidence.mkdir()
    store = CardStore(home)
    store.create(
        CardCore(
            id="f0d0ba98",
            title="Recovery",
            initial_owner="operator",
            initial_claim_revision="revision",
        )
    )
    valid = (
        CardEvent(
            card_id="086ea05c",
            action="link",
            writer="reviewer",
            link_key="verdict",
            link_value="BLOCKED",
        )
        .model_dump_json()
        .encode()
        + b"\n"
    )
    rejected = (
        json.dumps(
            {"card": "086ea05c", "agent": "legacy", "event": "verdict", "value": "PASS"}
        ).encode()
        + b"\n"
    )
    shard = home / "coordination" / "card_events" / "chiap08.jsonl"
    shard.parent.mkdir(parents=True)
    shard.write_bytes(valid + rejected)
    return {
        "home": home,
        "evidence": evidence,
        "shard": shard,
        "source_sha": _sha(valid + rejected),
        "line_sha": _sha(rejected),
        "original": valid + rejected,
        "repaired": valid,
    }


def _plan_args(fixture: dict[str, object]) -> list[str]:
    return [
        "coord",
        "recover-overlay",
        "plan",
        "--home",
        str(fixture["home"]),
        "--agent",
        "operator",
        "--writer",
        "chiap08.jsonl",
        "--line",
        "2",
        "--source-sha256",
        str(fixture["source_sha"]),
        "--line-sha256",
        str(fixture["line_sha"]),
        "--recovery-card",
        "f0d0ba98",
        "--evidence",
        str(fixture["evidence"]),
    ]


def test_plan_writes_only_the_hash_bound_plan(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    before = Path(fixture["shard"]).read_bytes()
    result = CliRunner().invoke(_main(), _plan_args(fixture))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    plan_path = Path(payload["plan"])
    assert plan_path.is_file()
    assert Path(fixture["shard"]).read_bytes() == before
    assert list(Path(fixture["evidence"]).glob("*.original.jsonl")) == []


def test_repeated_line_options_apply_one_atomic_multi_row_plan(
    tmp_path: Path, monkeypatch
) -> None:
    fixture = _fixture(tmp_path)
    second_rejected = (
        json.dumps(
            {
                "card_id": "second01",
                "action": "verdict",
                "writer": "legacy",
                "verdict": "BLOCKED",
            }
        ).encode()
        + b"\n"
    )
    original = fixture["original"]
    shard = fixture["shard"]
    assert isinstance(original, bytes)
    assert isinstance(shard, Path)
    third_rejected = b'{"card_id":"third003","action":"verdict","writer":"legacy"}\n'
    multi_original = original + second_rejected + third_rejected
    shard.write_bytes(multi_original)
    fixture["source_sha"] = _sha(multi_original)

    args = _plan_args(fixture) + [
        "--line",
        "3",
        "--line-sha256",
        _sha(second_rejected),
        "--line",
        "4",
        "--line-sha256",
        _sha(third_rejected),
    ]
    planned = CliRunner().invoke(_main(), args)
    assert planned.exit_code == 0, planned.output
    plan_path = Path(json.loads(planned.output)["plan"])
    plan = json.loads(plan_path.read_text())
    assert [target["line_number"] for target in plan["targets"]] == [2, 3, 4]

    monkeypatch.setattr(
        "skcapstone.jarvis_emergency.authorize_coord_mutation", lambda *args, **kwargs: None
    )
    applied = CliRunner().invoke(
        _main(),
        [
            "coord",
            "recover-overlay",
            "apply",
            "--home",
            str(fixture["home"]),
            "--agent",
            "operator",
            "--plan",
            str(plan_path),
            "--writer-quiesced",
        ],
    )
    assert applied.exit_code == 0, applied.output
    receipt = json.loads(applied.output)
    assert len(receipt["rejected_artifacts"]) == 3
    assert shard.read_bytes() == fixture["repaired"]


def test_plan_rejects_unpaired_repeated_line_options(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    result = CliRunner().invoke(_main(), _plan_args(fixture) + ["--line", "3"])
    assert result.exit_code != 0
    assert "matching counts" in result.output


def test_apply_requires_quiescence_and_then_routes_to_skcoord(tmp_path: Path, monkeypatch) -> None:
    fixture = _fixture(tmp_path)
    planned = CliRunner().invoke(_main(), _plan_args(fixture))
    plan_path = json.loads(planned.output)["plan"]
    monkeypatch.setattr(
        "skcapstone.jarvis_emergency.authorize_coord_mutation", lambda *args, **kwargs: None
    )
    command = [
        "coord",
        "recover-overlay",
        "apply",
        "--home",
        str(fixture["home"]),
        "--agent",
        "operator",
        "--plan",
        plan_path,
    ]
    refused = CliRunner().invoke(_main(), command)
    assert refused.exit_code != 0
    assert "writer-quiesced" in refused.output
    applied = CliRunner().invoke(_main(), command + ["--writer-quiesced"])
    assert applied.exit_code == 0, applied.output
    receipt = json.loads(applied.output)
    assert receipt["disposition"] == "applied_and_schema_verified"
    assert Path(fixture["shard"]).read_bytes() == fixture["repaired"]


def test_doctor_reports_schema_rejection_with_line_and_card_hint(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    cards = next(
        check for check in _check_store_integrity(fixture["home"]) if check.name == "store:cards"
    )
    assert not cards.passed
    assert "1 rejected" in cards.detail
    assert "chiap08.jsonl line 2" in cards.detail
    assert "card_hint=086ea05c" in cards.detail


def test_rollback_requires_quiescence_and_routes_to_skcoord(tmp_path: Path, monkeypatch) -> None:
    fixture = _fixture(tmp_path)
    planned = CliRunner().invoke(_main(), _plan_args(fixture))
    plan_path = json.loads(planned.output)["plan"]
    monkeypatch.setattr(
        "skcapstone.jarvis_emergency.authorize_coord_mutation", lambda *args, **kwargs: None
    )
    applied = CliRunner().invoke(
        _main(),
        [
            "coord",
            "recover-overlay",
            "apply",
            "--home",
            str(fixture["home"]),
            "--agent",
            "operator",
            "--plan",
            plan_path,
            "--writer-quiesced",
        ],
    )
    receipt = json.loads(applied.output)
    command = [
        "coord",
        "recover-overlay",
        "rollback",
        "--home",
        str(fixture["home"]),
        "--agent",
        "operator",
        "--receipt",
        str(Path(fixture["evidence"]) / receipt["receipt_artifact"]),
    ]
    refused = CliRunner().invoke(_main(), command)
    assert refused.exit_code != 0
    assert "writer-quiesced" in refused.output
    rolled_back = CliRunner().invoke(_main(), command + ["--writer-quiesced"])
    assert rolled_back.exit_code == 0, rolled_back.output
    assert Path(fixture["shard"]).read_bytes() == fixture["original"]
