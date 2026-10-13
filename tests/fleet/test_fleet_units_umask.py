"""Every fleet authority and node service unit must run with UMask=0077.

systemd user services default to UMask=0002 on chi, so Python
``mkdir(parents=True, mode=0o700)`` left parent directories 0775 and the
reviewer's guarded handoff (owned, mode 0700) refused them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# Units that are intentionally not covered: pure systemctl wrappers and
# credential syncs to other hosts, which create no authority state.
EXEMPT = {
    "skfleet-estate-glm.service",
    "skfleet-pull-chiwk13.service",
    "skfleet-ziowk01-cursor-auth-sync.service",
    "skfleet-ziowk01-kimi-auth-sync.service",
}


def _fleet_units() -> list[Path]:
    dirs = [ROOT / "systemd", ROOT / "systemd/production", ROOT / "scripts/fleet/systemd"]
    units: list[Path] = []
    for d in dirs:
        for p in sorted(d.glob("*.service")):
            if (p.name.startswith("skfleet-") or p.name == "sknoded.service") and (
                p.name not in EXEMPT
            ):
                units.append(p)
    return units


def _service_section(text: str) -> str:
    m = re.search(r"^\[Service\]\n(.*?)(?=^\[|\Z)", text, re.M | re.S)
    assert m, "no [Service] section"
    return m.group(1)


def test_discovery_finds_the_known_units():
    names = {p.name for p in _fleet_units()}
    known = {"skfleet-seat-cycle.service", "sknoded.service", "skfleet-auto-rollout.service"}
    assert known <= names


@pytest.mark.parametrize("unit", _fleet_units(), ids=lambda p: str(p.relative_to(ROOT)))
def test_fleet_unit_sets_owner_only_umask(unit: Path):
    lines = [
        ln.strip()
        for ln in _service_section(unit.read_text()).splitlines()
        if ln.strip().startswith("UMask=")
    ]
    assert lines == ["UMask=0077"], f"{unit} must set exactly UMask=0077, got {lines}"


def test_packaged_mirror_carries_umask():
    for p in (ROOT / "src/skcapstone/data/systemd").glob("skfleet-*.service"):
        assert "UMask=0077" in p.read_text(), p.name
    assert "UMask=0077" in (ROOT / "src/skcapstone/data/systemd/sknoded.service").read_text()
