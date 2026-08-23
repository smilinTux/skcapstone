"""The operator seat refreshes on a timer; it must not rewrite unchanged specs.

`skoperator.timer` fires every 15 minutes and refreshes all 7 operatorapp
objects together. `_write_preserving_ratifications` used to call `write_spec`
unconditionally, and `write_spec` has no no-op short-circuit, so every refresh
bumped the generation and rewrote the file.

Measured on the live control node on 2026-08-17: those objects had reached
generation 1674, and watching one across a timer tick caught the write
directly, generation 1674 -> 1675 with a byte-identical body (sha
3abb1b3523529136 on both sides). That is roughly 672 no-op writes a day into
`~/.skcapstone`, a Syncthing folder shared to four machines.
"""

from __future__ import annotations

from skcapstone.fleet import store
from skcapstone.fleet.paths import FleetPaths
from skcapstone.operator_seat import registration

SPEC = {
    "cli": "skgateway",
    "repos": ["skgateway"],
    "conditions": ["GatewayReachable"],
    "contractVersion": 1,
    "proposedStandardActions": [],
    "deleted": False,
}


def _seat():
    return store.Writer(role="operator", node="node-41", identity="operator", agent_seat=True)


def _gen(paths, name="skgateway"):
    return (store.read_spec(paths, "operatorapp", name) or {}).get("generation")


def test_an_unchanged_refresh_does_not_bump_the_generation(tmp_path) -> None:
    """The load-bearing assertion: this is the ~672 writes/day."""
    paths = FleetPaths(root=tmp_path / "fleet")
    registration._write_preserving_ratifications(paths, "skgateway", dict(SPEC), writer=_seat())
    first = _gen(paths)
    assert first == 1

    for _ in range(5):
        registration._write_preserving_ratifications(
            paths, "skgateway", dict(SPEC), writer=_seat()
        )
    assert _gen(paths) == first, "an unchanged refresh rewrote the spec"


def test_a_changed_spec_still_writes(tmp_path) -> None:
    """Positive control. A guard that never wrote would pass the test above."""
    paths = FleetPaths(root=tmp_path / "fleet")
    registration._write_preserving_ratifications(paths, "skgateway", dict(SPEC), writer=_seat())
    before = _gen(paths)

    changed = dict(SPEC, conditions=["GatewayReachable", "WrapperReachable"])
    registration._write_preserving_ratifications(paths, "skgateway", changed, writer=_seat())

    assert _gen(paths) == before + 1
    stored = store.read_spec(paths, "operatorapp", "skgateway")
    assert stored["spec"]["conditions"] == ["GatewayReachable", "WrapperReachable"]


def test_ratifications_are_still_preserved_across_a_refresh(tmp_path) -> None:
    """The original reason this helper exists must survive the new guard."""
    paths = FleetPaths(root=tmp_path / "fleet")
    registration._write_preserving_ratifications(paths, "skgateway", dict(SPEC), writer=_seat())

    stored = store.read_spec(paths, "operatorapp", "skgateway")
    spec = dict(stored["spec"], ratifiedStandardActions=["restart"])
    store.write_spec(
        paths,
        "operatorapp",
        "skgateway",
        spec,
        writer=store.Writer(role="operator", node="node-41", identity="human"),
    )

    registration._write_preserving_ratifications(paths, "skgateway", dict(SPEC), writer=_seat())
    after = store.read_spec(paths, "operatorapp", "skgateway")
    assert after["spec"]["ratifiedStandardActions"] == ["restart"]


def test_a_ratification_alone_does_not_cause_a_rewrite_loop(tmp_path) -> None:
    """Guard against the subtle version: the helper injects the prior
    ratifications into the spec it compares, so a ratified object must also
    settle instead of differing from the incoming spec on every pass."""
    paths = FleetPaths(root=tmp_path / "fleet")
    registration._write_preserving_ratifications(paths, "skgateway", dict(SPEC), writer=_seat())
    stored = store.read_spec(paths, "operatorapp", "skgateway")
    store.write_spec(
        paths,
        "operatorapp",
        "skgateway",
        dict(stored["spec"], ratifiedStandardActions=["restart"]),
        writer=store.Writer(role="operator", node="node-41", identity="human"),
    )
    settled = _gen(paths)

    for _ in range(3):
        registration._write_preserving_ratifications(
            paths, "skgateway", dict(SPEC), writer=_seat()
        )
    assert _gen(paths) == settled, "a ratified object rewrites on every refresh"
