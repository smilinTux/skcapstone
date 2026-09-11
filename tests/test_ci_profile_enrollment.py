"""Initial policy enrollment is not a claim that repository CI ran."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from skcoord.card_store import CardStore

from skcapstone import ci_applicability as ci
from skcapstone.coordination import Board
from skcapstone.mcp_server import call_tool
from tests.test_ci_applicability import (
    completion_fixture,
    enrollment_fixture,
    repository_fixture,
)
from tests.test_coord_completion_parity import _invoke, _main, _review_home

ENTRYPOINTS = ["cli_complete", "cli_move", "mcp_complete", "mcp_move"]
KEY = "ci_profile_enrollment"


def enrollment_home(tmp_path, monkeypatch):
    """Bind real Git enrollment and create an isolated review card."""
    _, _, meta, request = enrollment_fixture(tmp_path)
    monkeypatch.setattr(
        ci,
        "INITIAL_PROFILE_DIGESTS",
        {ci._repository(meta["repository"]): request["profile_sha256"]},
    )
    capsule = ci.bind_ci_profile(meta, request)
    home = _review_home(tmp_path, "[REVIEW] initial enrollment", "SUCCESS")
    _, full_receipt, append = completion_fixture(home, card_id="abcd1234")
    path = home / "cards/abcd1234/core.json"
    core = json.loads(path.read_text())
    core["meta"] = {**meta, "ci_profile": capsule}
    path.write_text(json.dumps(core))
    full_receipt["candidate_revision"] = request["candidate_revision"]
    receipt = {
        key: capsule[key]
        for key in ("schema_version", "repository", "candidate_revision", "profile_sha256")
    }
    receipt["evidence"] = "sha256:" + "d" * 64
    return home, core, receipt, full_receipt, append


def test_binder_marks_only_initial_enrollment(tmp_path, monkeypatch):
    (tmp_path / "initial").mkdir()
    (tmp_path / "established").mkdir()
    home, core, _, _, _ = enrollment_home(tmp_path / "initial", monkeypatch)
    assert core["meta"]["ci_profile"]["initial_enrollment"] is True
    _, _, meta, request = repository_fixture(tmp_path / "established")
    assert "initial_enrollment" not in ci.bind_ci_profile(meta, request)


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
@pytest.mark.parametrize(
    "case",
    [
        "valid_digest",
        "valid_url",
        "identical_duplicate",
        "escaped_key",
        "missing",
        "substitution",
        "extra_checks",
        "wrong_repository",
        "wrong_candidate",
        "wrong_digest",
        "wrong_schema",
        "mutable_evidence",
        "missing_evidence",
        "duplicate_receipt",
        "malformed_receipt",
        "conflict",
        "newer_wrong_pins",
        "outer_duplicate",
        "escaped_outer_duplicate",
        "malformed_row",
        "chain",
        "sync_conflict",
        "missing_verdict",
        "unrelated_corruption",
        "invalid_flag",
        "unregistered_flag",
    ],
)
async def test_enrollment_completion_exact_receipt(tmp_path, monkeypatch, entrypoint, case):
    home, core, receipt, full_receipt, append = enrollment_home(tmp_path, monkeypatch)
    if case == "invalid_flag":
        core["meta"]["ci_profile"]["initial_enrollment"] = 1
        (home / "cards/abcd1234/core.json").write_text(json.dumps(core))
    if case == "unregistered_flag":
        monkeypatch.setattr(ci, "INITIAL_PROFILE_DIGESTS", {})
    if case == "substitution":
        append(full_receipt)
    elif case != "missing":
        if case == "valid_url":
            receipt["evidence"] = "https://ci.example.test/runs/123/jobs/456"
        if case == "extra_checks":
            receipt["checks"] = full_receipt["checks"]
        if case.startswith("wrong_"):
            field, value = {
                "wrong_repository": ("repository", "https://other.test/repo"),
                "wrong_candidate": ("candidate_revision", "c" * 40),
                "wrong_digest": ("profile_sha256", "c" * 64),
                "wrong_schema": ("schema_version", True),
            }[case]
            receipt[field] = value
        if case == "mutable_evidence":
            receipt["evidence"] = "/tmp/latest"
        if case == "missing_evidence":
            del receipt["evidence"]
        append(receipt, link_key=KEY)
        if case in ("identical_duplicate", "conflict"):
            other = dict(receipt)
            if case == "conflict":
                other["evidence"] = "sha256:" + "e" * 64
            append(other, link_key=KEY, writer="second")
        if case == "newer_wrong_pins":
            receipt["candidate_revision"] = "f" * 40
            append(receipt, link_key=KEY, ts="2026-09-11T11:00:00Z")
        if case in ("duplicate_receipt", "malformed_receipt"):
            value = json.dumps(receipt)
            value = (
                value.replace(
                    '"schema_version": 1', '"schema_version": 1, "schema_\\u0076ersion": 1'
                )
                if case == "duplicate_receipt"
                else "{"
            )
            append(link_key=KEY, link_value=value, ts="2026-09-11T11:00:00Z")
        if case in (
            "escaped_key",
            "outer_duplicate",
            "escaped_outer_duplicate",
            "malformed_row",
            "chain",
        ):
            row = {
                "card_id": "abcd1234",
                "action": "link",
                "link_key": KEY,
                "link_value": json.dumps(receipt),
                "ts": "2026-09-11T11:00:00Z",
            }
            if case == "chain":
                row["prev_hash"] = "f" * 64
            raw = json.dumps(row)
            if "duplicate" in case:
                raw = raw.replace('"action": "link"', '"action": "link", "action": "label"')
            if "escaped" in case:
                raw = raw.replace(KEY, "ci_profile_\\u0065nrollment")
            if case == "malformed_row":
                raw = "{"
            (home / "cards/abcd1234/events/other.jsonl").write_text(raw + "\n")
        if case == "sync_conflict":
            (home / "cards/abcd1234/events/other.sync-conflict.jsonl").touch()
        if case == "unrelated_corruption":
            path = home / "cards/another/events/broken.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text("{")
        if case == "missing_verdict":
            path = home / "coordination/card_events/test.jsonl"
            path.write_text(path.read_text().replace('"PASS"', '"FAIL"'))
    before = {str(p): p.read_bytes() for p in home.rglob("*.jsonl")}
    ok, detail = await _invoke(home, entrypoint)
    expected = case in (
        "valid_digest",
        "valid_url",
        "identical_duplicate",
        "escaped_key",
        "unrelated_corruption",
    )
    assert ok == expected, detail
    if not expected:
        assert Board(home).load_agent("reviewer").current_task == "abcd1234"
        assert {str(p): p.read_bytes() for p in home.rglob("*.jsonl")} == before


@pytest.mark.parametrize("flag", [False, None, 1, 0, "true", {}, []])
def test_enrollment_flag_requires_literal_true(tmp_path, monkeypatch, flag):
    home, core, receipt, _, append = enrollment_home(tmp_path, monkeypatch)
    core["meta"]["ci_profile"]["initial_enrollment"] = flag
    append(receipt, link_key=KEY)
    with pytest.raises(ValueError):
        ci.validate_profile_completion("abcd1234", home, core)


def test_enrollment_rechecks_central_registration(tmp_path, monkeypatch):
    home, core, receipt, _, append = enrollment_home(tmp_path, monkeypatch)
    append(receipt, link_key=KEY)
    monkeypatch.setattr(ci, "INITIAL_PROFILE_DIGESTS", {})
    with pytest.raises(ValueError):
        ci.validate_profile_completion("abcd1234", home, core)


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ENTRYPOINTS)
@pytest.mark.parametrize("full_results", [False, True])
async def test_unflagged_capsule_keeps_full_policy(
    tmp_path, monkeypatch, entrypoint, full_results
):
    home, core, receipt, full_receipt, append = enrollment_home(tmp_path, monkeypatch)
    core["meta"]["ci_profile"].pop("initial_enrollment", None)
    (home / "cards/abcd1234/core.json").write_text(json.dumps(core))
    append(receipt, link_key=KEY)
    if full_results:
        append(full_receipt)
    before = {str(p): p.read_bytes() for p in home.rglob("*.jsonl")}
    ok, detail = await _invoke(home, entrypoint)
    assert ok == full_results, detail
    if not ok:
        assert Board(home).load_agent("reviewer").current_task == "abcd1234"
        assert {str(p): p.read_bytes() for p in home.rglob("*.jsonl")} == before


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["cli", "mcp"])
@pytest.mark.parametrize("invalid", [False, True])
async def test_enrollment_link_uses_exact_card_store(tmp_path, monkeypatch, adapter, invalid):
    home, _, receipt, _, _ = enrollment_home(tmp_path, monkeypatch)
    before = {str(p): p.read_bytes() for p in home.rglob("*.jsonl")}
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
                KEY,
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
                    "key": KEY,
                    "value": value,
                    "agent": "reviewer",
                },
            )
        assert ("error" not in json.loads(result[0].text)) == (not invalid), result
    events = CardStore(home)._read_events("abcd1234")
    matches = [e for e in events if e.get("link_key") == KEY]
    if invalid:
        assert not matches
        assert {str(p): p.read_bytes() for p in home.rglob("*.jsonl")} == before
    else:
        assert len(matches) == 1 and matches[0]["link_value"] == value
        assert matches[0]["writer"] == "reviewer"
        for path, data in before.items():
            if "/coordination/card_events/" in path:
                assert Path(path).read_bytes() == data
        ok, detail = await _invoke(home, "cli_complete")
        assert ok, detail
