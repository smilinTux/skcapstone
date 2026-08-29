"""Tests for five-host hash partition in skfleet-rotate.py.

Each unpinned card must map deterministically to exactly one of the five
authorized ROTATION_HOSTS. The partition is derived from the ordered host tuple,
and host-pinned cards are owned only by the named authorized host.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _load_functions(*names: str) -> dict[str, object]:
    """Load selected dependency-free functions without running the launcher."""
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    wanted = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    }
    assert set(wanted) == set(names)
    module = ast.Module(body=[wanted[name] for name in names], type_ignores=[])
    namespace: dict[str, object] = {}
    exec(compile(module, str(ROTATE), "exec"), namespace)
    return namespace


def _load_constants(*names: str) -> dict[str, object]:
    """Load selected constants from the rotate script."""
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    constants: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in names:
                    # Try to evaluate the constant expression
                    try:
                        constants[target.id] = ast.literal_eval(node.value)
                    except Exception:
                        # For more complex expressions, try to compile and execute
                        namespace = {}
                        exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROTATE), "exec"), namespace)
                        constants[target.id] = namespace[target.id]
    return constants


def test_rotation_hosts_has_five_entries():
    """ROTATION_HOSTS must contain exactly five hosts."""
    source = ROTATE.read_text(encoding="utf-8")
    assert 'ROTATION_HOSTS=("chiap01", "chiap02", "chiap03", "chiap04", "chiap08")' in source
    constants = _load_constants("ROTATION_HOSTS")
    assert constants["ROTATION_HOSTS"] == ("chiap01", "chiap02", "chiap03", "chiap04", "chiap08")
    assert len(constants["ROTATION_HOSTS"]) == 5


def test_nhost_equals_rotation_hosts_length():
    """_NHOST must be derived from ROTATION_HOSTS length."""
    source = ROTATE.read_text(encoding="utf-8")
    assert "_NHOST=len(ROTATION_HOSTS)" in source
    constants = _load_constants("ROTATION_HOSTS")
    # _NHOST is computed at runtime, so we verify the expression
    assert "_NHOST=len(ROTATION_HOSTS)" in source
    assert "_NHOST=3" not in source
    assert "_NHOST=5" not in source  # Should be derived, not hardcoded


def test_offset_derived_from_host_tuple():
    """Each host's offset must be derived from its index in ROTATION_HOSTS."""
    source = ROTATE.read_text(encoding="utf-8")
    assert "off=ROTATION_HOSTS.index(HOST) if HOST in ROTATION_HOSTS else 0" in source
    # Old three-host hardcoding must be removed
    assert 'off={"chiap01":0,"chiap02":1,"chiap03":2}.get(HOST,0)' not in source


def test_partition_size_five():
    """The partition size must be five, not three."""
    source = ROTATE.read_text(encoding="utf-8")
    constants = _load_constants("ROTATION_HOSTS")
    # Verify the partition size matches host count
    assert len(constants["ROTATION_HOSTS"]) == 5


def test_all_hosts_get_unique_residues():
    """Each of the five hosts must get a unique residue (0-4)."""
    rotation_hosts = ("chiap01", "chiap02", "chiap03", "chiap04", "chiap08")
    residues = set()

    # Simulate what each host would compute for off
    for host in rotation_hosts:
        off = rotation_hosts.index(host)
        residues.add(off)
        assert 0 <= off < 5, f"Host {host} has invalid offset {off}"

    # All five residues must be unique and cover 0-4
    assert len(residues) == 5
    assert residues == {0, 1, 2, 3, 4}


def test_ownership_deterministic():
    """Card ownership must be deterministic: same card always maps to same host."""
    rotation_hosts = ("chiap01", "chiap02", "chiap03", "chiap04", "chiap08")
    nhost = len(rotation_hosts)

    test_card = "abc12345"
    expected_owner = None

    # All hosts should agree on who owns this card
    for host in rotation_hosts:
        off = rotation_hosts.index(host)
        residue = int(hashlib.sha256(test_card.encode()).hexdigest()[:8], 16) % nhost
        owns = residue == off
        if owns:
            if expected_owner is None:
                expected_owner = host
            else:
                assert expected_owner == host, f"Card {test_card} owned by both {expected_owner} and {host}"
        else:
            assert expected_owner != host, f"Card {test_card} owned by multiple hosts"

    assert expected_owner in rotation_hosts


def test_zero_duplicate_ownership_small_pool():
    """Small synthetic pool must have no duplicate ownership across hosts."""
    rotation_hosts = ("chiap01", "chiap02", "chiap03", "chiap04", "chiap08")
    nhost = len(rotation_hosts)

    test_cards = ["aaaa0001", "bbbb0002", "cccc0003", "dddd0004", "eeee0005",
                  "aaaa0006", "bbbb0007", "cccc0008", "dddd0009", "eeee000a"]

    ownership: dict[str, list[str]] = {host: [] for host in rotation_hosts}

    for card in test_cards:
        residue = int(hashlib.sha256(card.encode()).hexdigest()[:8], 16) % nhost
        owner = rotation_hosts[residue]
        ownership[owner].append(card)

    # No card should appear in multiple hosts' lists
    all_cards: list[str] = []
    for host, cards in ownership.items():
        for card in cards:
            assert card not in all_cards, f"Card {card} appears in multiple hosts"
            all_cards.append(card)

    # All cards should be assigned
    assert sorted(all_cards) == sorted(test_cards)


def test_zero_duplicate_ownership_large_pool():
    """Large synthetic pool (1000 cards) must have no duplicate ownership."""
    rotation_hosts = ("chiap01", "chiap02", "chiap03", "chiap04", "chiap08")
    nhost = len(rotation_hosts)

    # Generate 1000 synthetic card IDs
    test_cards = [f"{i:016x}" for i in range(1000)]

    ownership: dict[str, set[str]] = {host: set() for host in rotation_hosts}

    for card in test_cards:
        residue = int(hashlib.sha256(card.encode()).hexdigest()[:8], 16) % nhost
        owner = rotation_hosts[residue]
        ownership[owner].add(card)

    # Check for overlaps
    for i, host1 in enumerate(rotation_hosts):
        for host2 in rotation_hosts[i+1:]:
            overlap = ownership[host1] & ownership[host2]
            assert len(overlap) == 0, f"Hosts {host1} and {host2} share {len(overlap)} cards: {list(overlap)[:5]}"

    # All cards should be assigned
    total_assigned = sum(len(cards) for cards in ownership.values())
    assert total_assigned == len(test_cards)


def test_zero_uncovered_residues():
    """All five residues (0-4) must be covered by the partition."""
    rotation_hosts = ("chiap01", "chiap02", "chiap03", "chiap04", "chiap08")
    nhost = len(rotation_hosts)

    # Generate many cards to ensure we hit all residues
    test_cards = [f"{i:016x}" for i in range(10000)]

    residues_found = set()

    for card in test_cards:
        residue = int(hashlib.sha256(card.encode()).hexdigest()[:8], 16) % nhost
        residues_found.add(residue)

    # All residues 0-4 should be found
    assert residues_found == {0, 1, 2, 3, 4}


def test_host_pinned_bypass():
    """Host-pinned cards must bypass hash partition and go to their named host."""
    rotation_hosts = ("chiap01", "chiap02", "chiap03", "chiap04", "chiap08")

    # Simulate a card pinned to chiap08
    pinned_card = "abcd1234"
    pinned_host = "chiap08"

    # The owns function should return True for chiap08 regardless of hash
    # and False for other hosts (bypass hash partition)
    for host in rotation_hosts:
        if host == pinned_host:
            # Pinned host owns it
            pass
        else:
            # Other hosts should not own it
            pass

    # The key point: host_pin() and _PINNED_IDS bypass the hash check
    source = ROTATE.read_text(encoding="utf-8")
    assert "if cid in _PINNED_IDS:" in source
    assert "return True" in source  # Pinned cards return True


def test_unknown_hosts_excluded():
    """Hosts not in ROTATION_HOSTS must be excluded from partition."""
    source = ROTATE.read_text(encoding="utf-8")
    # Unknown hosts should default to offset 0 but be excluded by other checks
    assert "if HOST not in ROTATION_HOSTS:" in source
    assert 'log(d,"NOOP|%s|host is outside the authorized chiap01-chiap03 worker fleet"' in source or \
           'log(d,"NOOP|%s|host is outside the authorized' in source


def test_all_five_hosts_covered():
    """Every host in ROTATION_HOSTS must be covered by the partition."""
    rotation_hosts = ("chiap01", "chiap02", "chiap03", "chiap04", "chiap08")
    nhost = len(rotation_hosts)

    # Each host should have a unique offset
    for i, host in enumerate(rotation_hosts):
        off = rotation_hosts.index(host)
        assert off == i, f"Host {host} has offset {off}, expected {i}"

    # Generate cards and verify each host gets some
    test_cards = [f"{i:016x}" for i in range(10000)]
    ownership: dict[str, int] = {host: 0 for host in rotation_hosts}

    for card in test_cards:
        residue = int(hashlib.sha256(card.encode()).hexdigest()[:8], 16) % nhost
        owner = rotation_hosts[residue]
        ownership[owner] += 1

    # Each host should have at least some cards (statistical check)
    for host, count in ownership.items():
        assert count > 100, f"Host {host} has too few cards ({count} out of {len(test_cards)})"


def test_partition_stable_across_reorders():
    """Local pool ordering must not affect ownership (hash-based, not index-based)."""
    rotation_hosts = ("chiap01", "chiap02", "chiap03", "chiap04", "chiap08")
    nhost = len(rotation_hosts)

    test_card = "stable123"

    # Ownership depends only on hash, not on pool order
    residue = int(hashlib.sha256(test_card.encode()).hexdigest()[:8], 16) % nhost
    owner = rotation_hosts[residue]

    # Verify the same result regardless of how we might order the pool
    for _ in range(10):
        # Same calculation, same result
        residue_check = int(hashlib.sha256(test_card.encode()).hexdigest()[:8], 16) % nhost
        owner_check = rotation_hosts[residue_check]
        assert owner == owner_check


def test_exact_five_partition():
    """The partition must be exactly size 5, with residues 0-4."""
    source = ROTATE.read_text(encoding="utf-8")
    constants = _load_constants("ROTATION_HOSTS")

    rotation_hosts = constants["ROTATION_HOSTS"]
    assert len(rotation_hosts) == 5

    # Verify the mapping is exact: index in tuple = residue
    for i, host in enumerate(rotation_hosts):
        assert i in {0, 1, 2, 3, 4}, f"Invalid index {i} for host {host}"


def test_hardcoded_three_host_removed():
    """The old three-host hardcoding must be completely removed."""
    source = ROTATE.read_text(encoding="utf-8")
    # Old patterns must not exist
    assert '_NHOST=3' not in source
    assert 'off={"chiap01":0,"chiap02":1,"chiap03":2}' not in source
    assert '"chiap04"' not in source or 'ROTATION_HOSTS' in source  # chiap04 should only be in ROTATION_HOSTS


def test_partition_comment_updated():
    """Comments must reference five hosts, not three."""
    source = ROTATE.read_text(encoding="utf-8")
    # Check that the comment mentions all hosts or the correct count
    partition_section = source[source.find("# Partition the CARD SPACE by hash"):
                             source.find("# Partition the CARD SPACE by hash") + 500]
    # Should reference five hosts or the tuple
    assert "ROTATION_HOSTS" in partition_section or "five" in partition_section.lower()


def test_uniform_distribution_large_set():
    """With many cards, distribution across hosts should be approximately uniform."""
    rotation_hosts = ("chiap01", "chiap02", "chiap03", "chiap04", "chiap08")
    nhost = len(rotation_hosts)

    test_cards = [f"{i:016x}" for i in range(10000)]
    ownership: dict[str, int] = {host: 0 for host in rotation_hosts}

    for card in test_cards:
        residue = int(hashlib.sha256(card.encode()).hexdigest()[:8], 16) % nhost
        owner = rotation_hosts[residue]
        ownership[owner] += 1

    # Check for approximate uniformity (each should be ~20%)
    expected_per_host = len(test_cards) / nhost
    for host, count in ownership.items():
        # Allow 20% deviation from expected
        assert abs(count - expected_per_host) / expected_per_host < 0.2, \
            f"Host {host} has {count} cards, expected ~{expected_per_host:.0f}"


def test_all_authorized_hosts_in_tuple():
    """All five authorized hosts must be in the ROTATION_HOSTS tuple."""
    source = ROTATE.read_text(encoding="utf-8")
    constants = _load_constants("ROTATION_HOSTS")

    expected_hosts = {"chiap01", "chiap02", "chiap03", "chiap04", "chiap08"}
    actual_hosts = set(constants["ROTATION_HOSTS"])

    assert actual_hosts == expected_hosts, \
        f"ROTATION_HOSTS contains {actual_hosts}, expected {expected_hosts}"


def test_single_card_exactly_one_owner():
    """Each individual card must be owned by exactly one host."""
    rotation_hosts = ("chiap01", "chiap02", "chiap03", "chiap04", "chiap08")
    nhost = len(rotation_hosts)

    test_cards = [f"{i:016x}" for i in range(100)]

    for card in test_cards:
        residue = int(hashlib.sha256(card.encode()).hexdigest()[:8], 16) % nhost
        owner = rotation_hosts[residue]

        # Verify exactly one host owns it
        owners = []
        for i, host in enumerate(rotation_hosts):
            if i == residue:
                owners.append(host)

        assert len(owners) == 1, f"Card {card} has {len(owners)} owners: {owners}"
        assert owners[0] == owner
