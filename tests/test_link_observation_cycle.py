from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).parents[1] / "scripts/fleet/skfleet-link-producer.py"
SPEC = importlib.util.spec_from_file_location("skfleet_link_producer", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

REPOSITORIES = (
    "smilinTux/skcapstone",
    "smilinTux/skdashboard",
    "smilinTux/skworld",
    "smilinTux/sk-standards",
)


def configure(monkeypatch) -> None:
    monkeypatch.setenv("SKAGENT", "link-producer")
    monkeypatch.setenv("SKCAPSTONE_AGENT", "link-producer")
    monkeypatch.setenv("SKFLEET_LINK_REPOSITORIES", ",".join(REPOSITORIES))


def test_role_identity_is_required(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("SKAGENT", raising=False)
    monkeypatch.delenv("SKCAPSTONE_AGENT", raising=False)
    assert MODULE.run(home=tmp_path) == 77


def test_repository_scope_is_required_and_fails_before_execution(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("SKAGENT", "link-producer")
    monkeypatch.setenv("SKCAPSTONE_AGENT", "link-producer")
    monkeypatch.delenv("SKFLEET_LINK_REPOSITORIES", raising=False)
    monkeypatch.setattr(
        MODULE.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("executed")),
    )
    assert MODULE.run(home=tmp_path) == 78


def test_repository_scope_rejects_malformed_or_non_exact_values(
    tmp_path: Path, monkeypatch
) -> None:
    configure(monkeypatch)
    invalid = (
        "",
        "smilinTux/skcapstone,not-a-repository",
        ",".join(REPOSITORIES[:-1]),
        ",".join((*REPOSITORIES, "smilinTux/extra")),
        ",".join((*REPOSITORIES, REPOSITORIES[-1])),
        ",".join(REPOSITORIES) + ",",
        ",,".join(REPOSITORIES),
    )
    monkeypatch.setattr(
        MODULE.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("executed")),
    )
    for scope in invalid:
        monkeypatch.setenv("SKFLEET_LINK_REPOSITORIES", scope)
        assert MODULE.run(home=tmp_path) == 78


def test_incomplete_lineage_is_bounded_and_preserves_link_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    configure(monkeypatch)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if "link-lineage.py" in command[1]:
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(
            returncode=78,
            stdout=json.dumps({"healthy": False, "reason": "lineage_incomplete"}) + "\n",
            stderr="",
        )

    monkeypatch.setattr(MODULE.subprocess, "run", fake_run)
    assert MODULE.run(home=tmp_path) == 0
    assert len(calls) == 2
    assert str(tmp_path / "coordination/link-lineage.json") in calls[0]
    assert str(tmp_path / "coordination/link-observations.json") in calls[1]
    for repository in REPOSITORIES:
        assert repository in calls[0]
        assert repository in calls[1]


def test_real_producer_failure_is_propagated(tmp_path: Path, monkeypatch) -> None:
    configure(monkeypatch)
    results = iter(
        [
            SimpleNamespace(returncode=0),
            SimpleNamespace(
                returncode=78,
                stdout=json.dumps({"healthy": False, "reason": "connector_unavailable"}) + "\n",
                stderr="",
            ),
        ]
    )
    monkeypatch.setattr(MODULE.subprocess, "run", lambda *args, **kwargs: next(results))
    assert MODULE.run(home=tmp_path) == 78


def test_packaged_units_are_identical() -> None:
    root = Path(__file__).parents[1]
    for suffix in ("service", "timer"):
        name = f"skfleet-link-producer.{suffix}"
        assert (root / "systemd" / name).read_bytes() == (
            root / "src/skcapstone/data/systemd" / name
        ).read_bytes()


def test_service_supplies_exact_repository_scope() -> None:
    service = (Path(__file__).parents[1] / "systemd/skfleet-link-producer.service").read_text()
    assert "Environment=SKFLEET_LINK_REPOSITORIES=" + ",".join(REPOSITORIES) in service


def test_installer_wires_wrapper_and_units() -> None:
    installer = (Path(__file__).parents[1] / "scripts/install.sh").read_text()
    assert "skfleet-link-producer.service skfleet-link-producer.timer" in installer
    packaging = (Path(__file__).parents[1] / "pyproject.toml").read_text()
    assert '"scripts/fleet/link-lineage.py"' in packaging
    assert '"scripts/fleet/skfleet-link-producer.py"' in packaging
