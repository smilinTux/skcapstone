"""skfleet node stignore (card 20a1d4d3).

Beyond the output shape, three properties matter. The command must write
NOTHING, because it is meant to run on the node it is judging. It must not
need a role, because the invariant is folder-keyed and a role-less node
holding the folder is exposed exactly as much as a control node. And it must
be seen FAILING, or nobody knows it works.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from skcapstone.fleet import stignore_doctor as sd
from skcapstone.fleet import store
from skcapstone.fleet.cli import fleet

GOOD = "*.key\n*.pem\n**/private.*\n"
STRIPPED = "// rules rewritten by hand\n__pycache__\nsessions\n"


def _env(paths) -> dict:
    return {"SKFLEET_ROOT": str(paths.root), "SKFLEET_NODE": "node-under-test"}


def _json_payload(output: str) -> list:
    """Parse the JSON array out of mixed stdout+stderr (CliRunner merges them)."""
    return json.loads(output[output.index("[") :])


def _snapshot(root):
    return {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def folder(tmp_path, monkeypatch):
    """A throwaway sovereign folder root, wired in as skcapstone-sync."""
    root = tmp_path / "agent-home"
    root.mkdir()
    monkeypatch.setitem(
        sd.DEFAULT_RULESETS,
        "skcapstone-sync",
        sd.SyncFolderRuleset(
            folder_id="skcapstone-sync",
            root=str(root),
            required=("*.key", "*.pem", "**/private.*"),
            recommended=("**/telegram.session",),
        ),
    )
    return root


def test_intact_ruleset_reports_clean(folder, paths) -> None:
    (folder / ".stignore").write_text(GOOD + "**/telegram.session\n", encoding="utf-8")

    result = CliRunner().invoke(fleet, ["node", "stignore"], env=_env(paths))

    assert result.exit_code == 0
    assert "OK" in result.output
    assert "(clean)" in result.output


def test_stripped_ruleset_is_reported(folder, paths) -> None:
    """The negative control at the CLI seam."""
    (folder / ".stignore").write_text(STRIPPED, encoding="utf-8")

    result = CliRunner().invoke(fleet, ["node", "stignore"], env=_env(paths))

    assert "ERROR" in result.output
    assert "missing_required_ignore" in result.output
    assert "*.key" in result.output


def test_no_stignore_at_all_is_reported(folder, paths) -> None:
    result = CliRunner().invoke(fleet, ["node", "stignore"], env=_env(paths))
    assert "no_stignore" in result.output


def test_strict_exits_nonzero_only_on_an_error_finding(folder, paths) -> None:
    (folder / ".stignore").write_text(STRIPPED, encoding="utf-8")
    bad = CliRunner().invoke(fleet, ["node", "stignore", "--strict"], env=_env(paths))
    assert bad.exit_code == 1

    (folder / ".stignore").write_text(GOOD, encoding="utf-8")
    ok = CliRunner().invoke(fleet, ["node", "stignore", "--strict"], env=_env(paths))
    assert ok.exit_code == 0, ok.output


def test_a_warn_finding_does_not_fail_strict(folder, paths) -> None:
    """Only `error` gates. A warn that failed a gate is a warn nobody reads."""
    (folder / ".stignore").write_text(GOOD, encoding="utf-8")
    result = CliRunner().invoke(fleet, ["node", "stignore", "--strict"], env=_env(paths))
    assert result.exit_code == 0
    assert "WARN" in result.output


def test_json_output_is_machine_readable(folder, paths) -> None:
    (folder / ".stignore").write_text(STRIPPED, encoding="utf-8")

    result = CliRunner().invoke(fleet, ["node", "stignore", "--json"], env=_env(paths))

    payload = _json_payload(result.output)
    assert payload[0]["folder"] == "skcapstone-sync"
    assert payload[0]["severity"] == "error"
    assert "*.key" in payload[0]["missing_required"]


def test_a_folder_this_node_does_not_hold_is_skipped(tmp_path, paths, monkeypatch) -> None:
    monkeypatch.setitem(
        sd.DEFAULT_RULESETS,
        "skcapstone-sync",
        sd.SyncFolderRuleset(folder_id="skcapstone-sync", root=str(tmp_path / "absent")),
    )
    result = CliRunner().invoke(fleet, ["node", "stignore"], env=_env(paths))
    assert result.exit_code == 0
    assert "no sovereign sync folders on this node" in result.output


def test_no_role_is_needed(folder, paths) -> None:
    """node doctor skips a role-less node. This check must not, because a
    role-less node holding the folder leaks exactly the same keys."""
    (folder / ".stignore").write_text(STRIPPED, encoding="utf-8")
    assert store.read_spec(paths, "node", "node-under-test") is None

    result = CliRunner().invoke(fleet, ["node", "stignore"], env=_env(paths))

    assert "ERROR" in result.output


def test_the_check_writes_nothing(folder, paths, operator) -> None:
    store.write_spec(paths, "profile", "control", {"description": "x"}, writer=operator)
    (folder / ".stignore").write_text(STRIPPED, encoding="utf-8")
    before = _snapshot(paths.root) | _snapshot(folder)

    CliRunner().invoke(fleet, ["node", "stignore"], env=_env(paths))

    assert (_snapshot(paths.root) | _snapshot(folder)) == before


def test_a_syncfolder_object_extends_the_built_in_rules(folder, paths, operator) -> None:
    store.write_spec(
        paths,
        "syncfolder",
        "skcapstone-sync",
        {"root": str(folder), "requiredIgnores": ["**/*.gpg"]},
        writer=operator,
    )
    (folder / ".stignore").write_text(GOOD + "**/telegram.session\n", encoding="utf-8")

    result = CliRunner().invoke(fleet, ["node", "stignore", "--json"], env=_env(paths))

    payload = _json_payload(result.output)
    assert payload[0]["missing_required"] == ["**/*.gpg"]
