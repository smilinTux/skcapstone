"""Regression tests for fail-closed cross-host fleet report health."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace

SCRIPT = Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"
HOSTS = ("chiap01", "chiap02", "chiap03", "chiap04", "chiap08")


def _load_health(live: Path):
    """Load report health without executing the scheduler script."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "live_report_health"
    )
    namespace = {
        "json": json,
        "os": os,
        "Path": Path,
        "time": SimpleNamespace(time=lambda: 10_000.0),
        "LIVE": str(live),
        "LIVE_FRESH": 1_800,
        "LIVE_TIMER_CYCLE": 360,
        "ROTATION_HOSTS": HOSTS,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SCRIPT), "exec"), namespace)
    return namespace["live_report_health"]


def _report(live: Path, host: str, age: int, cards=()) -> None:
    """Write one serialized host report."""
    (live / f"{host}.json").write_text(
        json.dumps({"host": host, "ts": 10_000 - age, "cards": list(cards)}),
        encoding="utf-8",
    )


def test_four_of_five_exposes_missing_host_and_age_without_authoritative_absence(tmp_path) -> None:
    """A reporting=4 known=5 need>=3 view names faults but cannot reap."""
    for host, age in zip(HOSTS[:4], (10, 20, 30, 600), strict=True):
        _report(tmp_path, host, age)

    health = _load_health(tmp_path)(now=10_000.0)
    reporting = len(health["reporting"])
    known = len(health["expected"])
    need = known // 2 + 1

    assert (reporting, known, need) == (4, 5, 3)
    assert not health["authoritative"]
    assert health["expected"] - health["reporting"] == {"chiap08"}
    assert {fault["host"]: fault for fault in health["faults"]} == {
        "chiap04": {
            "host": "chiap04",
            "reason": "transport_delayed",
            "age_seconds": 600,
            "detail": "",
        },
        "chiap08": {
            "host": "chiap08",
            "reason": "missing",
            "age_seconds": None,
            "detail": "[Errno 2] No such file or directory: '%s/chiap08.json'" % tmp_path,
        },
    }


def test_live_card_is_retained_until_all_five_reports_are_visible(tmp_path) -> None:
    """The oldest authoritative view includes a live remote worker."""
    for host in HOSTS:
        _report(tmp_path, host, 120, cards=("livecard",) if host == "chiap08" else ())

    health = _load_health(tmp_path)(now=10_000.0)

    assert health["reporting"] == set(HOSTS)
    assert health["faults"] == []
    assert health["running"] == {"livecard"}
    assert health["oldest"] == 9_880


def test_stale_and_identity_mismatched_reports_fail_closed(tmp_path) -> None:
    """Stale or misaddressed bytes cannot satisfy known-host visibility."""
    for host in HOSTS:
        _report(tmp_path, host, 10)
    _report(tmp_path, "chiap04", 1_801)
    (tmp_path / "chiap08.json").write_text(
        json.dumps({"host": "chiap04", "ts": 9_990, "cards": []}), encoding="utf-8"
    )

    health = _load_health(tmp_path)(now=10_000.0)

    assert health["reporting"] == {"chiap01", "chiap02", "chiap03"}
    faults = {fault["host"]: fault for fault in health["faults"]}
    assert faults["chiap04"]["reason"] == "stale"
    assert faults["chiap04"]["age_seconds"] == 1_801
    assert faults["chiap08"]["reason"] == "invalid"
    assert "report host=chiap04" in faults["chiap08"]["detail"]


def test_reaper_logs_distinct_shortage_and_visibility_loss() -> None:
    """Operator output distinguishes quorum loss from a 4/5 transport fault."""
    source = SCRIPT.read_text(encoding="utf-8")

    assert "quorum_shortage reporting=%d known=%d need>=%d" in source
    assert "known_host_visibility_loss reporting=%d known=%d" in source
    assert "if not oldest or nhosts < REAP_QUORUM:" in source
    assert 'if not report_health.get("authoritative", nhosts >= known):' in source
    assert "missing=%s; reaped nothing" in source
    assert "FLEET_LIVE_FAULT|%s|host=%s|reason=%s|age_seconds=%s" in source
