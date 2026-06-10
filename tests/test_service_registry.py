"""T4 — service_health unions sdk.register_service entries with defaults."""

from __future__ import annotations

from pathlib import Path

import pytest

from skcapstone import sdk, service_health


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    import skcapstone as pkg

    monkeypatch.setattr(pkg, "AGENT_HOME", str(tmp_path))
    # Disable the built-in checks that honour the "disabled" sentinel so the
    # test stays offline. The daemon/skchat checks probe localhost and fail
    # fast (connection refused → status "down"), which is fine for these
    # assertions about the registry union.
    for var in ("SKMEMORY_SKVECTOR_URL", "SKMEMORY_SKGRAPH_HOST", "SYNCTHING_API_URL"):
        monkeypatch.setenv(var, "disabled")
    return tmp_path


def test_registered_service_appears(home: Path):
    sdk.register_service("myservice", pid_file=str(home / "x.pid"))
    names = {r["name"] for r in service_health.check_all_services()}
    assert "myservice" in names


def test_registry_entry_without_targets_is_unknown(home: Path):
    sdk.register_service("bare")  # no health_url, no pid_file
    row = next(r for r in service_health.check_all_services() if r["name"] == "bare")
    assert row["status"] == "unknown"
    assert "without" in (row["error"] or "")


def test_empty_registry_no_extra_rows(home: Path):
    """With no registry, only built-in checks appear (none crash)."""
    rows = service_health.check_all_services()
    # registry dir does not exist → loader returns [] and adds nothing
    assert isinstance(rows, list)
    assert all("name" in r for r in rows)


def test_builtin_name_not_duplicated_by_registry(home: Path):
    """A registry entry colliding with a built-in name is skipped."""
    sdk.register_service("skcapstone daemon", pid_file=str(home / "d.pid"))
    names = [r["name"] for r in service_health.check_all_services()]
    assert names.count("skcapstone daemon") == 1
