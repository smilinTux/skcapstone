"""ATLAS Soak: the Phase 3 dual-read recorder + gate report (card 90b5b277).

Everything here either exercises the pure reduction/aggregation functions
directly (no I/O) or drives `record`/`report` against a throwaway `tmp_path`
fleet tree -- never the live `~/.skcapstone/fleet`.
"""

from __future__ import annotations

import json

from skcapstone.fleet import store
from skcapstone.fleet.paths import FleetPaths
from skcapstone.operator_seat import soak

PWT = frozenset()


# ── capture(): reducing one eyes.assess() pass ──────────────────────────────


def _ok_lane(conds):
    return {
        "state": "ok",
        "conditions": [{"type": t, "status": s} for t, s in conds],
        "detail": "",
    }


def _dead_lane(state, detail=""):
    return {"state": state, "conditions": [], "detail": detail}


def _assessment(*apps):
    return {
        "schema": "skoperator.eyes/v1",
        "at": "2026-08-24T00:00:00Z",
        "frozen": False,
        "apps": list(apps),
    }


def test_capture_classifies_no_endpoint_registered():
    assessment = _assessment(
        {
            "name": "cmdb",
            "cli_lane": _ok_lane([("A", "True")]),
            "seat_lane": _ok_lane([("A", "True")]),
        }
    )
    sample = soak.capture(assessment, endpoints={"cmdb": None})
    (app,) = sample["apps"]
    assert app["endpoint"] == {"flavor": "no-endpoint-registered", "conditions": {}}
    assert app["old"] == {"flavor": "ok", "conditions": {"A": "True"}}


def test_capture_classifies_endpoint_unreachable_when_pending():
    assessment = _assessment(
        {
            "name": "skgateway",
            "cli_lane": _dead_lane("endpoint-pending", "endpoint=... declared"),
            "seat_lane": _ok_lane([("UpstreamServing", "False")]),
        }
    )
    sample = soak.capture(
        assessment, endpoints={"skgateway": "https://100.64.0.5:9392/operator/v1"}
    )
    (app,) = sample["apps"]
    assert app["endpoint"]["flavor"] == "endpoint-unreachable"
    assert app["endpoint"]["raw_state"] == "endpoint-pending"
    assert app["endpoint"]["conditions"] == {}


def test_capture_classifies_endpoint_ok_reading():
    assessment = _assessment(
        {
            "name": "cmdb",
            "cli_lane": _ok_lane([("A", "True"), ("B", "Unknown")]),
            "seat_lane": _ok_lane([("A", "True"), ("B", "False")]),
        }
    )
    sample = soak.capture(assessment, endpoints={"cmdb": "https://node/operator/v1"})
    (app,) = sample["apps"]
    assert app["endpoint"] == {"flavor": "ok", "conditions": {"A": "True", "B": "Unknown"}}
    assert app["old"] == {"flavor": "ok", "conditions": {"A": "True", "B": "False"}}


def test_capture_old_lane_unreachable_when_seat_not_ok():
    assessment = _assessment(
        {
            "name": "skdashboard",
            "cli_lane": _dead_lane("no-cli"),
            "seat_lane": _dead_lane("no-adapter", "n/a"),
        }
    )
    sample = soak.capture(assessment, endpoints={"skdashboard": None})
    (app,) = sample["apps"]
    assert app["old"]["flavor"] == "unreachable"
    assert app["old"]["raw_state"] == "no-adapter"


def test_capture_preserves_pass_timestamp_and_frozen_flag():
    assessment = {
        "schema": "skoperator.eyes/v1",
        "at": "2026-08-01T12:00:00Z",
        "frozen": True,
        "apps": [],
    }
    sample = soak.capture(assessment, endpoints={})
    assert sample["at"] == "2026-08-01T12:00:00Z"
    assert sample["frozen"] is True
    assert sample["schema"] == soak.SCHEMA


# ── record(): the one write this module ever does ──────────────────────────


def _tmp_fleet(tmp_path, *, endpoint=None, contract_version=1):
    paths = FleetPaths(root=tmp_path / "fleet")
    writer = store.Writer(role="operator", node="cli", identity="test")
    spec = {"name": "appx", "cli": "appx-cli operator", "conditions": ["Ready"]}
    if endpoint is not None:
        spec.update(
            {
                "contractVersion": contract_version,
                "endpoint": endpoint,
                "node": "node-x",
                "transport": "http",
            }
        )
    store.write_spec(paths, "operatorapp", "appx", spec, writer=writer)
    return paths


def _fake_assess(no_endpoint=True):
    def fn(paths, *, now_iso, **kwargs):
        conds = [("Ready", "True")]
        return {
            "schema": "skoperator.eyes/v1",
            "at": now_iso,
            "frozen": False,
            "apps": [
                {
                    "name": "appx",
                    "cli_lane": _dead_lane("no-cli") if no_endpoint else _ok_lane(conds),
                    "seat_lane": _ok_lane(conds),
                }
            ],
        }

    return fn


def test_record_appends_one_sample_and_returns_path(tmp_path):
    paths = _tmp_fleet(tmp_path)
    result = soak.record(
        paths, node="node-a", assess_fn=_fake_assess(), now_iso="2026-08-24T01:00:00Z"
    )
    assert result["path"].exists()
    lines = result["path"].read_text().splitlines()
    assert len(lines) == 1
    recorded = json.loads(lines[0])
    assert recorded == result["sample"]
    (app,) = recorded["apps"]
    assert app["endpoint"]["flavor"] == "no-endpoint-registered"


def test_record_lives_under_atlas_soak_and_partitions_by_node_and_day(tmp_path):
    paths = _tmp_fleet(tmp_path)
    r1 = soak.record(
        paths, node="node-a", assess_fn=_fake_assess(), now_iso="2026-08-24T01:00:00Z"
    )
    r2 = soak.record(
        paths, node="node-b", assess_fn=_fake_assess(), now_iso="2026-08-24T02:00:00Z"
    )
    r3 = soak.record(
        paths, node="node-a", assess_fn=_fake_assess(), now_iso="2026-08-25T01:00:00Z"
    )
    assert r1["path"].parent == soak.soak_dir(paths)
    assert r1["path"] != r2["path"], "different nodes must not share a file"
    assert r1["path"] != r3["path"], "different days must not share a file"
    # same node+day appends to the same file
    r1b = soak.record(
        paths, node="node-a", assess_fn=_fake_assess(), now_iso="2026-08-24T01:30:00Z"
    )
    assert r1b["path"] == r1["path"]
    assert len(r1["path"].read_text().splitlines()) == 2


def test_record_never_writes_outside_its_own_soak_dir(tmp_path):
    paths = _tmp_fleet(tmp_path)
    before = sorted(
        p for p in (tmp_path / "fleet").rglob("*") if p.is_file() and "atlas/soak" not in str(p)
    )
    before_bytes = {p: p.read_bytes() for p in before}
    soak.record(paths, node="node-a", assess_fn=_fake_assess(), now_iso="2026-08-24T01:00:00Z")
    after = sorted(
        p for p in (tmp_path / "fleet").rglob("*") if p.is_file() and "atlas/soak" not in str(p)
    )
    assert before == after
    assert all(
        p.read_bytes() == before_bytes[p] for p in after
    ), "record() must be read-only outside its own dir"


def test_record_works_while_frozen_and_never_touches_the_freeze_file(tmp_path):
    paths = _tmp_fleet(tmp_path)
    human = store.Writer(role="operator", node="cli", identity="chef")
    store.set_frozen(paths, True, writer=human, reason="drill")
    frozen_bytes = paths.freeze_path().read_bytes()
    result = soak.record(
        paths, node="node-a", assess_fn=_fake_assess(), now_iso="2026-08-24T01:00:00Z"
    )
    assert (
        result["sample"]["frozen"] is False
    )  # our fake assess reports unfrozen; record just carries it through
    assert paths.freeze_path().read_bytes() == frozen_bytes


def test_record_never_writes_to_operatorapp_objects(tmp_path):
    paths = _tmp_fleet(tmp_path)
    obj_dir = paths.objects / "operatorapp"
    before = {p: p.read_bytes() for p in obj_dir.glob("*.json")}
    soak.record(paths, node="node-a", assess_fn=_fake_assess(), now_iso="2026-08-24T01:00:00Z")
    after = {p: p.read_bytes() for p in obj_dir.glob("*.json")}
    assert before == after, "soak must never register/modify an Operatorapp"


def test_record_prunes_files_older_than_retention(tmp_path):
    paths = _tmp_fleet(tmp_path)
    old = soak.record(
        paths, node="node-a", assess_fn=_fake_assess(), now_iso="2026-01-01T00:00:00Z"
    )
    assert old["path"].exists()
    # a later pass, far enough that the old file falls outside the default retention window
    soak.record(
        paths,
        node="node-a",
        assess_fn=_fake_assess(),
        now_iso="2026-08-24T00:00:00Z",
        retention_days=21,
    )
    assert not old["path"].exists(), "stale sample file must be pruned"


def test_record_endpoint_registered_reflects_live_spec(tmp_path):
    paths = _tmp_fleet(tmp_path, endpoint="https://node-x/operator/v1", contract_version=2)
    result = soak.record(
        paths,
        node="node-a",
        assess_fn=_fake_assess(no_endpoint=False),
        now_iso="2026-08-24T01:00:00Z",
    )
    (app,) = result["sample"]["apps"]
    assert app["endpoint"]["flavor"] == "ok"
    assert app["endpoint"]["conditions"] == {"Ready": "True"}


# ── prune() ──────────────────────────────────────────────────────────────────


def test_prune_on_missing_dir_is_a_noop(tmp_path):
    paths = FleetPaths(root=tmp_path / "fleet")
    assert soak.prune(paths) == []


def test_prune_ignores_malformed_filenames(tmp_path):
    paths = FleetPaths(root=tmp_path / "fleet")
    d = soak.soak_dir(paths)
    d.mkdir(parents=True)
    junk = d / "not-a-soak-file.jsonl"
    junk.write_text("{}\n")
    from datetime import datetime, timezone

    soak.prune(paths, retention_days=1, now=datetime(2026, 8, 24, tzinfo=timezone.utc))
    assert junk.exists()


# ── report(): the two gate metrics ──────────────────────────────────────────


def _write_sample(paths, *, node, day_iso, apps):
    from datetime import datetime

    when = datetime.strptime(day_iso, "%Y-%m-%dT%H:%M:%SZ")
    path = soak.soak_dir(paths) / f"{node}-{when.strftime('%Y-%m-%d')}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    sample = {"schema": soak.SCHEMA, "at": day_iso, "frozen": False, "apps": apps}
    with path.open("a") as f:
        f.write(json.dumps(sample) + "\n")


def _app_sample(name, endpoint_flavor, endpoint_conds, old_flavor, old_conds):
    return {
        "name": name,
        "endpoint": {"flavor": endpoint_flavor, "conditions": endpoint_conds},
        "old": {"flavor": old_flavor, "conditions": old_conds},
    }


def test_report_with_no_samples_is_nothing_to_compare(tmp_path):
    paths = FleetPaths(root=tmp_path / "fleet")
    rep = soak.report(paths)
    assert rep["apps"] == []
    assert rep["sample_passes"] == 0
    text = soak.render(rep)
    assert "No soak samples recorded yet" in text


def test_report_all_no_endpoint_is_nothing_to_compare_per_app(tmp_path):
    paths = FleetPaths(root=tmp_path / "fleet")
    _write_sample(
        paths,
        node="node-a",
        day_iso="2026-08-24T00:00:00Z",
        apps=[_app_sample("cmdb", "no-endpoint-registered", {}, "ok", {"A": "True"})],
    )
    rep = soak.report(
        paths,
        now=__import__("datetime").datetime(
            2026, 8, 24, tzinfo=__import__("datetime").timezone.utc
        ),
    )
    (app,) = rep["apps"]
    assert app["verdict"] == "NO-ENDPOINT"
    assert app["lane_conflicts"] == 0
    assert app["unknown_regressions"] == 0
    assert app["comparable_samples"] == 0
    text = soak.render(rep)
    assert "READY TO DEMOTE (0): none" in text


def test_report_counts_lane_conflict_only_when_both_lanes_are_ok(tmp_path):
    paths = FleetPaths(root=tmp_path / "fleet")
    _write_sample(
        paths,
        node="node-a",
        day_iso="2026-08-24T00:00:00Z",
        apps=[
            _app_sample("skchat", "ok", {"AuthEnforced": "True"}, "ok", {"AuthEnforced": "False"})
        ],
    )
    rep = soak.report(paths)
    (app,) = rep["apps"]
    assert app["lane_conflicts"] == 1
    assert "AuthEnforced" in app["conflict_examples"][0]
    assert app["verdict"] == "BLOCKED"


def test_report_does_not_count_conflict_when_endpoint_lane_not_ok(tmp_path):
    paths = FleetPaths(root=tmp_path / "fleet")
    _write_sample(
        paths,
        node="node-a",
        day_iso="2026-08-24T00:00:00Z",
        apps=[
            _app_sample("skgateway", "endpoint-unreachable", {}, "ok", {"UpstreamServing": "True"})
        ],
    )
    rep = soak.report(paths)
    (app,) = rep["apps"]
    assert app["comparable_samples"] == 0
    assert app["lane_conflicts"] == 0
    assert app["verdict"] == "PENDING"


def test_report_counts_unknown_regression_only_in_the_dangerous_direction(tmp_path):
    paths = FleetPaths(root=tmp_path / "fleet")
    _write_sample(
        paths,
        node="node-a",
        day_iso="2026-08-24T00:00:00Z",
        apps=[
            _app_sample(
                "skmemory",
                "ok",
                {"EmbedServing": "Unknown", "ReconcileFresh": "True"},
                "ok",
                {"EmbedServing": "True", "ReconcileFresh": "Unknown"},
            )
        ],
    )
    rep = soak.report(paths)
    (app,) = rep["apps"]
    # EmbedServing: old=True -> endpoint=Unknown is the dangerous direction: counts.
    # ReconcileFresh: old=Unknown -> endpoint=True is signal GAINED, not lost: does not count.
    assert app["unknown_regressions"] == 1
    assert "EmbedServing" in app["regression_examples"][0]
    assert not any("ReconcileFresh" in ex for ex in app["regression_examples"])
    # Both differ in status, so both are lane conflicts (a superset of the regression set).
    assert app["lane_conflicts"] == 2


def test_report_verdict_ready_requires_span_and_sample_floor(tmp_path):
    paths = FleetPaths(root=tmp_path / "fleet")
    for day in range(1, 9):  # 8 days of clean samples: 2026-08-01 .. 2026-08-08
        _write_sample(
            paths,
            node="node-a",
            day_iso=f"2026-08-{day:02d}T00:00:00Z",
            apps=[_app_sample("cmdb", "ok", {"A": "True"}, "ok", {"A": "True"})],
        )
    from datetime import datetime, timezone

    rep = soak.report(paths, window_days=30, now=datetime(2026, 8, 9, tzinfo=timezone.utc))
    (app,) = rep["apps"]
    assert app["lane_conflicts"] == 0
    assert app["unknown_regressions"] == 0
    assert app["span_days"] >= 7
    assert app["comparable_samples"] >= 7
    assert app["verdict"] == "READY"


def test_report_verdict_soaking_when_clean_but_under_the_floor(tmp_path):
    paths = FleetPaths(root=tmp_path / "fleet")
    _write_sample(
        paths,
        node="node-a",
        day_iso="2026-08-24T00:00:00Z",
        apps=[_app_sample("cmdb", "ok", {"A": "True"}, "ok", {"A": "True"})],
    )
    rep = soak.report(paths)
    (app,) = rep["apps"]
    assert app["verdict"] == "SOAKING"


def test_report_skips_malformed_lines_without_crashing(tmp_path):
    paths = FleetPaths(root=tmp_path / "fleet")
    d = soak.soak_dir(paths)
    d.mkdir(parents=True)
    p = d / "node-a-2026-08-24.jsonl"
    p.write_text(
        "not json\n" + json.dumps({"at": "2026-08-24T00:00:00Z", "apps": []}) + "\n{broken\n"
    )
    rep = soak.report(paths)
    assert rep["sample_passes"] == 1


def test_report_ignores_samples_outside_the_window(tmp_path):
    paths = FleetPaths(root=tmp_path / "fleet")
    _write_sample(
        paths,
        node="node-a",
        day_iso="2020-01-01T00:00:00Z",
        apps=[_app_sample("cmdb", "ok", {"A": "True"}, "ok", {"A": "False"})],
    )
    from datetime import datetime, timezone

    rep = soak.report(paths, window_days=7, now=datetime(2026, 8, 24, tzinfo=timezone.utc))
    assert rep["apps"] == []
    assert rep["sample_passes"] == 0


# ── render() ─────────────────────────────────────────────────────────────────


def test_render_lists_ready_and_blocked_apps():
    rep = {
        "at": "2026-08-24T00:00:00Z",
        "window_days": 21,
        "min_span_days": 7,
        "min_samples": 7,
        "sample_passes": 10,
        "apps": [
            {
                "name": "cmdb",
                "total_samples": 10,
                "endpoint_registered_samples": 10,
                "comparable_samples": 10,
                "span_days": 8.0,
                "lane_conflicts": 0,
                "unknown_regressions": 0,
                "conflict_examples": [],
                "regression_examples": [],
                "verdict": "READY",
            },
            {
                "name": "skchat",
                "total_samples": 10,
                "endpoint_registered_samples": 10,
                "comparable_samples": 10,
                "span_days": 8.0,
                "lane_conflicts": 2,
                "unknown_regressions": 0,
                "conflict_examples": [
                    "2026-08-24T00:00:00Z AuthEnforced: endpoint='True' old='False'"
                ],
                "regression_examples": [],
                "verdict": "BLOCKED",
            },
        ],
    }
    text = soak.render(rep)
    assert "READY TO DEMOTE (1): cmdb" in text
    assert "BLOCKED (1): skchat" in text
    assert "AuthEnforced" in text
