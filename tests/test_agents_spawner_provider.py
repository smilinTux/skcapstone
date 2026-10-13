"""Local provider initialization and spawn rollback guards."""

from pathlib import Path

import pytest

from skcapstone.cli import agents_spawner


def test_local_provider_uses_supported_work_dir_constructor(tmp_path: Path):
    backend, provider_type = agents_spawner._resolve_provider_backend(
        "local", tmp_path
    )

    assert provider_type.value == "local"
    assert backend._work_dir == tmp_path / "agents" / "local"


def test_provider_initialization_failure_stops_before_spawn(monkeypatch, tmp_path: Path):
    class BrokenProvider:
        def __init__(self, **kwargs):
            raise TypeError("unexpected keyword argument agents_root")

    import skcapstone.providers as providers

    monkeypatch.setattr(providers, "LocalProvider", BrokenProvider)

    with pytest.raises(TypeError, match="agents_root"):
        agents_spawner._resolve_provider_backend("local", tmp_path)

    # The resolver fails before SubAgentSpawner or TeamEngine is constructed,
    # so no deployment file or coordination claim can be created.
    assert not (tmp_path / "deployments").exists()
    assert not (tmp_path / "comms").exists()
