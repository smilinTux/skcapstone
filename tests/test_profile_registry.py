"""Acceptance and adversarial sensitivity tests for the common profile registry."""

from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path

import pytest

from skcapstone import _detect_active_agent as detect_active_agent
from skcapstone.profile_registry import (
    conformance_pack_hash,
    profile_content_hash,
    profile_is_eligible,
    registry_content_hash,
    resolve_profile,
)

FIXTURE_PACK = Path(__file__).parents[1] / "src/skcapstone/data/profile-conformance-v1.json"
EXPECTED_PACK_HASH = "sha256:52f74fca0abbb0ad8fe54fc550b83827175eff1f14b5fa3aad0140ad9a8a56e1"
PICKER = Path(__file__).parents[1] / "src/skcapstone/data/sk-agent-picker.sh"


def _write_case(root: Path, case: dict) -> None:
    """Materialize one synthetic registry fixture under a shared root."""
    registry = case["registry"]
    if registry is not None:
        path = root / "config/profile-registry.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(registry if isinstance(registry, str) else json.dumps(registry))
    profile = case["profile"]
    if profile is not None:
        path = root / "agents" / case["profile_id"] / "profile.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(profile if isinstance(profile, str) else json.dumps(profile))


def _load_pack() -> dict:
    """Load the public-synthetic golden fixture pack."""
    return json.loads(FIXTURE_PACK.read_text(encoding="utf-8"))


def _case(name: str) -> dict:
    """Return a deep copy of one named fixture."""
    return copy.deepcopy(next(case for case in _load_pack()["cases"] if case["name"] == name))


def _rehash_profile(case: dict) -> None:
    """Rebind a deliberately changed fixture profile and registry entry."""
    profile = case["profile"]
    profile["profile_hash"] = profile_content_hash(profile)
    binding = next(
        entry
        for entry in case["registry"]["profiles"]
        if entry["profile_id"] == case["profile_id"]
    )
    for field in (
        "profile_kind",
        "selectable",
        "fallback_eligible",
        "memory_principal_id",
        "schema_revision",
        "profile_revision",
        "profile_hash",
    ):
        binding[field] = profile[field]
    case["registry"]["registry_hash"] = registry_content_hash(case["registry"])


def test_fixture_pack_exact_hash_and_every_golden_case(tmp_path: Path) -> None:
    """The published hash is exact and all positive and denied vectors resolve exactly."""
    pack = _load_pack()
    assert pack["synthetic"] is True
    assert pack["pack_hash"] == EXPECTED_PACK_HASH
    assert conformance_pack_hash(pack) == EXPECTED_PACK_HASH

    for index, case in enumerate(pack["cases"]):
        root = tmp_path / str(index)
        _write_case(root, case)
        resolved = resolve_profile(root, case["profile_id"])
        assert {
            "state": resolved.state,
            "healthy": resolved.healthy,
            "selectable": resolved.selectable,
            "fallback_eligible": resolved.fallback_eligible,
            "memory_principal_id": resolved.memory_principal_id,
            "default_tools": resolved.default_tools,
        } == case["expected"], case["name"]
        assert resolved.state != "Unknown"


def test_fixture_pack_sensitivity_breaks_each_condition(tmp_path: Path) -> None:
    """Breaking every golden fixture condition changes its exact projection."""
    mutators = {
        "healthy_human": lambda c: c["profile"].update(memory_principal_id="memory:tampered"),
        "bounded_service": lambda c: c["profile"].update(default_tools=["memory_search"]),
        "missing_registry": lambda c: c.update(registry=_case("healthy_human")["registry"]),
        "corrupt_registry": lambda c: c.update(registry=_case("healthy_human")["registry"]),
        "unknown_registry_version": lambda c: c["registry"].update(
            schema_version="skcapstone.profile-registry.v1"
        ),
        "registry_hash_mismatch": lambda c: c["registry"].update(registry_revision="synthetic-1"),
        "unknown_profile": lambda c: c.update(profile_id="synthetic-human"),
        "missing_profile": lambda c: c.update(profile=_case("healthy_human")["profile"]),
        "corrupt_profile": lambda c: c.update(profile=_case("healthy_human")["profile"]),
        "unknown_profile_version": lambda c: c["profile"].update(
            schema_version="skcapstone.agent-profile.v1"
        ),
        "stale_profile": lambda c: c["profile"].update(profile_revision="1"),
        "profile_hash_mismatch": lambda c: c["profile"].update(
            memory_principal_id="memory:synthetic-human"
        ),
        "profile_id_mismatch": lambda c: c["profile"].update(profile_id="synthetic-human"),
        "service_selectable_conflict": lambda c: c["profile"].update(selectable=False),
        "service_tool_conflict": lambda c: c["profile"].update(default_tools=[]),
    }
    for index, original in enumerate(_load_pack()["cases"]):
        case = copy.deepcopy(original)
        mutators[case["name"]](case)
        if isinstance(case.get("profile"), dict) and isinstance(case.get("registry"), dict):
            _rehash_profile(case)
        elif isinstance(case.get("registry"), dict):
            case["registry"]["registry_hash"] = registry_content_hash(case["registry"])
        root = tmp_path / str(index)
        _write_case(root, case)
        resolved = resolve_profile(root, case["profile_id"])
        projection = {
            "state": resolved.state,
            "healthy": resolved.healthy,
            "selectable": resolved.selectable,
            "fallback_eligible": resolved.fallback_eligible,
            "memory_principal_id": resolved.memory_principal_id,
            "default_tools": resolved.default_tools,
        }
        assert projection != original["expected"], case["name"]


@pytest.mark.parametrize("field", ["selectable", "fallback_eligible", "default_tools"])
def test_service_cannot_cross_identity_or_tool_boundary(tmp_path: Path, field: str) -> None:
    """Even correctly rehashed service metadata cannot opt into human behavior."""
    case = _case("bounded_service")
    case["profile"][field] = ["memory_search"] if field == "default_tools" else True
    _rehash_profile(case)
    _write_case(tmp_path, case)

    resolved = resolve_profile(tmp_path, case["profile_id"])
    assert resolved.healthy is False
    assert resolved.memory_principal_id is None
    assert resolved.default_tools == []
    assert not profile_is_eligible(tmp_path, case["profile_id"])


def test_active_discovery_excludes_service_and_preserves_explicit_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discovery accepts an approved human and never falls back to a service."""
    import skcapstone

    case = _case("healthy_human")
    registry = copy.deepcopy(case["registry"])
    service = _case("bounded_service")
    registry["registry_hash"] = registry_content_hash(registry)
    case["registry"] = registry
    service["registry"] = registry
    _write_case(tmp_path, case)
    _write_case(tmp_path, service)

    monkeypatch.setenv("SKAGENT", "synthetic-human")
    assert detect_active_agent(str(tmp_path)) == "synthetic-human"
    monkeypatch.setenv("SKAGENT", "synthetic-service")
    assert detect_active_agent(str(tmp_path)) is None
    monkeypatch.delenv("SKAGENT")
    monkeypatch.delenv("SKCAPSTONE_AGENT", raising=False)
    monkeypatch.setattr(skcapstone, "DEFAULT_AGENT", "")
    assert detect_active_agent(str(tmp_path)) == "synthetic-human"


def test_shell_picker_and_first_directory_fallback_exclude_service(tmp_path: Path) -> None:
    """Interactive inventory, explicit switch, and first-directory fallback reject service."""
    human = _case("healthy_human")
    service = _case("bounded_service")
    registry = copy.deepcopy(human["registry"])
    registry["registry_hash"] = registry_content_hash(registry)
    human["registry"] = registry
    service["registry"] = registry
    _write_case(tmp_path, human)
    _write_case(tmp_path, service)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    probe = bin_dir / "profile-probe"
    probe.write_text("#!/bin/sh\nprintf 'launched=%s\\n' \"$SKAGENT\"\n")
    probe.chmod(0o755)

    script = f"""source {PICKER!s}
SKAGENT=synthetic-service
picked=$(_sk_pick_agent)
printf 'picked=%s\\n' "$picked"
skswitch synthetic-service >/dev/null 2>&1; printf 'service_rc=%s\\n' "$?"
SKAGENT= SKCAPSTONE_AGENT= _sk_profile_eligible synthetic-human fallback
printf 'fallback_human_rc=%s\\n' "$?"
SKAGENT= SKCAPSTONE_AGENT= _sk_profile_eligible synthetic-service fallback
printf 'fallback_service_rc=%s\\n' "$?"
SKAGENT= SKCAPSTONE_AGENT= _sk_launch profile-probe '' --print
"""
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=Path(__file__).parents[1],
        env={
            "HOME": str(tmp_path),
            "SKCAPSTONE_HOME": str(tmp_path),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
        },
        text=True,
        capture_output=True,
        check=True,
    )
    assert "picked=synthetic-human" in result.stdout
    assert "service_rc=1" in result.stdout
    assert "fallback_human_rc=0" in result.stdout
    assert "fallback_service_rc=1" in result.stdout
    assert "launched=synthetic-human" in result.stdout
