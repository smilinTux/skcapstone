"""CLI tests for ``skcapstone sync audit``.

Three properties matter beyond the output shape. The default invocation
must write NOTHING, because it is meant to run on the node it is judging.
The exit code must fail closed: nonzero whenever any folder can synchronize
private material, zero only on a clean tree. And --json must parse as one
document even with CliRunner merging streams.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from skcapstone.cli import main
from skcapstone.sync_policy import MATERIAL_CLASSES

COVERING_RULES = "\n".join(line for material in MATERIAL_CLASSES for _, line in material.probes)


def _home(tmp_path, rules: str | None = COVERING_RULES) -> tuple:
    """A synthetic home with one configured folder, plus the config path."""
    home = tmp_path / "home"
    folder = home / "sync"
    folder.mkdir(parents=True)
    if rules is not None:
        (folder / ".stignore").write_text(rules, encoding="utf-8")
    config = home / ".config" / "syncthing" / "config.xml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '<configuration><folder id="skcapstone-sync" path="%s" type="sendreceive"/>'
        "</configuration>" % folder,
        encoding="utf-8",
    )
    return home, folder


def test_clean_tree_exits_zero(tmp_path) -> None:
    home, _ = _home(tmp_path)
    result = CliRunner().invoke(main, ["sync", "audit", "--home", str(home)])
    assert result.exit_code == 0
    assert "CLEAN" in result.output


def test_violation_exits_nonzero(tmp_path) -> None:
    home, folder = _home(tmp_path, rules="*.key\n")
    (folder / "identity").mkdir()
    (folder / "identity" / "private.asc").write_text("synthetic", encoding="utf-8")
    result = CliRunner().invoke(main, ["sync", "audit", "--home", str(home)])
    assert result.exit_code == 1
    assert "VIOLATIONS FOUND" in result.output
    assert "private_material_uncovered" in result.output
    assert "identity/private.asc" in result.output


def test_missing_config_exits_nonzero(tmp_path) -> None:
    home = tmp_path / "bare"
    home.mkdir()
    result = CliRunner().invoke(main, ["sync", "audit", "--home", str(home)])
    assert result.exit_code == 1
    assert "config_not_found" in result.output


def test_json_output_parses(tmp_path) -> None:
    home, _ = _home(tmp_path)
    result = CliRunner().invoke(main, ["sync", "audit", "--home", str(home), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["ok"] is True
    assert payload["folders"][0]["folder_id"] == "skcapstone-sync"
    assert payload["dry_run"] is True


def test_json_output_parses_on_violation(tmp_path) -> None:
    home, _ = _home(tmp_path, rules="*.key\n")
    result = CliRunner().invoke(main, ["sync", "audit", "--home", str(home), "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output[result.output.index("{") :])
    assert payload["ok"] is False
    categories = {f["category"] for f in payload["folders"][0]["findings"]}
    assert "private_pattern_uncovered" in categories


def test_dry_run_prints_exact_lines_and_writes_nothing(tmp_path) -> None:
    home, folder = _home(tmp_path, rules="*.key\n")
    stignore = folder / ".stignore"
    before = stignore.read_bytes()
    result = CliRunner().invoke(main, ["sync", "audit", "--home", str(home)])
    assert result.exit_code == 1
    assert "DRY-RUN" in result.output
    assert "+ *.pem" in result.output
    assert "+ **/private.*" in result.output
    assert "+ capauth/security/tokens" in result.output
    # The already-present rule is not re-proposed.
    assert "+ *.key\n" not in result.output
    assert stignore.read_bytes() == before
    assert not (folder / ".stignore.bak-sync-policy").exists()


def test_apply_merges_and_second_run_is_clean(tmp_path) -> None:
    home, folder = _home(tmp_path, rules="*.key\n")
    runner = CliRunner()
    first = runner.invoke(main, ["sync", "audit", "--home", str(home), "--apply"])
    assert "Applied remediation" in first.output
    text = (folder / ".stignore").read_text(encoding="utf-8")
    assert "*.pem" in text and "**/private.*" in text
    second = runner.invoke(main, ["sync", "audit", "--home", str(home)])
    assert second.exit_code == 0
    assert "CLEAN" in second.output


def test_explicit_config_path_is_used(tmp_path) -> None:
    home, _ = _home(tmp_path)
    config = home / ".config" / "syncthing" / "config.xml"
    result = CliRunner().invoke(
        main, ["sync", "audit", "--home", str(home), "--config", str(config), "--json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.output[result.output.index("{") :])
    assert len(payload["folders"]) == 1
