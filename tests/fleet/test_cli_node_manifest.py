"""``skcapstone fleet node manifest`` (task-3-fix report, Fix 3).

``deployment_manifest.write_manifest`` had no non-test caller: the manifest
pipeline built things in memory but nothing ever published one to disk
(``docs/fleet/rollout-drift.md`` said so outright). This command is that
caller -- it builds a fresh manifest (the same ``build_manifest`` ``node
drift`` already uses) and publishes it to the FLEET tree
(``fleet/paths.py``'s ``default_paths()``, i.e. ``$SKFLEET_ROOT`` or
``~/.skcapstone/fleet``) at ``status/node-<node>/manifest/manifest.json``,
matching every other status write's ``status/<node>/<kind>/<name>.json``
shape. Published through ``default_paths()`` rather than a path built from
``--home``: this is fleet STATE, and ``paths.py`` is the one module allowed
to name where the fleet tree lives
(``tests/fleet/test_root_relocation.py``), so relocating ``SKFLEET_ROOT``
must relocate this too.

Unlike ``node drift``/``node doctor``, this command WRITES on purpose: that
is its entire job, so it carries no ``--strict``/report-only contract to
violate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from skcapstone.fleet import deployment_manifest  # noqa: E402
from skcapstone.fleet.cli import fleet  # noqa: E402


def _env(fleet_root: Path) -> dict:
    return {"SKFLEET_NODE": "node-under-test", "SKFLEET_ROOT": str(fleet_root)}


@pytest.fixture
def fake_manifest(monkeypatch):
    """Stub build_manifest, and record how the CLI called it."""
    calls: dict = {}

    def fake_build_manifest(repo_root, home):
        calls["build_manifest"] = (Path(repo_root), Path(home))
        return {
            "revision": "rev-under-test",
            "git_sha": "abc12345",
            "package_version": "0.0.0-test",
            "required_env": ["SKFLEET_TARGET"],
            "units": ["skcapstone.service"],
        }

    monkeypatch.setattr(deployment_manifest, "build_manifest", fake_build_manifest)
    return calls


def _published_path(fleet_root: Path) -> Path:
    return fleet_root / "status" / "node-under-test" / "manifest" / "manifest.json"


def test_publishes_manifest_to_the_well_known_path(tmp_path, fake_manifest):
    home = tmp_path / "home"
    home.mkdir()
    fleet_root = tmp_path / "fleet"

    result = CliRunner().invoke(
        fleet,
        ["node", "manifest", "--repo-root", str(tmp_path), "--home", str(home)],
        env=_env(fleet_root),
    )

    assert result.exit_code == 0, result.output
    published = _published_path(fleet_root)
    assert published.exists(), "write_manifest was never called with the well-known path"
    payload = json.loads(published.read_text())
    assert payload["git_sha"] == "abc12345"
    assert payload["units"] == ["skcapstone.service"]


def test_publish_is_idempotent_and_overwrites_the_prior_manifest(tmp_path, fake_manifest):
    home = tmp_path / "home"
    home.mkdir()
    fleet_root = tmp_path / "fleet"
    runner = CliRunner()

    runner.invoke(
        fleet,
        ["node", "manifest", "--repo-root", str(tmp_path), "--home", str(home)],
        env=_env(fleet_root),
    )
    result = runner.invoke(
        fleet,
        ["node", "manifest", "--repo-root", str(tmp_path), "--home", str(home)],
        env=_env(fleet_root),
    )

    assert result.exit_code == 0, result.output


def test_json_flag_prints_the_published_manifest(tmp_path, fake_manifest):
    home = tmp_path / "home"
    home.mkdir()
    fleet_root = tmp_path / "fleet"

    result = CliRunner().invoke(
        fleet,
        ["node", "manifest", "--repo-root", str(tmp_path), "--home", str(home), "--json"],
        env=_env(fleet_root),
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["git_sha"] == "abc12345"


def test_text_output_names_the_node_and_the_published_path(tmp_path, fake_manifest):
    home = tmp_path / "home"
    home.mkdir()
    fleet_root = tmp_path / "fleet"

    result = CliRunner().invoke(
        fleet,
        ["node", "manifest", "--repo-root", str(tmp_path), "--home", str(home)],
        env=_env(fleet_root),
    )

    assert result.exit_code == 0, result.output
    assert "node-under-test" in result.output
    assert "abc12345" in result.output
    assert "manifest.json" in result.output


def test_a_build_manifest_error_is_a_clean_cli_error_not_a_traceback(tmp_path):
    def boom(repo_root, home):
        raise RuntimeError("could not resolve the git HEAD")

    import skcapstone.fleet.deployment_manifest as dm

    original = dm.build_manifest
    dm.build_manifest = boom
    try:
        result = CliRunner().invoke(
            fleet,
            ["node", "manifest", "--repo-root", str(tmp_path), "--home", str(tmp_path)],
            env=_env(tmp_path / "fleet"),
        )
    finally:
        dm.build_manifest = original

    assert result.exit_code != 0
    assert not isinstance(result.exception, RuntimeError)
    assert "could not resolve the git HEAD" in result.output


def test_real_pipeline_publishes_a_real_manifest(tmp_path):
    """No mocking: build_manifest against this real checkout, written for
    real, and read back for real -- proves the wiring itself, not just the
    mocked call shape.
    """
    repo_root = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    home.mkdir()
    fleet_root = tmp_path / "fleet"

    result = CliRunner().invoke(
        fleet,
        ["node", "manifest", "--repo-root", str(repo_root), "--home", str(home)],
        env=_env(fleet_root),
    )

    assert result.exit_code == 0, result.output
    published = _published_path(fleet_root)
    payload = json.loads(published.read_text())
    assert payload["revision"]
    assert payload["git_sha"]
    assert "skcapstone.service" in payload["units"]
