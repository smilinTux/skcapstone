"""skos adapter: conformant to the contract, health mapped correctly.

Every test here redirects SK_WATCHDOG_DIR and SKCAPSTONE_HOME at tmp_path, so the
watchdog probes read a throwaway tree and never the real fleet's digest store.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from skcapstone.operator_seat import adapter, brief, loop
from skcapstone.operator_seat import skos_adapter as ad


class _FakeProc:
    returncode = 0
    stdout = ""
    stderr = ""


@pytest.fixture(autouse=True)
def _isolated_watchdog_home(tmp_path, monkeypatch):
    """Point the watchdog probes at an empty tmp tree and stub the skos CLI call,
    so nothing here reads or writes real fleet state."""
    monkeypatch.setenv("SK_WATCHDOG_DIR", str(tmp_path / "watchdog"))
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path / "skcapstone"))
    monkeypatch.setattr("subprocess.run", lambda *a, **k: _FakeProc(), raising=False)
    return tmp_path


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_digest(*, age_hours: float = 1.0, events: list[dict] | None = None) -> None:
    """Publish a digest at the path the adapter reads, aged by `age_hours`."""
    until = datetime.now(timezone.utc) - timedelta(hours=age_hours)
    digest = {
        "date": until.strftime("%Y-%m-%d"),
        "window": {"since": _iso(until - timedelta(hours=24)), "until": _iso(until)},
        "headline": "test digest",
        "problems": [],
        "notable": list(events or []),
        "info_counts": {},
        "per_source": {},
    }
    path = ad._digest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(digest), encoding="utf-8")


def _grading_gap(*, budget_exhausted, skipped: int = 3) -> dict:
    """A GradingGap event exactly as skos.watchdog.adapters.grading emits it."""
    meta = {"skipped": skipped, "rubric_ref": "lumina-replies@v1"}
    if budget_exhausted is not None:
        meta["budget_exhausted"] = budget_exhausted
    return {
        "ts": "2026-08-16T06:00:00Z",
        "source": "grading",
        "kind": "GradingGap",
        "object": "lumina-replies",
        "severity": "notable",
        "summary": f"{skipped} reply grade(s) skipped this run; no score was fabricated.",
        "link": {"uri": "skworld://skos/watchdog/grading/gap", "http": ""},
        "ref": "grading:gap:2026-08-16",
        "meta": meta,
    }


def _status(obs: dict, condition_type: str) -> str:
    return next(c["status"] for c in obs["conditions"] if c["type"] == condition_type)


def test_skos_explain_is_contract_conformant():
    assert adapter.validate_explain(ad.skos_explain()) == []


def test_skos_observe_is_contract_conformant():
    obs = ad.observe()
    assert adapter.validate_observe(obs) == []


def test_skos_observe_emits_every_declared_condition():
    obs = ad.skos_observe(probe=dict)
    assert [c["type"] for c in obs["conditions"]] == ad.CONDITIONS


def test_skos_healthy_all_true():
    obs = ad.skos_observe(
        probe=lambda: {
            "upstream_serving": True,
            "pool_healthy": True,
            "scheduler_alive": True,
            "gtd_draining": True,
            "digest_fresh": True,
            "grading_backlog": False,  # a problem type: healthy is False
        }
    )
    by_type = {c["type"]: c["status"] for c in obs["conditions"]}
    assert by_type["SchedulerAlive"] == "True"
    assert by_type["GtdSinkDraining"] == "True"
    assert by_type["WatchdogDigestFresh"] == "True"
    assert by_type["GradingBacklog"] == "False"


def test_skos_default_probe_failure_is_unknown(monkeypatch):
    def _boom(*a, **k):
        raise OSError("down")

    import skos.operator_probe as sop

    monkeypatch.setattr(sop, "observe", _boom)
    monkeypatch.setattr("urllib.request.urlopen", _boom, raising=False)
    st = ad._default_probe()
    # Missing reachability evidence is Unknown, never fabricated healthy.
    assert st["scheduler_alive"] is None
    assert st["gtd_draining"] is None
    # ...the watchdog halves fail to UNKNOWN, never to healthy: with no digest
    # published there is nothing to call fresh.
    assert st["digest_fresh"] is None
    assert st["grading_backlog"] is None


def test_skos_default_probe_delegates_to_skos_operator_probe(monkeypatch):
    # Card 504d0046: scheduler_alive/gtd_draining must be ONE real signal with
    # two callers (this in-process seat, the out-of-process `skos operator
    # observe` cli), not a second, independently-drifting signal reader. The
    # old default probe shelled out to `skos scheduler status` (a subcommand
    # that does not exist, always reading confidently WRONG) and hardcoded
    # gtd_draining to None (never implemented, though the real signal was
    # available the whole time). Assert the delegation actually happens.
    import skos.operator_probe as sop

    monkeypatch.setattr(
        sop,
        "observe",
        lambda: {
            "conditions": [
                {"type": "SchedulerAlive", "status": "False", "object": "skscheduler"},
                {"type": "GtdSinkDraining", "status": "True", "object": "gtd-sink"},
            ]
        },
    )
    st = ad._default_probe()
    assert st["scheduler_alive"] is False
    assert st["gtd_draining"] is True


# --- WatchdogDigestFresh -----------------------------------------------------


def test_fresh_digest_does_not_fire():
    _write_digest(age_hours=2)
    st = ad._default_probe()
    assert st["digest_fresh"] is True
    assert _status(ad.skos_observe(probe=lambda: st), "WatchdogDigestFresh") == "True"


def test_stale_digest_fires():
    """27h of silence is the narrator going quiet: health type, so False = firing."""
    _write_digest(age_hours=27)
    st = ad._default_probe()
    assert st["digest_fresh"] is False
    obs = ad.skos_observe(probe=lambda: st)
    assert _status(obs, "WatchdogDigestFresh") == "False"
    the_brief = brief.build_brief({"skos": obs["conditions"]}, set(loop.PROBLEM_WHEN_TRUE))
    assert "WatchdogDigestFresh" in {entry["type"] for entry in the_brief["firing"]}


def test_digest_boundary_is_26h():
    assert ad._digest_fresh(26 * 3600) is True
    assert ad._digest_fresh(26 * 3600 + 1) is False


def test_missing_digest_is_unknown_not_fresh():
    st = ad._default_probe()
    assert st["digest_fresh"] is None
    assert _status(ad.skos_observe(probe=lambda: st), "WatchdogDigestFresh") == "Unknown"


def test_unreadable_digest_is_unknown():
    path = ad._digest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    st = ad._default_probe()
    assert st["digest_fresh"] is None
    assert st["grading_backlog"] is None


def test_digest_age_prefers_window_over_mtime():
    """A stale digest re-published today must not read as fresh: the age comes
    from the window the run covered, not from when the bytes were written."""
    _write_digest(age_hours=40)  # written just now, covering a window 40h old
    assert ad._digest_age_s(ad._read_digest()) > 26 * 3600


def test_digest_age_reads_the_real_wire_window_shape():
    """The regression that made the window preference dead code in production.

    `skos.watchdog.port.Window` names its attribute `until`, but
    `Window.to_dict()` serialises `{"from": since, "to": until}`, and every
    digest.json on disk carries `{"from", "to"}`. The adapter read only
    `until`, so on a REAL digest it never matched and fell through to mtime,
    silently defeating the staleness check it exists to perform.

    The bug survived because the fixture above builds its own window using the
    attribute names, a shape the publisher never emits. So this test writes the
    wire shape deliberately: a digest published seconds ago, covering a window
    that ended 40h back, must read as stale.
    """
    until = datetime.now(timezone.utc) - timedelta(hours=40)
    path = ad._digest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "date": until.strftime("%Y-%m-%d"),
                # exactly what Window.to_dict() produces
                "window": {"from": _iso(until - timedelta(hours=24)), "to": _iso(until)},
                "headline": "test digest",
                "problems": [],
                "notable": [],
                "info_counts": {},
                "per_source": {},
            }
        ),
        encoding="utf-8",
    )
    age = ad._digest_age_s(ad._read_digest())
    assert age is not None
    assert age > 26 * 3600, (
        "age fell back to mtime instead of reading window['to']; the digest was "
        "written just now but covers a window that ended 40h ago"
    )


def test_digest_without_window_falls_back_to_mtime():
    path = ad._digest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": "2026-08-16", "notable": []}), encoding="utf-8")
    age = ad._digest_age_s(ad._read_digest())
    assert age is not None and age < 3600


# --- GradingBacklog ----------------------------------------------------------


def test_grading_backlog_fires_when_budget_exhausted():
    _write_digest(events=[_grading_gap(budget_exhausted=True)])
    st = ad._default_probe()
    assert st["grading_backlog"] is True
    obs = ad.skos_observe(probe=lambda: st)
    assert _status(obs, "GradingBacklog") == "True"
    # Problem type: True is what fires. Read with the loop's own polarity set.
    the_brief = brief.build_brief({"skos": obs["conditions"]}, set(loop.PROBLEM_WHEN_TRUE))
    assert "GradingBacklog" in {entry["type"] for entry in the_brief["firing"]}


def test_grading_gap_without_budget_exhausted_does_not_fire():
    """THE negative case: an unreachable grader or an unparseable reply emits the
    same GradingGap kind, and that is grader AVAILABILITY, not backlog."""
    _write_digest(events=[_grading_gap(budget_exhausted=False)])
    st = ad._default_probe()
    assert st["grading_backlog"] is False
    obs = ad.skos_observe(probe=lambda: st)
    assert _status(obs, "GradingBacklog") == "False"
    the_brief = brief.build_brief({"skos": obs["conditions"]}, set(loop.PROBLEM_WHEN_TRUE))
    assert "GradingBacklog" not in {entry["type"] for entry in the_brief["firing"]}
    assert "GradingBacklog" not in {entry["type"] for entry in the_brief["stale"]}


def test_grading_gap_with_absent_budget_flag_does_not_fire():
    _write_digest(events=[_grading_gap(budget_exhausted=None)])
    assert ad._default_probe()["grading_backlog"] is False


def test_grading_gap_with_truthy_non_bool_flag_does_not_fire():
    """Narrow by construction: only a real boolean true means the budget ran out."""
    _write_digest(events=[_grading_gap(budget_exhausted="false")])
    assert ad._default_probe()["grading_backlog"] is False


def test_source_unavailable_event_does_not_fire_backlog():
    """The grader's OTHER degrade path (a per-channel SourceUnavailable) is
    availability too and shares no ground with a backlog."""
    unavailable = {
        "kind": "SourceUnavailable",
        "source": "grading.skchat",
        "severity": "problem",
        "meta": {"error": "skgateway timeout", "budget_exhausted": True},
    }
    _write_digest(events=[unavailable])
    assert ad._default_probe()["grading_backlog"] is False


def test_grading_backlog_found_in_problems_list_too():
    """Severity routing is the digest assembler's business, not ours: scan both
    lists so a re-graded severity never silently hides the signal."""
    _write_digest(age_hours=1)
    path = ad._digest_path()
    digest = json.loads(path.read_text(encoding="utf-8"))
    digest["problems"] = [_grading_gap(budget_exhausted=True)]
    path.write_text(json.dumps(digest), encoding="utf-8")
    assert ad._default_probe()["grading_backlog"] is True


def test_no_grading_gap_at_all_is_quiet():
    _write_digest(events=[])
    assert ad._default_probe()["grading_backlog"] is False


# --- read-only discipline ----------------------------------------------------


def test_observing_creates_nothing(tmp_path):
    """A probe that creates the store it looks at manufactures its own state.
    Nothing under the watchdog root may exist after a full observe pass."""
    root = tmp_path / "watchdog"
    assert not root.exists()
    ad._default_probe()
    ad.observe()
    assert not root.exists()
    assert not (tmp_path / "skcapstone").exists()


def test_unresolvable_watchdog_root_is_unknown(monkeypatch):
    monkeypatch.setattr(ad, "_watchdog_home", lambda: None)
    assert ad._digest_path() is None
    st = ad._default_probe()
    assert st["digest_fresh"] is None
    assert st["grading_backlog"] is None


def test_watchdog_home_precedence(tmp_path, monkeypatch):
    """Mirrors skos.watchdog.cursor.watchdog_home()'s precedence exactly."""
    monkeypatch.setenv("SK_WATCHDOG_DIR", str(tmp_path / "explicit"))
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path / "home"))
    assert ad._watchdog_home() == tmp_path / "explicit"
    monkeypatch.delenv("SK_WATCHDOG_DIR")
    assert ad._watchdog_home() == tmp_path / "home" / "watchdog"


# --- polarity ----------------------------------------------------------------


def test_grading_backlog_is_declared_problem_when_true():
    assert "GradingBacklog" in ad.PROBLEM_WHEN_TRUE
    assert "GradingBacklog" in loop.PROBLEM_WHEN_TRUE
    # Health types must NOT be in it, or they would fire inverted.
    assert ad.PROBLEM_WHEN_TRUE.isdisjoint({"SchedulerAlive", "WatchdogDigestFresh"})


def test_loop_problem_types_still_carry_the_fleet_set():
    from skcapstone.operator_seat import fleet_adapter

    assert set(fleet_adapter.PROBLEM_WHEN_TRUE) <= set(loop.PROBLEM_WHEN_TRUE)
