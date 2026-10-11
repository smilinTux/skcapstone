"""Deterministic tests for detached fleet worker cgroup migration."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _load(*names: str) -> dict[str, object]:
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    # Pull in helper dependencies referenced by the requested functions.
    wanted_names = set(names) | {"_worker_card_token"}
    nodes = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted_names
    }
    assert wanted_names <= set(nodes), sorted(wanted_names - set(nodes))
    wanted = {
        "_WORKER_UNIT_RE",
        "_WORKER_CARD_RE",
        "_WORKER_CARD_RE_HEX8",
        "_WORKER_CARD_MAX",
        "_WORKER_LANES",
    }
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id in wanted for target in node.targets)
    ]
    namespace: dict[str, object] = {"re": re}
    module = ast.Module(assignments + [nodes[name] for name in sorted(wanted_names)], [])
    exec(compile(module, str(ROTATE), "exec"), namespace)
    return namespace


def test_launch_command_creates_a_collected_user_service() -> None:
    functions = _load("_worker_unit_name", "_worker_launch_command")
    unit = functions["_worker_unit_name"]("codex", "3b227de2")

    assert functions["_worker_launch_command"](unit, "/workspace", "worker") == [
        "systemd-run",
        "--user",
        "--quiet",
        "--collect",
        "--service-type=exec",
        "--unit",
        "skfleet-worker-codex-3b227de2.service",
        "--property=KillMode=control-group",
        "--working-directory",
        "/workspace",
        "bash",
        "-lc",
        "worker",
    ]


@pytest.mark.parametrize(
    ("lane", "card"),
    [("bad", "3b227de2"), ("codex", "../../bad"), ("codex", "ABCDEF12")],
)
def test_worker_unit_identity_rejects_unbounded_values(lane: str, card: str) -> None:
    function = _load("_worker_unit_name")["_worker_unit_name"]
    with pytest.raises(ValueError, match="invalid worker unit identity"):
        function(lane, card)


@pytest.mark.parametrize("card", ["a" * 65, "5a71c2dd-", "-5a71c2dd", "5a71--c2dd"])
def test_overlong_or_malformed_ids_fail_closed(card: str) -> None:
    function = _load("_worker_unit_name")["_worker_unit_name"]
    with pytest.raises(ValueError, match="invalid worker unit identity"):
        function("codex", card)


def test_suffixed_canonical_id_does_not_block_valid_subsequent_unit() -> None:
    make = _load("_worker_unit_name")["_worker_unit_name"]
    launched = []
    for card_id in ("a8100002-1", "5a71c2dd"):
        try:
            unit = make("codex", card_id)
        except ValueError:
            continue
        launched.append((card_id, unit))

    assert launched == [
        ("a8100002-1", "skfleet-worker-codex-a8100002-1.service"),
        ("5a71c2dd", "skfleet-worker-codex-5a71c2dd.service"),
    ]


def test_unit_identity_skip_precedes_workspace_and_claim() -> None:
    source = ROTATE.read_text(encoding="utf-8")
    loop = source[source.index("for _pick_index,") :]
    skip_at = loop.index("UNSUPPORTED_CARD_ID|")
    materialize_at = loop.index("workspace=_materialize_worker_workspace(")
    claim_at = loop.index('claim=subprocess.run([SKC,"coord","claim",cid')
    launch_at = loop.index("_worker_launch_command(unit,workspace,inner)")
    assert skip_at < materialize_at < claim_at < launch_at


def test_migration_counts_and_publishes_old_and_new_workers() -> None:
    functions = _load("_parse_worker_units", "_lane_busy", "_worker_cards")
    output = """
      skfleet-worker-codex-feedface.service loaded active running worker
      unrelated.service loaded active running other
      skfleet-worker-glm-cafebabe.service loaded active running worker
    """
    units = functions["_parse_worker_units"](output)
    lanes = [
        {"name": "codex", "prefix": "codex-auto-"},
        {"name": "glm", "prefix": "glm-auto-"},
    ]
    sessions = ["codex-auto-deadbeef", "persistent-pane"]

    assert functions["_lane_busy"](lanes[0], sessions, units) == [
        "codex-auto-deadbeef",
        "skfleet-worker-codex-feedface.service",
    ]
    assert functions["_lane_busy"](lanes[1], sessions, units) == [
        "skfleet-worker-glm-cafebabe.service"
    ]
    assert functions["_worker_cards"](sessions, units, lanes) == [
        "cafebabe",
        "deadbeef",
        "feedface",
    ]


def test_launch_delegates_exact_claim_release_to_wrapper() -> None:
    source = ROTATE.read_text(encoding="utf-8")
    child = source[source.index("child=(") : source.index("wrapper=os.path")]
    assert "release-claim" not in child
    assert "idle_agent" not in child
    assert 'trap "stop_beat; exit 143" HUP INT TERM' in source
    assert 'trap "stop_beat" EXIT' in source
    assert '"--claim-revision",claimed_revision' in source
    assert '"--live-snapshot",os.path.join(LIVE, HOST + ".json")' in source
    assert "subprocess.run(_worker_launch_command(unit,workspace,inner)" in source
    assert '["tmux","new-session"' not in source
