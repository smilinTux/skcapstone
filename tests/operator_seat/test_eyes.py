"""ATLAS Eyes: read-only, freeze-proof, and Unknown is never quietly healthy."""

from __future__ import annotations

import json
import subprocess
import time

from skcapstone.fleet import store
from skcapstone.fleet.paths import FleetPaths
from skcapstone.operator_seat import eyes

PWT = frozenset({"GradingBacklog"})


# ── condition classification ────────────────────────────────────────────────


def test_classify_condition_polarity_and_unknown():
    assert eyes.classify_condition("Ready", "True", PWT) == "quiet"
    assert eyes.classify_condition("Ready", "False", PWT) == "firing"
    assert eyes.classify_condition("GradingBacklog", "True", PWT) == "firing"
    assert eyes.classify_condition("GradingBacklog", "False", PWT) == "quiet"
    assert eyes.classify_condition("Ready", "Unknown", PWT) == "unknown"


# ── cli lane failure modes are states, never exceptions ─────────────────────


def test_cli_lane_no_binary_is_no_cli():
    lane = eyes.observe_via_cli("definitely-not-a-binary-xyz operator", ["A"], PWT)
    assert lane["state"] == "no-cli"
    assert lane["conditions"] == []


def test_cli_lane_timeout_is_timeout():
    def hang(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    lane = eyes.observe_via_cli("sh -c x", ["A"], PWT, run=hang)
    assert lane["state"] == "timeout"


def test_cli_lane_nonzero_exit_is_cli_error():
    lane = eyes.observe_via_cli("sh -c x", ["A"], PWT, run=lambda argv, t: (2, "", "Usage: nope"))
    assert lane["state"] == "cli-error"
    assert "Usage: nope" in lane["detail"]


def test_cli_lane_garbage_stdout_is_unparseable():
    lane = eyes.observe_via_cli(
        "sh -c x", ["A"], PWT, run=lambda argv, t: (0, "not json at all", "")
    )
    assert lane["state"] == "unparseable"


def test_cli_lane_ok_tolerates_warning_preamble_and_marks_absent_declared():
    payload = json.dumps({"conditions": [{"type": "A", "status": "True"}]})
    lane = eyes.observe_via_cli(
        "sh -c x",
        ["A", "B"],
        PWT,
        run=lambda argv, t: (0, "Skipping legacy file...\n" + payload, ""),
    )
    assert lane["state"] == "ok"
    by_type = {c["type"]: c for c in lane["conditions"]}
    assert by_type["A"]["class"] == "quiet"
    # declared but unreported: Unknown (absent), never silently healthy
    assert by_type["B"]["status"] == "Unknown"
    assert by_type["B"]["absent"] is True


# ── seat lane ───────────────────────────────────────────────────────────────


def test_seat_lane_missing_adapter_is_no_adapter():
    lane = eyes.observe_via_seat("ghost", {}, ["A"], PWT, None, "now")
    assert lane["state"] == "no-adapter"


def test_seat_lane_hung_adapter_is_timeout_not_a_hang():
    def sleepy(paths, now):
        time.sleep(2)
        return {"conditions": []}

    start = time.monotonic()
    lane = eyes.observe_via_seat("slow", {"slow": sleepy}, [], PWT, None, "now", timeout=0.2)
    assert lane["state"] == "timeout"
    assert time.monotonic() - start < 1.5


def test_seat_lane_raising_adapter_is_error():
    def boom(paths, now):
        raise RuntimeError("nope")

    lane = eyes.observe_via_seat("bad", {"bad": boom}, [], PWT, None, "now")
    assert lane["state"] == "error"
    assert "nope" in lane["detail"]


# ── lane merge: conflicts and verdicts ──────────────────────────────────────


def _ok_lane(conds):
    return {
        "state": "ok",
        "conditions": [
            {"type": t, "status": s, "class": eyes.classify_condition(t, s, PWT)} for t, s in conds
        ],
        "detail": "",
    }


def test_lane_conflicts_only_on_shared_types_with_different_status():
    cli = _ok_lane([("A", "True"), ("B", "True")])
    seat = _ok_lane([("A", "False"), ("C", "True")])
    conflicts = eyes.lane_conflicts(cli, seat)
    assert conflicts == [{"type": "A", "cli": "True", "seat": "False"}]


def test_verdict_precedence_blind_firing_conflict_unknown_ok():
    dead = {"state": "no-cli", "conditions": [], "detail": ""}
    assert eyes.app_verdict(dead, {"state": "no-adapter", "conditions": []}, []) == "BLIND"
    firing = _ok_lane([("A", "False")])
    assert eyes.app_verdict(firing, dead, []) == "FIRING"
    quiet = _ok_lane([("A", "True")])
    assert eyes.app_verdict(quiet, quiet, [{"type": "A", "cli": "x", "seat": "y"}]) == "CONFLICT"
    unknown = _ok_lane([("A", "Unknown")])
    assert eyes.app_verdict(quiet, unknown, []) == "UNKNOWN"
    assert eyes.app_verdict(quiet, dead, []) == "UNKNOWN"  # one lane dark, none firing
    assert eyes.app_verdict(quiet, quiet, []) == "OK"


# ── the one pass: read-only against a tmp fleet ─────────────────────────────


def _tmp_fleet(tmp_path):
    paths = FleetPaths(root=tmp_path / "fleet")
    writer = store.Writer(role="operator", node="cli", identity="test")
    store.write_spec(
        paths,
        "operatorapp",
        "appx",
        {"name": "appx", "cli": "appx-cli operator", "conditions": ["Ready"]},
        writer=writer,
    )
    return paths


def test_assess_is_read_only_and_fail_soft(tmp_path):
    paths = _tmp_fleet(tmp_path)
    before = sorted(
        (p, p.stat().st_mtime_ns) for p in (tmp_path / "fleet").rglob("*") if p.is_file()
    )

    def fake_run(argv, timeout):
        return (0, json.dumps({"conditions": [{"type": "Ready", "status": "Unknown"}]}), "")

    res = eyes.assess(
        paths,
        run=fake_run,
        adapters={},
        problem_when_true=PWT,
        skcapstone_home=tmp_path / "nohome",
        itil_home=tmp_path / "nohome",
        now_iso="2026-08-23T00:00:00Z",
    )
    after = sorted(
        (p, p.stat().st_mtime_ns) for p in (tmp_path / "fleet").rglob("*") if p.is_file()
    )
    assert before == after, "assess must never write to the fleet store"
    assert res["schema"] == eyes.SCHEMA
    assert res["frozen"] is False
    (app,) = [a for a in res["apps"] if a["name"] == "appx"]
    # no-cli (binary absent) beats the fake runner: which() runs first
    assert app["cli_lane"]["state"] == "no-cli"
    assert app["seat_lane"]["state"] == "no-adapter"
    assert app["verdict"] == "BLIND"
    assert any("appx" in s and "invisible even unfrozen" in s for s in res["blind_spots"])


def test_assess_reports_frozen_state_without_touching_it(tmp_path):
    paths = _tmp_fleet(tmp_path)
    human = store.Writer(role="operator", node="cli", identity="chef")
    store.set_frozen(paths, True, writer=human, reason="drill")
    frozen_bytes = paths.freeze_path().read_bytes()
    res = eyes.assess(
        paths,
        run=lambda argv, t: (0, "{}", ""),
        adapters={},
        problem_when_true=PWT,
        skcapstone_home=tmp_path / "nohome",
        itil_home=tmp_path / "nohome",
    )
    assert res["frozen"] is True
    assert res["freeze_reason"] == "drill"
    assert paths.freeze_path().read_bytes() == frozen_bytes


def test_assess_includes_seat_only_builtin_adapters(tmp_path):
    paths = _tmp_fleet(tmp_path)
    res = eyes.assess(
        paths,
        run=lambda argv, t: (0, "{}", ""),
        adapters={"fleetish": lambda p, n: {"conditions": [{"type": "R", "status": "False"}]}},
        problem_when_true=PWT,
        skcapstone_home=tmp_path / "nohome",
        itil_home=tmp_path / "nohome",
    )
    (extra,) = [a for a in res["apps"] if a["name"] == "fleetish"]
    assert extra["cli_lane"]["state"] == "unregistered"
    assert extra["verdict"] == "FIRING"
    assert any("fleetish" in s and "discovery path" in s for s in res["blind_spots"])


# ── rendering ───────────────────────────────────────────────────────────────


def test_render_distinguishes_unknown_from_unreachable(tmp_path):
    paths = _tmp_fleet(tmp_path)
    res = eyes.assess(
        paths,
        run=lambda argv, t: (0, "{}", ""),
        adapters={"appx": lambda p, n: {"conditions": [{"type": "Ready", "status": "Unknown"}]}},
        problem_when_true=PWT,
        skcapstone_home=tmp_path / "nohome",
        itil_home=tmp_path / "nohome",
    )
    text = eyes.render(res)
    assert "NO CLI" in text  # unreachable state, rendered as such
    assert "? Ready" in text  # Unknown condition, rendered as a question, not an error
    assert "BLIND EVEN IF UNFROZEN" in text
