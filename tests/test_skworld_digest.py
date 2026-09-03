"""Fleet digest tolerates valid JSONL values that are not event objects."""

from __future__ import annotations

import ast
import datetime
import json
import os
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
