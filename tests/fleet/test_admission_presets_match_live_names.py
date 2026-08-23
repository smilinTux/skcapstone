"""Every real fleet node must resolve a preset (twice-burned regression).

`admit --preset` is silent when a name has no preset: it applies no labels, no
role and no taint, and exits 0. That made the failure invisible twice.

  - `node-158` never matched; the control node's preset applied nothing until
    it was rekeyed to `node-noroc2027`.
  - `node-100` never matched either, and survived the first fix because only
    one of the two address-style keys was rekeyed. The GPU box is `node-ollama`.

Both were address-style names for boxes whose `paths.self_node_name()` derives
from the HOSTNAME. This pins every live name so a third instance fails here
rather than in the field.
"""

from __future__ import annotations

import pytest

from skcapstone.fleet.admission import PRESET_ALIASES, PRESETS, resolve_preset

#: The names `paths.self_node_name()` actually produces on this fleet, with the
#: role each is expected to bind. Add a row when a node joins; do not remove one
#: to make a test pass.
LIVE_NODES = {
    "node-noroc2027": "control",
    "node-41": "builder-standby",
    "node-ollama": "worker-gpu",
}

#: Address-style spellings that runbooks and humans still type.
LEGACY_SPELLINGS = {"node-158": "node-noroc2027", "node-100": "node-ollama"}


@pytest.mark.parametrize("name,role", sorted(LIVE_NODES.items()))
def test_every_live_node_resolves_a_preset(name: str, role: str) -> None:
    preset = resolve_preset(name)
    assert preset is not None, (
        f"{name!r} has no preset, so `admit --preset` would silently apply "
        "nothing on that box and still exit 0"
    )
    assert preset["role"] == role


@pytest.mark.parametrize("legacy,canonical", sorted(LEGACY_SPELLINGS.items()))
def test_legacy_spellings_still_resolve_to_the_same_preset(legacy: str, canonical: str) -> None:
    """A runbook that says `admit node-100 --preset` must keep working."""
    assert resolve_preset(legacy) is resolve_preset(canonical)


def test_every_canonical_preset_key_is_a_name_a_node_can_have() -> None:
    """The shape that caused both failures: a canonical key no node answers to.

    Stated as membership rather than as a spelling rule. A first attempt here
    flagged any key ending in digits, which is wrong: `node-41` ends in digits
    and IS the live hostname-derived name for that box. The defect was never
    "looks like an address", it was "matches no live node", so assert exactly
    that. Aliases carry the spellings nobody's hostname produces.
    """
    allowed = set(LIVE_NODES) | {"node-local"}
    orphans = sorted(set(PRESETS) - allowed)
    assert not orphans, (
        f"PRESET key(s) {orphans} match no live node, so `admit --preset` would "
        "silently apply nothing there. Key on the hostname-derived name and put "
        "the other spelling in PRESET_ALIASES."
    )


def test_every_alias_points_at_a_real_preset() -> None:
    dangling = {a: t for a, t in PRESET_ALIASES.items() if t not in PRESETS}
    assert not dangling, f"alias(es) pointing at no preset: {dangling}"


def test_an_unknown_node_still_returns_none() -> None:
    """Positive control: resolve_preset must not start inventing presets."""
    assert resolve_preset("node-does-not-exist") is None
