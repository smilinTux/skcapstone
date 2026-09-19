"""Deterministic proof of worker stop semantics. Card 280e3c16.

AC 2 requires proving that stopping one exact worker service stops its own
descendants and cannot stop another generation or an unrelated worker, without
performing any live service action. The proof is static and deterministic:

1. Every launch goes through _worker_launch_command, so one worker IS one
   transient user service unit whose name pins the lane and the exact
   8-hex card generation.
2. Each worker unit carries KillMode=control-group on itself, so systemd stops
   the worker's whole descendant subtree when that exact unit stops, while
   sibling units are untouched because they are separate cgroups under the
   user manager, not descendants of the rotation oneshot.
3. The launcher has exactly ONE stop path, _stop_wedged_unit, and it refuses
   any name that is not one lane plus one 8-hex generation before reaching a
   subprocess, so one worker's stop can never reach a different generation or
   lane. This clause used to read "the launcher never issues any stop command
   at all"; that was true and sufficient until the wedged-worker reaper landed
   on 2026-09-19, and the proof is now by constraint rather than by absence.
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
    nodes = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    }
    assert set(nodes) == set(names), f"missing functions: {set(names) - set(nodes)}"
    namespace: dict[str, object] = {
        "_WORKER_UNIT_RE": re.compile(
            r"^skfleet-worker-(codex|glm|qwen|escalate)-([0-9a-f]{8})\.service$"
        ),
        "re": re,
    }
    exec(
        compile(ast.Module([nodes[name] for name in names], []), str(ROTATE), "exec"),
        namespace,
    )
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
    """The exact-generation identity is enforced, not assumed."""
    functions = _functions("_worker_unit_name")
    make = functions["_worker_unit_name"]

    with pytest.raises(ValueError):
        make("glm", "280e3c1")  # 7 hex: short generation
    with pytest.raises(ValueError):
        make("glm", "280e3c166")  # 9 hex: extended generation
    with pytest.raises(ValueError):
        make("glm", "280e3c16.service")  # unit smuggling into the card field
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


def _owning_function(tree: ast.Module) -> dict[int, str]:
    """Map each node id to the module-level function that contains it."""
    owner: dict[int, str] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            for child in ast.walk(node):
                owner.setdefault(id(child), node.name)
    return owner


def test_the_only_stop_path_is_fenced_to_one_exact_worker_unit() -> None:
    """A stop may exist, but only against one validated lane and generation.

    This assertion used to be "no systemctl line anywhere contains stop".
    That was a proof by ABSENCE, and it was the right proof while nothing in
    the launcher had any reason to stop a worker.

    On 2026-09-19 one did: a worker held card 139ec63d for 6h18m having
    written nothing, and ending it requires stopping its unit.  So the proof
    changes from absence to CONSTRAINT.  The property being protected is
    unchanged and is the one AC 2 of card 280e3c16 actually cares about: a
    stop can never reach a different generation or a different lane.

    It is now enforced three ways rather than by there being no stop at all:

    1. every ``systemctl ... stop`` in the launcher lives in exactly one
       function, ``_stop_wedged_unit``;
    2. that function's FIRST statement is a guard that returns False unless
       the name matches ``_WORKER_UNIT_RE``, which pins one lane and one
       8-hex generation, so an unvalidated name cannot reach a subprocess;
    3. the stop targets the bound ``unit`` name, never a pattern or a glob.
    """
    source = _source()
    tree = ast.parse(source)
    owner = _owning_function(tree)

    stop_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.List)
        and {element.value for element in node.elts if isinstance(element, ast.Constant)}
        >= {"systemctl", "stop"}
    ]
    assert stop_calls, "the wedged-worker stop path has vanished"
    for node in stop_calls:
        assert owner.get(id(node)) == "_stop_wedged_unit", (
            "a systemctl stop appeared outside the single fenced stop path, "
            f"in {owner.get(id(node))}"
        )
        # The target is the validated unit NAME, never a pattern or a glob.
        target = node.elts[-1]
        assert isinstance(target, ast.Name) and target.id == "unit", ast.dump(target)

    stopper = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_stop_wedged_unit"
    )
    body = [node for node in stopper.body if not _is_docstring(node)]
    guard = body[0]
    assert isinstance(guard, ast.If), "the fence is not the first thing that runs"
    assert "fullmatch" in {
        child.attr for child in ast.walk(guard.test) if isinstance(child, ast.Attribute)
    }
    assert "_WORKER_UNIT_RE" in {
        child.id for child in ast.walk(guard.test) if isinstance(child, ast.Name)
    }
    assert any(
        isinstance(node, ast.Return)
        and isinstance(node.value, ast.Constant)
        and node.value.value is False
        for node in guard.body
    ), "the fence does not fail closed"

    # Unit DISCOVERY is still read-only, and still present.
    systemctl_lines = re.findall(r"systemctl[^\n]*", source)
    assert any("list-units" in line for line in systemctl_lines)

    # The legacy tmux kill blast radius is still gone by construction.
    assert "kill-session" not in source
    assert "kill-server" not in source
    assert '["tmux","new-session"' not in source


def _is_docstring(node: ast.stmt) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)


def test_worker_scope_is_lane_and_generation_only() -> None:
    """Unit discovery matches exactly one lane and one 8-hex generation."""
    functions = _functions("_parse_worker_units")
    parse = functions["_parse_worker_units"]

    output = "\n".join(
        [
            "  skfleet-worker-glm-280e3c16.service loaded active running worker",
            "  skfleet-worker-glm-3b227de2.service loaded active running worker",
            "  skfleet-worker-codex-280e3c16.service loaded active running worker",
            "  skfleet-rotate.service loaded active running rotation",
            "  skfleet-worker-glm-280e3c166.service loaded active running other",
            "  skfleet-worker-all-280e3c16.service loaded active running other",
        ]
    )
    units = parse(output)
    names = sorted(unit["unit"] for unit in units)

    # Only exact lane-plus-generation units are recognized; the rotation unit,
    # overlong generations, and wildcard lanes never enter the managed set.
    assert names == [
        "skfleet-worker-codex-280e3c16.service",
        "skfleet-worker-glm-280e3c16.service",
        "skfleet-worker-glm-3b227de2.service",
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
