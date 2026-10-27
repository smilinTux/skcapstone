"""Scoping, time bounds and seat classification for doctor's memory checks.

On chiap04 (2026-10-27) ``skcapstone doctor`` never finished: the memory drift
check ran one ``skmemory health`` subprocess per agent folder with a
``memory/`` dir, 1,272 of them out of ~4,320 folders that are almost all
ephemeral coding-fleet seats. And for every unregistered seat skmemory's
fail-closed resolver reported on the ``template`` agent instead, so each of
those checks measured the wrong corpus.

Covers ``skcapstone.agent_scope`` and the scoped/bounded
``doctor._check_memory_drift`` / ``_check_memory_sync_timer``, plus
``skcapstone agents stale``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from skcapstone import agent_scope
from skcapstone.doctor import (
    _check_memory_drift,
    _check_memory_sync_timer,
    _drift_scope,
    _readonly_flat_drift,
    run_diagnostics,
)

#: Captured before the autouse fixture swaps it for a lenient stand-in.
_REAL_RESOLVABLE = agent_scope.resolvable_memory_profiles

HEALTHY = {"status": "ok", "primary": {"ok": True, "sync": {"sqlite_only": 0, "flat_only": 0}}}


@pytest.fixture(autouse=True)
def _no_selectors(monkeypatch):
    for var in agent_scope.AGENT_SELECTOR_VARS:
        monkeypatch.delenv(var, raising=False)
    # Fixture registries are lenient (no hashes). Treat every entry as
    # resolvable here; the strict resolver has its own tests below/elsewhere.
    from skcapstone import agent_scope as _scope

    monkeypatch.setattr(
        _scope,
        "resolvable_memory_profiles",
        lambda root: (_scope.registered_memory_profiles(root) or set(), {}),
    )


def _estate(tmp_path, registered=(), seats=(), real=(), register=True):
    """Build a shared root: registered agents, bare seats, and soul-bearing agents."""
    agents = tmp_path / "agents"
    for name in (*registered, *seats, *real):
        (agents / name / "memory" / "short-term").mkdir(parents=True)
    for name in real:
        (agents / name / "soul").mkdir()
    if register:
        (tmp_path / "config").mkdir()
        (tmp_path / "config" / "profile-registry.json").write_text(
            json.dumps(
                {
                    "profiles": [
                        {"profile_id": n, "memory_principal_id": f"memory:{n}"} for n in registered
                    ]
                    + [{"profile_id": "svc", "memory_principal_id": None}]
                }
            )
        )
    return tmp_path


def _by_name(checks):
    return {c.name: c for c in checks}


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


def test_default_scope_is_registered_plus_active_not_every_folder(tmp_path, monkeypatch):
    home = _estate(tmp_path, registered=["jarvis"], seats=["pi-codex-chiap01-01c4363c", "Jarvis"])
    (home / "agents" / "chiwk11" / "memory").mkdir(parents=True)
    monkeypatch.setenv("SKAGENT", "chiwk11")
    scope = _drift_scope(home)
    assert scope.measured == ["chiwk11", "jarvis"]
    assert scope.skipped == ["Jarvis", "pi-codex-chiap01-01c4363c"]
    assert scope.swept == []
    assert scope.seats == 2


def test_unregistered_folders_are_never_handed_to_skmemory(tmp_path):
    home = _estate(tmp_path, registered=["jarvis"], seats=[f"codex-{i}" for i in range(50)])
    called = []

    def fake(agent, timeout=None):
        called.append(agent)
        return HEALTHY

    with patch("skcapstone.doctor._skmemory_health_json", side_effect=fake):
        checks = _check_memory_drift(home)
    assert called == ["jarvis"]
    scope = _by_name(checks)["memory:drift:scope"]
    assert scope.passed and not scope.unknown
    assert "skipped 50 other memory folder" in scope.detail
    assert "50 ephemeral worker seats" in scope.detail
    assert "--all-agents" in scope.detail


def test_missing_registry_falls_back_to_active_only(tmp_path, monkeypatch):
    home = _estate(tmp_path, seats=["lumina", "opus"], register=False)
    monkeypatch.setenv("SKAGENT", "lumina")
    with patch("skcapstone.doctor._skmemory_health_json", return_value=HEALTHY) as fake:
        checks = _check_memory_drift(home)
    assert [c.args[0] for c in fake.call_args_list] == ["lumina"]
    assert "registry unreadable" in _by_name(checks)["memory:drift:scope"].detail


def test_service_profile_without_memory_principal_is_not_in_scope(tmp_path):
    home = _estate(tmp_path, registered=["jarvis"])
    (home / "agents" / "svc" / "memory").mkdir(parents=True)
    assert _drift_scope(home).measured == ["jarvis"]


def test_health_resolved_to_another_agent_fails_instead_of_passing(tmp_path):
    """The template fallback: skmemory answered, but about someone else."""
    home = _estate(tmp_path, registered=["jarvis"])
    template_health = dict(
        HEALTHY,
        primary=dict(HEALTHY["primary"], base_path=str(tmp_path / "agents/template/memory")),
    )
    with patch("skcapstone.doctor._skmemory_health_json", return_value=template_health):
        check = _by_name(_check_memory_drift(home))["memory:drift:jarvis"]
    assert check.passed is False and check.unknown is False
    assert "'template'" in check.detail and "NOT measured" in check.detail
    assert "profile status" in check.fix


def test_health_for_the_right_agent_passes(tmp_path):
    home = _estate(tmp_path, registered=["jarvis"])
    ok = dict(
        HEALTHY, primary=dict(HEALTHY["primary"], base_path=str(tmp_path / "agents/jarvis/memory"))
    )
    with patch("skcapstone.doctor._skmemory_health_json", return_value=ok):
        assert _by_name(_check_memory_drift(home))["memory:drift:jarvis"].passed is True


# ---------------------------------------------------------------------------
# Time bounds
# ---------------------------------------------------------------------------


def test_budget_bounds_total_time_and_reports_unreached_as_unknown(tmp_path):
    names = [f"agent{i}" for i in range(8)]
    home = _estate(tmp_path, registered=names)

    def slow(agent, timeout=None):
        time.sleep(min(0.6, timeout or 0.6))
        return HEALTHY

    started = time.monotonic()
    with patch("skcapstone.doctor._skmemory_health_json", side_effect=slow):
        checks = _check_memory_drift(home, budget=0.9, workers=2)
    elapsed = time.monotonic() - started
    assert elapsed < 3.0
    by = _by_name(checks)
    unknown = [n for n in names if by[f"memory:drift:{n}"].unknown]
    assert unknown, "some agents must be reported as not reached"
    assert all("budget" in by[f"memory:drift:{n}"].detail for n in unknown)
    assert any(
        by[f"memory:drift:{n}"].passed and not by[f"memory:drift:{n}"].unknown for n in names
    )


def test_per_agent_timeout_is_capped_by_remaining_budget(tmp_path):
    home = _estate(tmp_path, registered=["jarvis"])
    seen = []

    def fake(agent, timeout=None):
        seen.append(timeout)
        return HEALTHY

    with patch("skcapstone.doctor._skmemory_health_json", side_effect=fake):
        _check_memory_drift(home, budget=5)
    assert seen and 0 < seen[0] <= 5


def test_skmemory_health_sets_all_three_selectors_and_timeout():
    class _Proc:
        stdout = "{}"

    with (
        patch("skcapstone.doctor.shutil.which", return_value="/bin/skmemory"),
        patch("skcapstone.doctor.subprocess.run", return_value=_Proc()) as run,
    ):
        from skcapstone.doctor import _skmemory_health_json

        assert _skmemory_health_json("jarvis", timeout=7) == {}
    kwargs = run.call_args.kwargs
    assert kwargs["timeout"] == 7
    assert {kwargs["env"][v] for v in agent_scope.AGENT_SELECTOR_VARS} == {"jarvis"}


# ---------------------------------------------------------------------------
# --all-agents read-only sweep
# ---------------------------------------------------------------------------


def _write_index(memory_dir, rows):
    conn = sqlite3.connect(memory_dir / "index.db")
    conn.execute("CREATE TABLE memories (id TEXT, layer TEXT, file_path TEXT)")
    conn.executemany("INSERT INTO memories VALUES (?,?,?)", rows)
    conn.commit()
    conn.close()


def test_readonly_drift_counts_orphans_and_ignores_archived(tmp_path):
    mem = tmp_path / "memory"
    (mem / "short-term").mkdir(parents=True)
    (mem / "archive" / "2026").mkdir(parents=True)
    (mem / "short-term" / "a.json").write_text("{}")
    (mem / "short-term" / "unindexed.json").write_text("{}")
    (mem / "archive" / "2026" / "cold.json").write_text("{}")
    _write_index(
        mem,
        [
            ("a", "short-term", str(mem / "short-term" / "a.json")),
            ("cold", "short-term", str(mem / "short-term" / "cold.json")),
            ("gone", "short-term", str(mem / "short-term" / "gone.json")),
        ],
    )
    drift = _readonly_flat_drift(mem)
    assert drift["sqlite_only"] == 1
    assert drift["flat_only"] == 1


def test_readonly_drift_creates_nothing(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    drift = _readonly_flat_drift(mem)
    assert drift == {
        "flat_total": 0,
        "sqlite_total": 0,
        "sqlite_only": 0,
        "flat_only": 0,
        "has_index": False,
    }
    assert list(mem.iterdir()) == []


def test_all_agents_sweeps_seats_in_process_and_reports_only_problems(tmp_path):
    seats = [f"pi-codex-{i}" for i in range(30)]
    home = _estate(tmp_path, registered=["jarvis"], seats=seats)
    (home / "agents" / "pi-codex-7" / "memory" / "short-term" / "x.json").write_text("{}")
    with patch("skcapstone.doctor._skmemory_health_json", return_value=HEALTHY) as fake:
        checks = _check_memory_drift(home, all_agents=True)
    assert fake.call_count == 1  # seats are swept in-process, never via skmemory
    by = _by_name(checks)
    assert by["memory:drift:pi-codex-7"].passed is False
    assert "flat_only=1" in by["memory:drift:pi-codex-7"].detail
    # A bare `skmemory profile sync` unregisters every profile without a
    # config/skmemory.yaml; the suggestion must always lead with a dry run.
    assert "`skmemory profile sync --dry-run` first" in by["memory:drift:pi-codex-7"].fix
    assert "memory:drift:pi-codex-8" not in by
    scope = by["memory:drift:scope"]
    assert "swept 30/30 other" in scope.detail and "1 with problems" in scope.detail
    assert "slowest:" in scope.detail


def test_all_agents_sweep_stops_at_budget(tmp_path):
    home = _estate(tmp_path, seats=[f"pi-{i}" for i in range(20)])
    with patch(
        "skcapstone.doctor._readonly_flat_drift",
        side_effect=lambda p: (
            time.sleep(0.05)
            or {
                "flat_total": 0,
                "sqlite_total": 0,
                "sqlite_only": 0,
                "flat_only": 0,
                "has_index": False,
            }
        ),
    ):
        checks = _check_memory_drift(home, all_agents=True, budget=0.2)
    scope = _by_name(checks)["memory:drift:scope"]
    assert scope.unknown is True
    assert "not reached within the 0s budget" in scope.detail or "not reached" in scope.detail


def test_run_diagnostics_passes_scope_options_through(tmp_path, monkeypatch):
    import skcapstone.doctor as doctor

    for name in dir(doctor):
        if name.startswith("_check_"):
            monkeypatch.setattr(doctor, name, lambda *a, **k: [])
    with patch("skcapstone.doctor._check_memory_drift", return_value=[]) as drift:
        run_diagnostics(tmp_path, all_agents=True, drift_budget=42)
    assert drift.call_args.kwargs == {"all_agents": True, "budget": 42}


# ---------------------------------------------------------------------------
# Sync timer scope
# ---------------------------------------------------------------------------


def test_sync_timer_check_skips_seats(tmp_path):
    home = _estate(tmp_path, registered=["jarvis"], seats=[f"codex-{i}" for i in range(20)])

    class _Proc:
        stdout = "enabled\n"

    with (
        patch("skcapstone.doctor.platform.system", return_value="Linux"),
        patch("skcapstone.systemd.systemd_available", return_value=True),
        patch("skcapstone.doctor.subprocess.run", return_value=_Proc()) as run,
    ):
        checks = _check_memory_sync_timer(home)
    assert [c.name for c in checks] == ["memory:sync_timer:jarvis"]
    assert run.call_count == 1


# ---------------------------------------------------------------------------
# Seat classification
# ---------------------------------------------------------------------------


def test_seat_classification(tmp_path):
    agents = tmp_path / "agents"
    for name in (
        "jarvis",
        "aster",
        "chiwk11",
        "template",
        "lumina-template",
        "pi-glm-x",
        "--help",
        "me",
    ):
        (agents / name).mkdir(parents=True)
    (agents / "aster" / "soul").mkdir()
    (agents / "chiwk11" / "identity").mkdir()
    reg = {"jarvis"}
    seat = lambda n: agent_scope.is_ephemeral_seat(agents / n, reg, active="me")  # noqa: E731
    assert not seat("jarvis")  # registered
    assert not seat("aster")  # soul
    assert not seat("chiwk11")  # identity
    assert not seat("template") and not seat("lumina-template")  # structural
    assert not seat("me")  # active
    assert seat("pi-glm-x") and seat("--help")


def test_seat_reason(tmp_path):
    lw = tmp_path / "role-a"
    lw.mkdir()
    (lw / "identity.json").write_text(json.dumps({"profile": "lightweight", "role": "worker"}))
    named = tmp_path / "codex-luna-8d314225-repairer"
    named.mkdir()
    stray = tmp_path / "Jarvis"
    stray.mkdir()
    assert agent_scope.seat_reason(lw) == "lightweight"
    assert agent_scope.seat_reason(named) == "name"
    assert agent_scope.seat_reason(stray) == "no-identity"


def test_registered_memory_profiles_corrupt_registry_is_none(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "profile-registry.json").write_text("{")
    assert agent_scope.registered_memory_profiles(tmp_path) is None
    assert agent_scope.registered_memory_profiles(tmp_path / "nope") is None


def test_shared_root_for_both_home_shapes(tmp_path):
    assert agent_scope.shared_root_for(tmp_path) == tmp_path
    assert agent_scope.shared_root_for(tmp_path / "agents" / "jarvis") == tmp_path


# ---------------------------------------------------------------------------
# Stale report
# ---------------------------------------------------------------------------


def _age(path, days):
    t = time.time() - days * 86400
    for dirpath, dirnames, filenames in os.walk(path):
        for n in dirnames + filenames:
            os.utime(os.path.join(dirpath, n), (t, t))
    os.utime(path, (t, t))


def _stale_estate(tmp_path):
    home = _estate(tmp_path, registered=["jarvis"], seats=["pi-old", "pi-fresh"], real=["aster"])
    (home / "agents" / "pi-old" / "memory" / "short-term" / "m.json").write_text("x" * 100)
    for name in ("pi-old", "jarvis", "aster"):
        _age(home / "agents" / name, 40)
    return home


def test_stale_report_lists_only_idle_seats(tmp_path):
    report = agent_scope.stale_seats(_stale_estate(tmp_path), days=14)
    assert [s.name for s in report.stale] == ["pi-old"]
    assert report.stale[0].size_bytes == 100 and report.stale[0].files == 1
    assert report.seats == 2 and report.total_dirs == 4
    assert set(report.real_agents) == {"jarvis", "aster"}


def test_stale_report_is_read_only(tmp_path):
    home = _stale_estate(tmp_path)
    before = sorted(p.name for p in (home / "agents").iterdir())
    agent_scope.stale_seats(home, days=14)
    assert sorted(p.name for p in (home / "agents").iterdir()) == before


def test_archive_moves_real_files_and_rechecks_each_seat(tmp_path, monkeypatch):
    home = _stale_estate(tmp_path)
    (home / "agents" / "pi-old2").mkdir()
    _age(home / "agents" / "pi-old2", 40)
    report = agent_scope.stale_seats(home, days=14)
    assert {s.name for s in report.stale} == {"pi-old", "pi-old2"}
    monkeypatch.setenv("SKAGENT", "pi-old2")  # became active after the report
    dest = tmp_path / "archive-out"
    moved = agent_scope.archive_seats(report, dest)
    assert [n for n, _ in moved] == ["pi-old"]
    assert (dest / "pi-old" / "memory" / "short-term" / "m.json").read_text() == "x" * 100
    assert not (home / "agents" / "pi-old").exists()
    assert (home / "agents" / "pi-old2").exists()


def test_agents_stale_cli_dry_run_json(tmp_path):
    from skcapstone.cli import main

    home = _stale_estate(tmp_path)
    result = CliRunner().invoke(main, ["agents", "stale", "--home", str(home), "--json-out"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["stale_count"] == 1 and data["stale_bytes"] == 100
    assert data["archived"] == []
    assert (home / "agents" / "pi-old").exists()


def test_agents_stale_cli_archive(tmp_path):
    from skcapstone.cli import main

    home = _stale_estate(tmp_path)
    out = tmp_path / "arch"
    result = CliRunner().invoke(
        main, ["agents", "stale", "--home", str(home), "--archive", "--archive-dir", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert (out / "pi-old").is_dir()
    assert "Archived 1 seat" in result.output


# ---------------------------------------------------------------------------
# Strict resolvability (registry entry present but profile does not resolve)
# ---------------------------------------------------------------------------


def _conformance_case(name):
    from importlib import resources

    pack = json.loads(
        resources.files("skcapstone").joinpath("data/profile-conformance-v1.json").read_text()
    )
    return next(c for c in pack["cases"] if c["name"] == name)


def test_resolvable_profiles_uses_the_strict_resolver(tmp_path):
    """noroc2027, 2026-10-27: 7 of 13 registry entries had no profile.json."""
    case = _conformance_case("healthy_human")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "profile-registry.json").write_text(json.dumps(case["registry"]))
    home = tmp_path / "agents" / "synthetic-human"
    home.mkdir(parents=True)
    (home / "profile.json").write_text(json.dumps(case["profile"]))
    healthy, broken = _REAL_RESOLVABLE(tmp_path)
    assert healthy == {"synthetic-human"}
    assert broken == {}

    (home / "profile.json").unlink()
    healthy, broken = _REAL_RESOLVABLE(tmp_path)
    assert healthy == set()
    assert broken == {"synthetic-human": "missing_profile"}


def test_broken_registered_profiles_are_named_not_measured(tmp_path, monkeypatch):
    home = _estate(tmp_path, registered=["jarvis", "steward"])
    monkeypatch.setattr(
        agent_scope,
        "resolvable_memory_profiles",
        lambda root: ({"jarvis"}, {"steward": "missing_profile"}),
    )
    with patch("skcapstone.doctor._skmemory_health_json", return_value=HEALTHY) as fake:
        checks = _check_memory_drift(home)
    assert [c.args[0] for c in fake.call_args_list] == ["jarvis"]
    detail = _by_name(checks)["memory:drift:scope"].detail
    assert "steward (missing_profile)" in detail
    assert "do not resolve" in detail


def test_dot_directories_are_never_agents_or_seats(tmp_path):
    """noroc2027 keeps a 227 MiB agents/.git; it must never be archived."""
    home = _stale_estate(tmp_path)
    for name in (".git", ".stversions"):
        (home / "agents" / name).mkdir()
        _age(home / "agents" / name, 90)
    report = agent_scope.stale_seats(home, days=14)
    assert [s.name for s in report.stale] == ["pi-old"]
    assert report.total_dirs == 4
