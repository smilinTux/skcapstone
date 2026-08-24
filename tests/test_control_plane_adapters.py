from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from starlette.testclient import TestClient

from skdashboard.control_plane_adapters import (
    SCHEMA_VERSION,
    SPECS,
    Reader,
    _bounded_run,
    _local_readers,
    _subprocess_read,
    aggregate_reader,
    project_estate,
)
from skdashboard.dashboard import create_app

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
READ_HEADERS = {
    "Authorization": "Bearer valid-read",
    "Origin": "http://10.0.0.139:7778",
}


def _read_app():
    return create_app(
        Path("/tmp/does-not-matter"),
        control_plane_authorizer=lambda bearer, capability, _target: (
            bearer == "valid-read" and capability == "skdashboard.read"
        ),
    )


def _all_readers(*, observed_at: str | None = None) -> dict:
    readers = {}
    for spec in SPECS:
        aggregate = {field: 1 for field in spec.fields}
        readers[spec.adapter_id] = aggregate_reader(
            aggregate,
            observed_at=observed_at or NOW.isoformat(),
        )
    return readers


def test_every_estate_population_has_bounded_typed_metadata() -> None:
    items = project_estate({}, now=NOW)

    assert len(items) == len(SPECS) == 16
    assert len({item["adapter_id"] for item in items}) == 16
    assert len({item["population"] for item in items}) == 16
    for item in items:
        assert item["schema_version"] == SCHEMA_VERSION
        assert item["owner"]
        assert item["query_budget"] == {"max_items": 1, "timeout_ms": 1_000}
        assert item["ttl_seconds"] == 60
        assert item["classification"] in {"internal", "confidential"}
        assert item["truth_state"] == "unavailable"
        assert item["aggregate"] is None
        assert item["watermark"]["value"] is None
        assert item["errors"][0]["code"] == "SOURCE_UNAVAILABLE"

    protected = next(item for item in items if item["adapter_id"] == "sklegal.global")
    assert protected["visibility"] == {
        "state": "policy_filtered",
        "authorization": "unknown",
        "reason": "Tenant and Matter policy was not evaluated at global scope",
    }
    assert "tenant_id" not in str(protected)
    assert "matter_id" not in str(protected)


def test_truth_states_fail_closed_and_are_sensitive_to_source_evidence() -> None:
    spec = SPECS[0]
    fields = {field: 1 for field in spec.fields}
    readers = _all_readers()
    readers[spec.adapter_id] = aggregate_reader(
        fields,
        expected=2,
        reporting=1,
        observed_at=NOW.isoformat(),
    )
    assert project_estate(readers, now=NOW)[0]["truth_state"] == "partial"

    readers[spec.adapter_id] = aggregate_reader(
        fields,
        observed_at=(NOW - timedelta(seconds=61)).isoformat(),
    )
    stale = project_estate(readers, now=NOW)[0]
    assert stale["truth_state"] == "stale"
    assert stale["age_seconds"] == 61

    readers[spec.adapter_id] = aggregate_reader(
        fields,
        observed_at=NOW.isoformat(),
        has_observations=False,
    )
    assert project_estate(readers, now=NOW)[0]["truth_state"] == "unknown"

    readers[spec.adapter_id] = Reader(failure="timeout")
    timeout = project_estate(readers, now=NOW)[0]
    assert timeout["truth_state"] == "unavailable"
    assert timeout["errors"][0]["code"] == "SOURCE_TIMEOUT"
    assert "secret" not in str(timeout)


def test_malformed_or_future_source_cannot_leak_or_render_current() -> None:
    spec = next(value for value in SPECS if value.adapter_id == "sklegal.global")
    raw = aggregate_reader(
        {**{field: 1 for field in spec.fields}, "matter_title": "protected"},
        observed_at=NOW.isoformat(),
    )
    malformed = project_estate({spec.adapter_id: raw}, now=NOW)
    result = next(item for item in malformed if item["adapter_id"] == spec.adapter_id)
    assert result["truth_state"] == "unavailable"
    assert result["errors"][0]["code"] == "SOURCE_MALFORMED"
    assert "protected" not in str(result)

    future = aggregate_reader(
        {field: 1 for field in SPECS[0].fields},
        observed_at=(NOW + timedelta(minutes=6)).isoformat(),
    )
    result = project_estate({SPECS[0].adapter_id: future}, now=NOW)[0]
    assert result["truth_state"] == "unavailable"
    assert result["errors"][0]["code"] == "SOURCE_MALFORMED"


def test_unreachable_unknown_and_unauthorized_remain_distinct() -> None:
    spec = SPECS[0]
    cases = (
        ("unreachable", "unreachable", "SOURCE_UNREACHABLE", "authorized"),
        ("unauthorized", "unknown", "SOURCE_UNAUTHORIZED", "denied"),
    )
    for failure, truth, code, authorization in cases:
        item = project_estate(
            {spec.adapter_id: Reader(failure=failure)}, now=NOW
        )[0]
        assert item["truth_state"] == truth
        assert item["errors"][0]["code"] == code
        assert item["visibility"]["authorization"] == authorization
        assert item["truth_state"] != "current"

    unavailable = project_estate(
        {spec.adapter_id: Reader(failure="unavailable")}, now=NOW
    )[0]
    assert unavailable["truth_state"] == "unavailable"
    assert unavailable["errors"][0]["code"] == "SOURCE_UNAVAILABLE"


def test_sklegal_success_stays_policy_filtered_without_matter_detail() -> None:
    spec = next(value for value in SPECS if value.adapter_id == "sklegal.global")
    reader = aggregate_reader(
        {field: 1 for field in spec.fields}, observed_at=NOW.isoformat()
    )
    item = next(
        value
        for value in project_estate({spec.adapter_id: reader}, now=NOW)
        if value["adapter_id"] == spec.adapter_id
    )
    assert item["truth_state"] == "current"
    assert item["visibility"]["state"] == "policy_filtered"
    assert item["visibility"]["authorization"] == "unknown"
    assert "tenant_id" not in str(item)
    assert "matter_id" not in str(item)


def test_default_readers_keep_populations_and_measurement_lanes_separate(tmp_path: Path) -> None:
    board = {
        "summary": {"total": 3, "open": 1, "in_progress": 1, "done": 1},
        "tasks": [{"status": "blocked"}],
        "agents": [{"state": "active"}, {"state": "idle"}],
    }
    cmdb = {
        "total": 2,
        "health": {"operational": 2},
        "evidence_health": {"fresh": 2, "stale": 0, "unknown": 0, "unreachable": 0},
        "last_successful_reconciliation": NOW.isoformat(),
    }
    fleet = {
        "summary": {"graded": 1, "skipped": 1, "error": 0, "warn": 0, "info": 0, "ok": 1},
        "errors": [],
    }

    def usage(_home, filters):
        lane = filters["lane"]
        total = 10 if lane == "harness_reported" else 20
        return {
            "generated_at": NOW.isoformat(),
            "summary": {"total": total, "cost_state": "unavailable"},
            "coverage": {
                "expected_nodes": 1,
                "reporting_nodes": 1,
                "fresh_collectors": 1,
                "delayed_collectors": 0,
                "stale_collectors": 0,
            },
            "collectors": [{"last_seen": NOW.isoformat(), "node_id": lane}],
            "observation_count": 1,
            "errors": [],
        }

    stats = Mock(agent_balances={"jarvis": 7}, active_agents=1)
    with patch("skdashboard.dashboard_itil.get_overview", return_value={
        "kpis": {"open_incidents": 0, "sev1": 0, "sev2": 0, "awaiting_cab": 0},
        "activity": [],
    }), patch("skdashboard.dashboard_cmdb.get_overview", return_value=cmdb), patch(
        "skdashboard.dashboard_fleet.get_drift", return_value=fleet
    ), patch("skdashboard.dashboard_skcounter.get_ai_usage", side_effect=usage), patch(
        "skcapstone.skjoule.JouleEngine.get_network_stats", return_value=stats
    ):
        local = _local_readers(tmp_path, board_data=board)
        readers = {
            adapter_id: reader if isinstance(reader, Reader) else Reader(payload=reader())
            for adapter_id, reader in local.items()
        }
        items = project_estate(readers, now=NOW)

    by_id = {item["adapter_id"]: item for item in items}
    assert by_id["skcoord.agent_presence"]["aggregate"] == {
        "total_agents": 2,
        "active_agents": 1,
    }
    assert by_id["skcapstone.fleet"]["truth_state"] == "partial"
    assert by_id["cmdb.configuration"]["aggregate"] == {
        "total": 2,
        "operational": 2,
        "degraded": 0,
        "other_status": 0,
        "fresh": 2,
        "stale": 0,
        "unknown": 0,
    }
    assert by_id["skcounter.harness"]["aggregate"]["tokens_total"] == 10
    assert by_id["skgateway.observed"]["aggregate"]["tokens_total"] == 20
    assert by_id["skjoule.wallet"]["aggregate"] == {"total_supply": 7, "active_agents": 1}


def test_overview_etag_ignores_delivery_clocks_but_changes_with_source() -> None:
    observed_at = datetime.now(timezone.utc).isoformat()
    readers = _all_readers(observed_at=observed_at)
    client = TestClient(_read_app())
    with patch("skdashboard.control_plane_adapters.default_readers", return_value=readers):
        first = client.get("/api/v1/overview", headers=READ_HEADERS)
        unchanged = client.get(
            "/api/v1/overview",
            headers={**READ_HEADERS, "If-None-Match": first.headers["etag"]},
        )
    assert first.status_code == 200
    assert unchanged.status_code == 304

    changed = _all_readers(observed_at=observed_at)
    changed[SPECS[0].adapter_id] = aggregate_reader(
        {field: 2 for field in SPECS[0].fields}, observed_at=observed_at
    )
    with patch("skdashboard.control_plane_adapters.default_readers", return_value=changed):
        response = client.get(
            "/api/v1/overview",
            headers={**READ_HEADERS, "If-None-Match": first.headers["etag"]},
        )
    assert response.status_code == 200
    assert response.headers["etag"] != first.headers["etag"]


def test_empty_usage_is_unknown_not_zero_current() -> None:
    spec = next(value for value in SPECS if value.adapter_id == "skcounter.harness")
    aggregate = {field: 0 for field in spec.fields}
    aggregate["cost_usd"] = None
    aggregate["cost_state"] = "unavailable"
    reader = aggregate_reader(aggregate, expected=0, reporting=0, has_observations=False)
    item = next(
        value
        for value in project_estate({spec.adapter_id: reader})
        if value["adapter_id"] == spec.adapter_id
    )
    assert item["truth_state"] == "unknown"
    assert item["truth_state"] != "current"
    assert item["aggregate"] is None


def test_query_timeout_returns_within_declared_budget() -> None:
    started = time.monotonic()
    try:
        _bounded_run(
            [sys.executable, "-c", "import time; time.sleep(2.25)"],
            timeout_ms=1_000,
            environment=os.environ.copy(),
        )
    except TimeoutError:
        pass
    else:
        raise AssertionError("subprocess did not time out")
    elapsed = time.monotonic() - started
    assert elapsed < 1.4

    item = project_estate(
        {SPECS[0].adapter_id: Reader(failure="timeout")}, now=NOW
    )[0]
    assert item["truth_state"] == "unavailable"
    assert item["errors"][0]["code"] == "SOURCE_TIMEOUT"


def test_empty_or_malformed_owner_folds_never_become_current_zero(tmp_path: Path) -> None:
    empty_fleet = {
        "summary": {"graded": 0, "skipped": 0, "error": 0, "warn": 0, "info": 0, "ok": 0},
        "errors": [],
    }
    with patch("skdashboard.dashboard_fleet.get_drift", return_value=empty_fleet):
        local = _local_readers(tmp_path, board_data={})["skcapstone.fleet"]
        items = project_estate(
            {"skcapstone.fleet": Reader(payload=local())}, now=NOW
        )
    fleet = next(item for item in items if item["adapter_id"] == "skcapstone.fleet")
    assert fleet["truth_state"] == "unknown"
    assert fleet["truth_state"] != "current"
    assert fleet["aggregate"] is None

    malformed_board = _local_readers(
        tmp_path,
        board_data={"summary": {"total": 0}, "tasks": [], "agents": []},
    )
    items = project_estate(malformed_board, now=NOW)
    board = next(item for item in items if item["adapter_id"] == "skcapstone.portfolio")
    assert board["truth_state"] == "unavailable"
    assert board["aggregate"] is None


def test_http_timeout_is_bounded_and_projection_clock_follows_source_clock() -> None:
    client = TestClient(_read_app())
    with patch(
        "skdashboard.control_plane_adapters.default_readers",
        return_value={SPECS[0].adapter_id: Reader(failure="timeout")},
    ):
        started = time.monotonic()
        response = client.get("/api/v1/overview", headers=READ_HEADERS)
        elapsed = time.monotonic() - started
    assert elapsed < 1.4
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["errors"][0]["code"] == "SOURCE_TIMEOUT"

    item = project_estate(
        {
            SPECS[0].adapter_id: aggregate_reader(
                {field: 1 for field in SPECS[0].fields}
            )
        }
    )[0]
    observed = datetime.fromisoformat(item["observed_at"].replace("Z", "+00:00"))
    projected = datetime.fromisoformat(item["projected_at"].replace("Z", "+00:00"))
    assert observed <= projected <= datetime.now(timezone.utc)


def test_project_path_hung_sources_do_not_delay_later_healthy_source(tmp_path: Path) -> None:
    source_specs = SPECS[:5]
    starts = {}

    def read(adapter_id, _home, _timeout_ms):
        starts[adapter_id] = time.monotonic()
        if adapter_id != source_specs[4].adapter_id:
            time.sleep(0.3)
            raise TimeoutError
        return aggregate_reader(
            {field: 1 for field in source_specs[4].fields},
            observed_at=NOW.isoformat(),
        )()

    readers = {
        spec.adapter_id: Reader(
            adapter_id=spec.adapter_id,
            home=tmp_path,
            timeout_ms=300,
        )
        for spec in source_specs
    }
    started = time.monotonic()
    with patch("skdashboard.control_plane_adapters._subprocess_read", side_effect=read):
        items = project_estate(readers, now=NOW)
    assert time.monotonic() - started < 0.8
    assert starts[source_specs[4].adapter_id] - started < 0.15
    assert [item["adapter_id"] for item in items] == [spec.adapter_id for spec in SPECS]
    assert items[4]["truth_state"] == "current"


def test_timeout_kills_descendant_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "descendant.pid"
    script = (
        "import pathlib, subprocess, sys, time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid)); "
        "time.sleep(30)"
    )
    try:
        _bounded_run(
            [sys.executable, "-c", script],
            timeout_ms=300,
            environment=os.environ.copy(),
        )
    except TimeoutError:
        pass
    else:
        raise AssertionError("process group did not time out")

    child_pid = int(pid_file.read_text())
    alive = True
    for _ in range(100):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            alive = False
            break
        time.sleep(0.01)
    if alive:
        os.kill(child_pid, signal.SIGKILL)
    assert not alive


def test_real_worker_returns_bounded_read_without_mutating_home(tmp_path: Path) -> None:
    before = list(tmp_path.rglob("*"))
    raw = _subprocess_read("skcapstone.portfolio", tmp_path, 1_000)
    after = list(tmp_path.rglob("*"))
    assert raw["schema_version"] == SCHEMA_VERSION
    assert set(raw["aggregate"]) == {"total", "open", "in_progress", "done"}
    assert len(str(raw).encode()) < 8_192
    assert after == before


def test_production_worker_statuses_preserve_safe_failure_classes(tmp_path: Path) -> None:
    spec = SPECS[0]
    cases = (
        (4, "unavailable", "SOURCE_MALFORMED", "authorized"),
        (5, "unknown", "SOURCE_UNAUTHORIZED", "denied"),
        (6, "unreachable", "SOURCE_UNREACHABLE", "authorized"),
        (7, "unavailable", "SOURCE_MALFORMED", "authorized"),
    )
    for returncode, truth, code, authorization in cases:
        completed = subprocess.CompletedProcess([], returncode, "", "private detail")
        reader = Reader(adapter_id=spec.adapter_id, home=tmp_path, timeout_ms=1_000)
        with patch("skdashboard.control_plane_adapters._bounded_run", return_value=completed):
            item = project_estate({spec.adapter_id: reader}, now=NOW)[0]
        assert item["truth_state"] == truth
        assert item["errors"][0]["code"] == code
        assert item["visibility"]["authorization"] == authorization
        assert "private detail" not in str(item)


def test_oversize_or_bad_worker_json_is_malformed_not_unavailable(tmp_path: Path) -> None:
    spec = SPECS[0]
    reader = Reader(adapter_id=spec.adapter_id, home=tmp_path, timeout_ms=1_000)
    for stdout in ("x" * 8_193, "{"):
        completed = subprocess.CompletedProcess([], 0, stdout, "")
        with patch("skdashboard.control_plane_adapters._bounded_run", return_value=completed):
            item = project_estate({spec.adapter_id: reader}, now=NOW)[0]
        assert item["truth_state"] == "unavailable"
        assert item["errors"][0]["code"] == "SOURCE_MALFORMED"
