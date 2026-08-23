"""Tests for SK suite registration path discovery."""

from pathlib import Path
from unittest.mock import patch

from skcapstone.register import find_skill_md, register_all


def test_find_skill_md_supports_skcapstone_repos_layout(tmp_path: Path) -> None:
    skill = tmp_path / "skcapstone-repos" / "skmemory" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# SKMemory\n")

    assert find_skill_md("skmemory", tmp_path) == skill


def test_find_skcapstone_skill_in_suite_layout(tmp_path: Path) -> None:
    skill = tmp_path / "skcapstone-repos" / "skcapstone" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# SKCapstone\n")

    assert find_skill_md("skcapstone", tmp_path) == skill


def test_pi_defaults_only_register_capstone_and_memory_mcps(tmp_path: Path) -> None:
    packages = [
        {"name": "skcapstone", "mcp_cmd": "skcapstone-mcp"},
        {"name": "skmemory", "mcp_cmd": "skmemory-mcp"},
        {"name": "skchat", "mcp_cmd": "skchat-mcp"},
    ]
    for package in packages:
        skill = tmp_path / "skcapstone-repos" / package["name"] / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(f"# {package['name']}\n")

    with (
        patch("skcapstone.register._build_package_registry", return_value=packages),
        patch("skcapstone.register._discover_plugin_servers", return_value=[]),
        patch("skcapstone.codex_setup.ensure_pi_setup", return_value=[]),
        patch("skcapstone.register.register_package") as register,
    ):
        register_all(workspace=tmp_path, environments=["pi"], dry_run=False)

    environments_by_name = {
        call.kwargs["name"]: call.kwargs["environments"] for call in register.call_args_list
    }
    assert environments_by_name == {
        "skcapstone": ["pi"],
        "skmemory": ["pi"],
        "skchat": [],
    }
