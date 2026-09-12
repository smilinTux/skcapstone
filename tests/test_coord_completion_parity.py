"""CLI and MCP must share the governed review completion mutation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner
from skcoord.card_store import CardStore

from skcapstone.cli.coord import register_coord_commands
from skcapstone.coordination import Board, Task
from skcapstone.mcp_server import call_tool
from tests.test_ci_applicability import (
    completion_fixture,
    enrollment_fixture,
    git,
    repository_fixture,
)

_REQUIRED = (
    "ci_check_docs",
    "ci_check_gitleaks",
    "ci_check_lint",
    "ci_check_shim_imports",
    "ci_check_python311",
    "ci_check_python312",
)


def _main() -> click.Group:
    @click.group()
    def main():
        pass

    register_coord_commands(main)
    return main


def _review_home(tmp_path: Path, title: str, state: str | None) -> Path:
    home = tmp_path / "home"
    board = Board(home)
    board.ensure_dirs()
    board.create_task(Task(id="abcd1234", title="completion parity fixture"))
    board.claim_task("reviewer", "abcd1234")
    core = home / "cards" / "abcd1234" / "core.json"
    payload = json.loads(core.read_text())
    payload["title"] = title
    core.write_text(json.dumps(payload))
    rows = [
        {
            "card_id": "abcd1234",
            "action": "link",
            "link_key": "verdict",
            "link_value": "PASS",
            "ts": "2026-09-09T08:00:00Z",
        }
    ]
    for key in _REQUIRED:
        if state is not None:
            rows.append(
                {
                    "card_id": "abcd1234",
                    "action": "link",
                    "link_key": key,
                    "link_value": state,
                    "ts": "2026-09-09T08:01:00Z",
                }
            )
    events = home / "coordination" / "card_events"
    events.mkdir(parents=True, exist_ok=True)
    (events / "test.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    return home


def _cli_complete(home: Path) -> tuple[bool, str]:
    result = CliRunner().invoke(
        _main(),
        ["coord", "complete", "abcd1234", "--home", str(home), "--agent", "reviewer"],
    )
    return result.exit_code == 0, result.output


def _cli_move(home: Path) -> tuple[bool, str]:
    result = CliRunner().invoke(
        _main(),
        ["coord", "move", "abcd1234", "done", "--home", str(home), "--agent", "reviewer"],
    )
    return result.exit_code == 0, result.output


async def _mcp_complete(home: Path) -> tuple[bool, str]:
    with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(home)):
        result = await call_tool(
            "coord_complete", {"task_id": "abcd1234", "agent_name": "reviewer"}
        )
    payload = json.loads(result[0].text)
    return "error" not in payload, str(payload)


async def _mcp_move(home: Path) -> tuple[bool, str]:
    with patch("skcapstone.mcp_tools._helpers.SHARED_ROOT", str(home)):
        result = await call_tool(
            "coord_move",
            {"task_id": "abcd1234", "column": "done", "agent": "reviewer"},
        )
    payload = json.loads(result[0].text)
    return "error" not in payload, str(payload)


async def _invoke(home: Path, entrypoint: str) -> tuple[bool, str]:
    if entrypoint == "cli_complete":
        return _cli_complete(home)
    if entrypoint == "cli_move":
        return _cli_move(home)
    if entrypoint == "mcp_complete":
        return await _mcp_complete(home)
    return await _mcp_move(home)


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["[X][REVIEW] review", "[X][REREVIEW] rereview"])
@pytest.mark.parametrize("entrypoint", ["cli_complete", "cli_move", "mcp_complete", "mcp_move"])
async def test_cli_mcp_accept_only_complete_exact_success(
    tmp_path: Path, title: str, entrypoint: str
) -> None:
    home = _review_home(tmp_path, title, "SUCCESS")
    ok, detail = await _invoke(home, entrypoint)
    assert ok, detail


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["[X][REVIEW] review", "[X][REREVIEW] rereview"])
@pytest.mark.parametrize("entrypoint", ["cli_complete", "cli_move", "mcp_complete", "mcp_move"])
@pytest.mark.parametrize(
    "state",
    [
        "PASS",
        "PASSED",
        "SUCCESSFUL",
        "SUCCESS extra",
        "success",
        " SUCCESS ",
        None,
        "UNKNOWN",
        "PENDING",
    ],
)
async def test_cli_mcp_fail_closed_for_noncanonical_or_incomplete_ci(
    tmp_path: Path, title: str, entrypoint: str, state: str | None
) -> None:
    home = _review_home(tmp_path, title, state)
    ok, detail = await _invoke(home, entrypoint)
    assert not ok, detail
    assert Board(home).load_agent("reviewer").current_task == "abcd1234"


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["cli_complete", "cli_move", "mcp_complete", "mcp_move"])
@pytest.mark.parametrize(
    "case",
    [
        "node",
        "python",
        "mixed",
        "missing",
        "na_success",
        "stale",
        "null",
        "newer_failure",
        "weakened",
    ],
)
async def test_profile_completion_entrypoint_parity(tmp_path, entrypoint, case):
    """All completion adapters enforce policy before claim or lifecycle mutation."""
    home = _review_home(tmp_path, "[REVIEW] profile", "SUCCESS")
    core, receipt, append = completion_fixture(
        home, kind=case if case in {"python", "mixed"} else "node", card_id="abcd1234"
    )
    core_path = home / "cards/abcd1234/core.json"
    payload = json.loads(core_path.read_text())
    payload["meta"] = core["meta"]
    if case == "null":
        payload["meta"]["ci_profile"] = None
    if case == "newer_failure":
        append(ts="2026-09-11T09:00:00Z")
    if case == "na_success":
        receipt["checks"]["ci_check_python311"]["state"] = "SUCCESS"
    if case == "stale":
        receipt["candidate_revision"] = "c" * 40
    if case == "newer_failure":
        receipt["checks"]["ci_check_docs"]["state"] = "FAILURE"
    if case == "weakened":
        receipt["checks"] = {"ci_check_docs": receipt["checks"]["ci_check_docs"]}
    core_path.write_text(json.dumps(payload))
    if case != "missing":
        append()
    before = CardStore(home).fold("abcd1234").status
    ok, detail = await _invoke(home, entrypoint)
    assert ok == (case in {"node", "python", "mixed"}), detail
    if not ok:
        assert Board(home).load_agent("reviewer").current_task == "abcd1234"
        assert CardStore(home).fold("abcd1234").status == before


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", [False, True])
@pytest.mark.parametrize("invalid", [None, "digest", "null", "json", "missing_binding"])
async def test_cli_mcp_creation_capsule_parity(tmp_path, monkeypatch, initial, invalid):
    """Creation verifies the same capsule and rejects bad input before any card."""
    from skcapstone import ci_applicability

    fixture = enrollment_fixture if initial else repository_fixture
    _, _, meta, request = fixture(tmp_path)
    if initial:
        monkeypatch.setattr(
            ci_applicability,
            "INITIAL_PROFILE_DIGESTS",
            {meta["repository"].removesuffix(".git"): request["profile_sha256"]},
        )
    if invalid == "digest":
        request["profile_sha256"] = "f" * 64
    if invalid == "null":
        request = None
    if invalid == "json":
        request = "{"
    if invalid == "missing_binding":
        meta = {}
    cli_home, mcp_home = tmp_path / "cli", tmp_path / "mcp"
    arguments = [
        "coord",
        "create",
        "--title",
        "profile creation fixture",
        "--by",
        "tester",
        "--home",
        str(cli_home),
        "--ci-profile",
        json.dumps(request),
    ]
    for key, value in meta.items():
        arguments.extend(["--" + key.replace("_", "-"), value])
    result = CliRunner().invoke(_main(), arguments)
    with patch("skcapstone.mcp_tools._helpers.AGENT_HOME", str(mcp_home)):
        response = await call_tool(
            "coord_create",
            {
                "title": "profile creation fixture",
                "created_by": "tester",
                "ci_profile": request,
                **meta,
            },
        )
    payload = json.loads(response[0].text)
    assert (result.exit_code == 0) == (invalid is None), result.output
    assert ("error" not in payload) == (invalid is None), payload
    cli_cards = list(cli_home.glob("cards/*/core.json"))
    mcp_cards = list(mcp_home.glob("cards/*/core.json"))
    if invalid is not None:
        assert cli_cards == mcp_cards == []
    else:
        assert len(cli_cards) == len(mcp_cards) == 1
        cli_meta = json.loads(cli_cards[0].read_text())["meta"]
        mcp_meta = json.loads(mcp_cards[0].read_text())["meta"]
        assert cli_meta == mcp_meta
        assert "repository_path" not in cli_meta["ci_profile"]
        if initial:
            assert cli_meta["ci_profile"]["initial_enrollment"] is True
        else:
            assert "initial_enrollment" not in cli_meta["ci_profile"]


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["cli_complete", "cli_move", "mcp_complete", "mcp_move"])
@pytest.mark.parametrize("core_text", [None, "{", "[]", "{}", '{"title": null}'])
async def test_unreadable_core_cannot_hide_review_from_completion(tmp_path, entrypoint, core_text):
    """Unknown card identity must fail before any claim or lifecycle writes."""
    home = _review_home(tmp_path, "[REVIEW] malformed core", "SUCCESS")
    core = home / "cards/abcd1234/core.json"
    if core_text is None:
        core.unlink()
    else:
        core.write_text(core_text)
    before = {str(path): path.read_bytes() for path in home.rglob("*.jsonl")}
    ok, detail = await _invoke(home, entrypoint)
    assert not ok, detail
    assert "immutable core" in detail, detail
    assert Board(home).load_agent("reviewer").current_task == "abcd1234"
    assert {str(path): path.read_bytes() for path in home.rglob("*.jsonl")} == before


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["cli_complete", "cli_move", "mcp_complete", "mcp_move"])
async def test_nonreview_completion_remains_unchanged(tmp_path, entrypoint):
    """A valid non-review card still completes without any CI receipt."""
    home = _review_home(tmp_path, "ordinary implementation", None)
    ok, detail = await _invoke(home, entrypoint)
    assert ok, detail


@pytest.mark.parametrize("missing", [None, "candidate", "base", "blob", "tree"])
def test_promisor_profile_binding_never_attempts_transport(tmp_path, monkeypatch, missing):
    """Local policy remains readable while missing promisor objects fail offline."""
    import subprocess

    from skcapstone.ci_applicability import bind_ci_profile

    repo, _, meta, request = repository_fixture(tmp_path)
    git(repo, "config", "remote.origin.promisor", "true")
    git(repo, "config", "protocol.allow", "never")
    monkeypatch.setenv("GIT_NO_LAZY_FETCH", "0")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "https")
    if missing == "candidate":
        request["candidate_revision"] = "f" * 40
    if missing == "base":
        meta["base_revision"] = "f" * 40
    if missing in {"blob", "tree"}:
        suffix = ".skcapstone/ci-profile.json" if missing == "blob" else ".skcapstone"
        oid = git(repo, "rev-parse", "HEAD:" + suffix)
        (repo / ".git/objects" / oid[:2] / oid[2:]).unlink()
    observed = []
    actual_run = subprocess.run

    def observe(*args, **kwargs):
        """Record safe rejected subprocess stderr without permitting transport."""
        kwargs["env"] = dict(kwargs["env"], GIT_TRACE="1")
        try:
            return actual_run(*args, **kwargs)
        except subprocess.CalledProcessError as error:
            observed.append(error.stderr.decode())
            raise

    with patch("skcapstone.ci_applicability.subprocess.run", side_effect=observe) as run:
        if missing:
            with pytest.raises(ValueError):
                bind_ci_profile(meta, request)
        else:
            assert (
                bind_ci_profile(meta, request)["candidate_revision"]
                == request["candidate_revision"]
            )
    assert not any(
        "transport" in error or ("run_command:" in line and "fetch" in line)
        for error in observed
        for line in error.splitlines()
    ), observed
    for call in run.call_args_list:
        assert call.kwargs["env"]["GIT_NO_LAZY_FETCH"] == "1"
        assert call.kwargs["env"]["GIT_ALLOW_PROTOCOL"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["cli", "mcp"])
@pytest.mark.parametrize("invalid", [False, True])
async def test_profile_link_uses_exact_card_store(tmp_path, adapter, invalid):
    """Both public writers persist the authoritative receipt only on its card."""
    home = _review_home(tmp_path, "[REVIEW] receipt writer", "SUCCESS")
    core, receipt, _ = completion_fixture(home, card_id="abcd1234")
    path = home / "cards/abcd1234/core.json"
    payload = json.loads(path.read_text())
    payload["meta"] = core["meta"]
    path.write_text(json.dumps(payload))
    before = {str(p): p.read_bytes() for p in (home / "coordination/card_events").glob("*.jsonl")}
    value = json.dumps(receipt)
    if invalid:
        value = value.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
    if adapter == "cli":
        result = CliRunner().invoke(
            _main(),
            [
                "coord",
                "link",
                "abcd1234",
                "ci_applicability",
                value,
                "--home",
                str(home),
                "--agent",
                "reviewer",
            ],
        )
        assert (result.exit_code == 0) == (not invalid), result.output
    else:
        with patch("skcapstone.mcp_tools._helpers.SHARED_ROOT", str(home)):
            result = await call_tool(
                "coord_link",
                {
                    "task_id": "abcd1234",
                    "key": "ci_applicability",
                    "value": value,
                    "agent": "reviewer",
                },
            )
        assert ("error" not in json.loads(result[0].text)) == (not invalid), result
    events = CardStore(home)._read_events("abcd1234")
    matches = [e for e in events if e.get("link_key") == "ci_applicability"]
    if invalid:
        assert matches == []
        assert {
            str(p): p.read_bytes() for p in (home / "coordination/card_events").glob("*.jsonl")
        } == before
        return
    assert len(matches) == 1 and matches[0]["link_value"] == value
    assert matches[0]["writer"] == "reviewer"
    assert {
        str(p): p.read_bytes() for p in (home / "coordination/card_events").glob("*.jsonl")
    } == before
    ok, detail = await _invoke(home, "cli_complete")
    assert ok, detail


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["cli_complete", "cli_move", "mcp_complete", "mcp_move"])
@pytest.mark.parametrize("encoding", ["escaped_card", "escaped_key", "malformed", "chain"])
async def test_malformed_exact_card_receipt_preserves_all_entrypoints(
    tmp_path, entrypoint, encoding
):
    """Escapes or corrupt chains must not revive an older green receipt."""
    home = _review_home(tmp_path, "[REVIEW] escaped evidence", "SUCCESS")
    core, receipt, append = completion_fixture(home, card_id="abcd1234")
    path = home / "cards/abcd1234/core.json"
    payload = json.loads(path.read_text())
    payload["meta"] = core["meta"]
    path.write_text(json.dumps(payload))
    append()
    (home / "coordination/card_events/old-receipt.jsonl").write_text(
        json.dumps(
            {
                "card_id": "abcd1234",
                "action": "link",
                "link_key": "ci_applicability",
                "link_value": json.dumps(receipt),
                "ts": "2026-09-11T10:00:00Z",
            }
        )
        + "\n"
    )
    receipt["checks"]["ci_check_docs"]["state"] = "FAILURE"
    row = {
        "card_id": "abcd1234",
        "action": "link",
        "link_key": "ci_applicability",
        "link_value": json.dumps(receipt),
        "ts": "2026-09-11T11:00:00Z",
    }
    raw = json.dumps(row)
    if encoding.startswith("escaped"):
        raw = raw.replace('"action": "link"', '"action": "link", "action": "label"')
        raw = (
            raw.replace('"abcd1234"', '"\\u0061bcd1234"')
            if encoding == "escaped_card"
            else raw.replace('"ci_applicability"', '"ci_\\u0061pplicability"')
        )
    elif encoding == "chain":
        row["prev_hash"] = "f" * 64
        raw = json.dumps(row)
    else:
        raw = "{"
    (home / "cards/abcd1234/events/new-writer.jsonl").write_text(raw + "\n")
    before = {str(p): p.read_bytes() for p in home.rglob("*.jsonl")}
    ok, detail = await _invoke(home, entrypoint)
    assert not ok, detail
    assert Board(home).load_agent("reviewer").current_task == "abcd1234"
    assert {str(p): p.read_bytes() for p in home.rglob("*.jsonl")} == before
