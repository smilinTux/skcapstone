"""Deterministic proof of worker stop semantics. Card 280e3c16.

AC 2 requires proving that stopping one exact worker service stops its own
descendants and cannot stop another generation or an unrelated worker, without
performing any live service action. The proof is static and deterministic:

1. Every launch goes through _worker_launch_command, so one worker IS one
   transient user service unit whose name pins the lane and the exact
   bounded canonical card ID.
2. Each worker unit carries KillMode=control-group on itself, so systemd stops
   the worker's whole descendant subtree when that exact unit stops, while
   sibling units are untouched because they are separate cgroups under the
   user manager, not descendants of the rotation oneshot.
3. The launcher never issues any stop command at all: the only systemctl
   invocation in skfleet-rotate.py is list-units, and no kill-session,
   kill-server, or systemctl stop path exists, so one worker's stop can never
   reach a different generation or lane.
4. The legacy per-card tmux session no longer exists, so the old cross-session
   kill blast radius is gone by construction.

Run: python3 -m pytest tests/test_skfleet_worker_stop_semantics.py
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"

LANES = ("codex", "glm", "qwen", "escalate")


def _functions(*names: str) -> dict[str, object]:
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


def _source() -> str:
    return ROTATE.read_text(encoding="utf-8")


def test_every_worker_is_one_exact_card_pinned_unit() -> None:
    """A unit name pins lane and exact generation, so stops can be exact."""
    functions = _functions("_worker_unit_name")
    make = functions["_worker_unit_name"]

    for lane in LANES:
        assert make(lane, "280e3c16") == f"skfleet-worker-{lane}-280e3c16.service"
        assert make(lane, "3b227de2") == f"skfleet-worker-{lane}-3b227de2.service"

    # Distinct generations of one lane produce distinct units.
    assert make("glm", "280e3c16") != make("glm", "3b227de2")
    # Distinct lanes of one generation produce distinct units.
    assert make("glm", "280e3c16") != make("codex", "280e3c16")


def test_worker_unit_owner_cannot_be_widened() -> None:
    """The bounded-canonical identity is enforced, not assumed."""
    functions = _functions("_worker_unit_name")
    make = functions["_worker_unit_name"]

    with pytest.raises(ValueError):
        make("glm", "280e3c1")  # 7 chars: below the bounded floor
    with pytest.raises(ValueError):
        make("glm", "280e3c16.service")  # unit smuggling into the card field
    with pytest.raises(ValueError):
        make("glm", "2" * 65)  # overlong: above the bounded ceiling
    with pytest.raises(ValueError):
        make("all-lanes", "280e3c16")  # wildcard lane


def test_stopping_one_worker_stops_its_descendants_and_no_sibling() -> None:
    """KillMode pins the descendant subtree on the worker's own unit."""
    functions = _functions("_worker_launch_command")
    launch = functions["_worker_launch_command"]

    own = launch("skfleet-worker-glm-280e3c16.service", "/ws/a", "worker-a")
    other = launch("skfleet-worker-glm-3b227de2.service", "/ws/b", "worker-b")

    # Each worker unit owns its descendant cgroup: a stop of that exact unit
    # takes down everything beneath it and nothing beside it.
    assert "--property=KillMode=control-group" in own
    assert "--property=KillMode=control-group" in other

    # The two workers are separate units, so systemd cannot conflate them.
    assert own[own.index("--unit") + 1] != other[other.index("--unit") + 1]
    # Separate working directories: no shared session object to kill.
    assert (
        own[own.index("--working-directory") + 1] != other[other.index("--working-directory") + 1]
    )


def test_launcher_issues_no_stop_against_other_workers() -> None:
    """The only systemctl call is a read-only list-units; no stop path exists."""
    source = _source()

    systemctl_calls = re.findall(r"systemctl[^\n]*", source)
    assert systemctl_calls, "worker unit discovery must remain present"
    for line in systemctl_calls:
        assert "stop" not in line, f"launcher must never stop units: {line}"

    # The legacy tmux kill blast radius is gone by construction.
    assert "kill-session" not in source
    assert "kill-server" not in source
    assert '["tmux","new-session"' not in source


def test_worker_scope_is_lane_and_generation_only() -> None:
    """Unit discovery matches exactly one lane and one bounded card ID."""
    functions = _functions("_parse_worker_units")
    parse = functions["_parse_worker_units"]

    output = "\n".join(
        [
            "  skfleet-worker-glm-280e3c16.service loaded active running worker",
            "  skfleet-worker-glm-3b227de2.service loaded active running worker",
            "  skfleet-worker-codex-280e3c16.service loaded active running worker",
            "  skfleet-rotate.service loaded active running rotation",
            "  skfleet-worker-glm-a8100002-1.service loaded active running worker",
            "  skfleet-worker-glm-bad--id1.service loaded active running other",
            "  skfleet-worker-all-280e3c16.service loaded active running other",
        ]
    )
    units = parse(output)
    names = sorted(unit["unit"] for unit in units)

    # Only exact lane-plus-card units are recognized; malformed IDs, the
    # rotation unit, and wildcard lanes never enter the managed set.
    assert names == [
        "skfleet-worker-codex-280e3c16.service",
        "skfleet-worker-glm-280e3c16.service",
        "skfleet-worker-glm-3b227de2.service",
        "skfleet-worker-glm-a8100002-1.service",
    ]


def test_exact_generation_claim_release_is_owned_by_the_wrapper() -> None:
    """The child cannot retire ownership before the wrapper's exact release."""
    source = _source()

    child = source[source.index("child=(") : source.index("wrapper=os.path")]
    assert "release-claim" not in child
    assert "idle_agent" not in child
    assert 'trap "stop_beat; exit 143" HUP INT TERM' in source
    assert 'trap "stop_beat" EXIT' in source
    assert '"--claim-revision",claimed_revision' in source
    # Launches still flow through the single detached launch command.
    assert "subprocess.run(_worker_launch_command(unit,workspace,inner)" in source
