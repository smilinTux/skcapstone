"""Review host distinctness tests for the fleet rotation script."""

from __future__ import annotations

import ast
import glob
import hashlib
import json
import os
import re
from pathlib import Path

_SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"
_FUNCTION_NAMES = {
    "_fold_key",
    "_is_review_card",
    "_parse_launch_host",
    "_read_durable_launch_hosts",
    "_review_host_disposition",
    "_review_preparer_host",
}
_ROTATION_HOSTS = ("chiap01", "chiap02", "chiap03", "chiap04", "chiap08")


def _load_functions() -> dict[str, object]:
    """Compile only the pure helpers from the top-level rotation script."""
    tree = ast.parse(_SOURCE.read_text(encoding="utf-8"), filename=str(_SOURCE))
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in _FUNCTION_NAMES
    ]
    namespace: dict[str, object] = {
        "ROTATION_HOSTS": _ROTATION_HOSTS,
        "_LAUNCH_HOST_RE": re.compile(r"(?:^|\s)host=([A-Za-z0-9_.-]+)(?=\s|$)"),
        "glob": glob,
        "hashlib": hashlib,
        "json": json,
        "os": os,
        "re": re,
    }
    module = ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[]))
    exec(compile(module, str(_SOURCE), "exec"), namespace)
    return namespace


def _write_events(path: Path, events: list[dict[str, object]]) -> None:
    path.write_text(
        "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n",
        encoding="utf-8",
    )


def test_latest_durable_launch_link_records_host_and_source(tmp_path: Path) -> None:
    helpers = _load_functions()
    _write_events(
        tmp_path / "chiap08.jsonl",
        [
            {"action": "link", "card_id": "noise", "link_key": "result"},
            {
                "action": "link",
                "card_id": "7f72f938",
                "event_id": "first",
                "link_key": "launch",
                "link_value": "host=chiap08 identity=old-attempt",
                "ts": "2026-08-28T14:26:35Z",
                "writer": "jarvis",
            },
            {
                "action": "link",
                "card_id": "7f72f938",
                "event_id": "second",
                "link_key": "launch",
                "link_value": "host=chiap02 identity=pi-codex session=work",
                "ts": "2026-08-28T14:29:12Z",
                "writer": "jarvis",
            },
        ],
    )

    launches = helpers["_read_durable_launch_hosts"](str(tmp_path))

    assert launches == {"7f72f938": ("chiap02", "chiap08.jsonl:3")}


def test_c9af8738_routes_to_a_distinct_physical_host(tmp_path: Path) -> None:
    helpers = _load_functions()
    _write_events(
        tmp_path / "chiap08.jsonl",
        [
            {
                "action": "link",
                "card_id": "7f72f938",
                "link_key": "launch",
                "link_value": (
                    "host=chiap02 identity=pi-codex-chiap02-7f72f938 "
                    "session=codex-manual-7f72f938"
                ),
                "ts": "2026-08-28T14:29:12.565301+00:00",
                "writer": "jarvis",
            }
        ],
    )
    launches = helpers["_read_durable_launch_hosts"](str(tmp_path))
    helpers["_durable_launch_hosts"] = lambda: launches
    helpers["folded_dependencies"] = lambda card_id, core: core["dependencies"]
    core = {
        "title": (
            "[FLEET-GLM-ADMISSION-R3-R1][S][REVIEW] Independently review "
            "authoritative GLM admission rebuild"
        ),
        "dependencies": ["7f72f938"],
    }

    preparer_host, source = helpers["_review_preparer_host"](
        "c9af8738", core, ["fleet", "glm", "review"]
    )
    same_disposition, owner = helpers["_review_host_disposition"](
        "c9af8738", preparer_host, "chiap02"
    )
    owner_disposition, owner_readback = helpers["_review_host_disposition"](
        "c9af8738", preparer_host, owner
    )

    assert preparer_host == "chiap02"
    assert source == "7f72f938@chiap08.jsonl:1"
    assert same_disposition == "defer_same_host"
    assert owner != preparer_host
    assert owner_disposition == "owned"
    assert owner_readback == owner


def test_unknown_preparer_host_keeps_review_assignable() -> None:
    helpers = _load_functions()
    helpers["_durable_launch_hosts"] = lambda: {}
    helpers["folded_dependencies"] = lambda card_id, core: core["dependencies"]
    core = {"title": "[REVIEW] Unknown launch", "dependencies": ["subject"]}

    preparer_host, source = helpers["_review_preparer_host"]("review-card", core, [])
    disposition, owner = helpers["_review_host_disposition"](
        "review-card", preparer_host, "chiap02"
    )

    assert preparer_host is None
    assert source == "launch_missing_subject"
    assert disposition == "assignable"
    assert owner is None


def test_rotation_wires_distinctness_into_pool_ownership_and_logs() -> None:
    source = _SOURCE.read_text(encoding="utf-8")

    assert "_review_preparer_host(cid, core, labels)" in source
    assert "if cid in _REVIEW_OWNER_HOST:" in source
    assert "REVIEW_HOST_DEFERRED" in source
    assert "review_host_distinctness=%d" in source
    assert "preparer_host_source=%s" in source
