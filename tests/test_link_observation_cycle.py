from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from skcapstone.forgejo import SKGIT_REPOSITORY

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
        ",".join((*REPOSITORIES, SKGIT_REPOSITORY + ".git")),
        ",".join((*REPOSITORIES, SKGIT_REPOSITORY + "?token=bad")),
        ",".join((*REPOSITORIES, "https://other.invalid/smilinTux/sklegal")),
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


def _scope_file(home: Path, repositories: tuple[str, ...]) -> None:
    path = home / MODULE.REPOSITORY_SCOPE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema": MODULE.REPOSITORY_SCOPE_SCHEMA, "repositories": list(repositories)}),
        encoding="utf-8",
    )


def _no_subprocess(monkeypatch) -> None:
    monkeypatch.setattr(
        MODULE.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("executed")),
    )


def test_an_estate_with_no_scope_file_keeps_the_packaged_scope(tmp_path: Path) -> None:
    """An un-migrated node must behave exactly as before, not fail closed."""
    assert MODULE.declared_repositories(tmp_path) == MODULE.REQUIRED_REPOSITORIES


def test_an_estate_scope_file_replaces_the_packaged_scope(tmp_path: Path, monkeypatch) -> None:
    """The packaged set is right for nor and wrong for any estate watching
    different repositories, and it used to be the only set the producer would
    accept: anything else exited 78, so a second estate could not be configured
    without editing the script."""
    elsewhere = ("smilinTux/other-one", "smilinTux/other-two")
    _scope_file(tmp_path, elsewhere)
    configure(monkeypatch)
    _no_subprocess(monkeypatch)

    monkeypatch.setenv("SKFLEET_LINK_REPOSITORIES", ",".join(REPOSITORIES))
    assert MODULE.run(home=tmp_path) == 78  # the packaged set is no longer declared

    monkeypatch.setenv("SKFLEET_LINK_REPOSITORIES", ",".join(elsewhere))
    assert MODULE.repository_scope(tmp_path) == elsewhere


def test_a_declared_scope_still_has_to_match_the_environment_exactly(
    tmp_path: Path, monkeypatch
) -> None:
    """Moving the declaration out of the script must not weaken the check. The
    environment is what the producer actually fetches, so a narrower
    environment would publish a feed that looks complete while ignoring whole
    repositories."""
    declared = ("smilinTux/one", "smilinTux/two", "smilinTux/three")
    _scope_file(tmp_path, declared)
    configure(monkeypatch)
    _no_subprocess(monkeypatch)
    for scope in (
        ",".join(declared[:-1]),
        ",".join((*declared, "smilinTux/extra")),
        ",".join((*declared, declared[0])),
        ",".join(declared) + ",",
    ):
        monkeypatch.setenv("SKFLEET_LINK_REPOSITORIES", scope)
        assert MODULE.run(home=tmp_path) == 78
    monkeypatch.setenv("SKFLEET_LINK_REPOSITORIES", ",".join((*declared, SKGIT_REPOSITORY)))
    assert MODULE.repository_scope(tmp_path) == (*declared, SKGIT_REPOSITORY)


def test_a_malformed_scope_file_fails_closed_instead_of_falling_back(
    tmp_path: Path, monkeypatch
) -> None:
    """Silently substituting the packaged default for a scope the operator
    believes is in force is worse than a hard failure."""
    configure(monkeypatch)
    _no_subprocess(monkeypatch)
    path = tmp_path / MODULE.REPOSITORY_SCOPE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    for body in (
        "{ not json",
        json.dumps({"repositories": list(REPOSITORIES)}),
        json.dumps({"schema": "skfleet.link-scope/v0", "repositories": list(REPOSITORIES)}),
        json.dumps({"schema": MODULE.REPOSITORY_SCOPE_SCHEMA, "repositories": []}),
        json.dumps({"schema": MODULE.REPOSITORY_SCOPE_SCHEMA, "repositories": ["  "]}),
        json.dumps({"schema": MODULE.REPOSITORY_SCOPE_SCHEMA, "repositories": ["a", "a"]}),
        json.dumps({"schema": MODULE.REPOSITORY_SCOPE_SCHEMA, "repositories": "a,b"}),
    ):
        path.write_text(body, encoding="utf-8")
        assert MODULE.run(home=tmp_path) == 78


def test_exclusions_are_passed_to_the_reconciler_when_the_estate_has_them(
    tmp_path: Path, monkeypatch
) -> None:
    """link-lineage.py has accepted --exclude all along, but the producer never
    passed it, so the bounded exclusion record the reconciliation doc requires
    was unreachable from the one path that actually runs the reconciler."""
    configure(monkeypatch)
    exclusions = tmp_path / MODULE.EXCLUSIONS_FILE
    exclusions.parent.mkdir(parents=True, exist_ok=True)
    exclusions.write_text("[]", encoding="utf-8")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(MODULE.subprocess, "run", run)
    assert MODULE.run(home=tmp_path) == 0
    assert "--exclude" in calls[0]
    assert str(exclusions) in calls[0]
    assert calls[0][calls[0].index("--exclude") + 1] == str(exclusions)


def test_no_exclusion_file_means_no_exclude_flag(tmp_path: Path, monkeypatch) -> None:
    """An estate that excludes nothing behaves exactly as before."""
    configure(monkeypatch)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(MODULE.subprocess, "run", run)
    assert MODULE.run(home=tmp_path) == 0
    assert "--exclude" not in calls[0]


def test_packaged_units_are_identical() -> None:
    root = Path(__file__).parents[1]
    for suffix in ("service", "timer"):
        name = f"skfleet-link-producer.{suffix}"
        assert (root / "systemd" / name).read_bytes() == (
            root / "src/skcapstone/data/systemd" / name
        ).read_bytes()


def test_service_supplies_exact_repository_scope() -> None:
    service = (Path(__file__).parents[1] / "systemd/skfleet-link-producer.service").read_text()
    assert (
        "Environment=SKFLEET_LINK_REPOSITORIES=" + ",".join((*REPOSITORIES, SKGIT_REPOSITORY))
        in service
    )
    assert "EnvironmentFile=-%h/api-keys/link-skgit.env" in service


def test_private_route_is_optional_and_passed_to_both_steps(tmp_path, monkeypatch):
    configure(monkeypatch)
    assert MODULE.repository_scope() == REPOSITORIES
    monkeypatch.setenv("SKFLEET_LINK_REPOSITORIES", ",".join((*REPOSITORIES, SKGIT_REPOSITORY)))
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(MODULE.subprocess, "run", run)
    assert MODULE.run(home=tmp_path) == 0
    assert len(calls) == 2
    assert all(SKGIT_REPOSITORY in command for command in calls)


def test_installer_wires_wrapper_and_units() -> None:
    installer = (Path(__file__).parents[1] / "scripts/install.sh").read_text()
    assert "skfleet-link-producer.service skfleet-link-producer.timer" in installer
    packaging = (Path(__file__).parents[1] / "pyproject.toml").read_text()
    assert '"scripts/fleet/link-lineage.py"' in packaging
    assert '"scripts/fleet/skfleet-link-producer.py"' in packaging


def test_wrapper_gives_both_stages_one_private_run_cache(tmp_path, monkeypatch):
    configure(monkeypatch)
    seen = []

    def run(command, **kwargs):
        env = kwargs["env"]
        seen.append((env["SKFLEET_LINK_FETCH_CACHE"], env["SKFLEET_LINK_RUN_ID"]))
        assert Path(env["SKFLEET_LINK_FETCH_CACHE"]).parent.is_dir()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(MODULE.subprocess, "run", run)
    assert MODULE.run(home=tmp_path) == 0
    assert seen[0] == seen[1]
    assert not Path(seen[0][0]).parent.exists()  # removed when the run ends
    MODULE.run(home=tmp_path)
    assert seen[2][1] != seen[0][1]  # a new run id every run
