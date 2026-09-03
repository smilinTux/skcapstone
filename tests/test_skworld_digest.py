"""Fleet digest tolerates valid JSONL values that are not event objects."""

from __future__ import annotations

import ast
import datetime
import json
import os
import time
from collections import Counter
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "fleet" / "skworld-digest.py"


def test_collect_board_skips_non_object_json_rows(tmp_path) -> None:
    events = tmp_path / "cards" / "deadbeef" / "events"
    events.mkdir(parents=True)
    rows = [
        "bare string",
        ["array"],
        None,
        {"action": "claim", "ts": "9999-01-01T00:00:00+00:00", "writer": "agent"},
    ]
    (events / "agent.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "collect_board"
    )
    namespace = {
        "CARDS": str(tmp_path / "cards"),
        "Counter": Counter,
        "datetime": datetime,
        "json": json,
        "os": os,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SCRIPT), "exec"), namespace)

    result = namespace["collect_board"]()

    assert result["available"] is True
    assert result["states"] == {"claimed": 1}
    assert result["throughput"] == {"m10": {"claim": 1}, "m60": {"claim": 1}}


def _load_digest_functions(*names: str) -> dict:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    functions = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    namespace = {
        "os": os,
        "time": time,
        "SYNC_OUTBOX": os.path.expanduser("~/.skcapstone/sync/outbox"),
        "OUTBOX_DEPTH_THRESHOLD": 50,
        "OUTBOX_AGE_THRESHOLD_SECONDS": 3600,
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(SCRIPT), "exec"), namespace)
    return namespace


def test_collect_outbox_depth_reports_sustained_threshold_age(tmp_path) -> None:
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    for index in range(52):
        seed = outbox / f"{index}.seed.json"
        seed.write_text("{}", encoding="utf-8")
        os.utime(seed, (100 + index, 100 + index))
    namespace = _load_digest_functions("collect_outbox_depth")

    result = namespace["collect_outbox_depth"](str(outbox), now=5000)

    assert result == {
        "available": True,
        "count": 52,
        "above_threshold_age_seconds": 4899,
    }


def test_collect_outbox_depth_does_not_age_a_shallow_backlog(tmp_path) -> None:
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    seed = outbox / "old.seed.json"
    seed.write_text("{}", encoding="utf-8")
    os.utime(seed, (100, 100))
    namespace = _load_digest_functions("collect_outbox_depth")

    result = namespace["collect_outbox_depth"](str(outbox), now=5000)

    assert result == {
        "available": True,
        "count": 1,
        "above_threshold_age_seconds": 0,
    }


def test_assess_emits_one_alert_only_after_depth_and_age_thresholds() -> None:
    namespace = _load_digest_functions("assess")
    assess = namespace["assess"]
    healthy_gateway = {"available": True, "total": 0, "running": 0, "waiting": 0}
    healthy_lanes = {"available": True, "requests_1h": 1, "errors_1h": 0}
    board = {"available": True, "states": {}}

    alerts = assess(
        healthy_gateway,
        healthy_lanes,
        [],
        board,
        {"available": True, "count": 51, "above_threshold_age_seconds": 3601},
    )
    assert [alert for alert in alerts if "outbox depth" in alert[1].lower()] == [
        (
            "warn",
            "Sync outbox depth is 51",
            "Above 50 for at least 1.0 hours. Check seed housekeeping, GPG recipients, "
            "and Syncthing peers.",
        )
    ]

    for count, age in ((50, 7200), (51, 3600)):
        alerts = assess(
            healthy_gateway,
            healthy_lanes,
            [],
            board,
            {"available": True, "count": count, "above_threshold_age_seconds": age},
        )
        assert not any("outbox depth" in alert[1].lower() for alert in alerts)
