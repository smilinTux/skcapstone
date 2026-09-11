"""The worker fleet is estate configuration, and the default must never move.

Card ownership is a hash of the card id modulo the rotation host tuple, so the
tuple's contents AND its order decide who may claim what. These tests exist to
make a future edit that reshuffles either one fail loudly here instead of
silently handing two hosts the same card on a live estate.
"""

from __future__ import annotations

import ast
import hashlib
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"

#: The fleet this file carried as a literal before it became configurable.
#: Frozen here on purpose: it is the value an estate that declares nothing must
#: still get, in this exact order.
FROZEN_DEFAULT = ("chiap01", "chiap02", "chiap03", "chiap04", "chiap08")

#: Owners computed under FROZEN_DEFAULT, recorded so a change of membership or
#: order cannot pass review unnoticed.
FROZEN_OWNERS = {
    "b2fec032": "chiap04",
    "c5a81d4b": "chiap02",
    "0e98a570": "chiap02",
    "90b5b277": "chiap02",
    "504d0046": "chiap08",
    "fb801e30": "chiap01",
    "39922b45": "chiap04",
    "b0c8489a": "chiap02",
    "cb9259ff": "chiap01",
    "554d393b": "chiap02",
    "a1b2c3d4": "chiap04",
    "deadbeef": "chiap04",
    "feedface": "chiap03",
    "00000001": "chiap08",
    "abcdef01": "chiap08",
}


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
    namespace: dict[str, object] = {"os": os, "re": re, "hashlib": hashlib}
    exec(compile(module, str(ROTATE), "exec"), namespace)
    return namespace


def test_unset_configuration_reproduces_the_original_tuple_exactly() -> None:
    """No estate record and no variable must give the historic fleet, in order."""
    resolve = _load_functions("_resolve_rotation_hosts")["_resolve_rotation_hosts"]
    assert resolve({}, None) == FROZEN_DEFAULT
    assert resolve({"SKFLEET_ROTATION_HOSTS": ""}, None) == FROZEN_DEFAULT
    assert resolve({}, ()) == FROZEN_DEFAULT
    source = ROTATE.read_text(encoding="utf-8")
    assert "ROTATION_HOSTS=_resolve_rotation_hosts(declared=_estate_rotation_hosts())" in source


def test_default_partitioning_is_unchanged_for_known_card_ids() -> None:
    """Fixed card ids keep their owners under the default configuration."""
    helpers = _load_functions("_resolve_rotation_hosts", "_partition_owner")
    hosts = helpers["_resolve_rotation_hosts"]({}, None)
    owner = helpers["_partition_owner"]
    actual = {card_id: owner(card_id, hosts) for card_id in FROZEN_OWNERS}
    assert actual == FROZEN_OWNERS
    assert set(actual.values()) == set(FROZEN_DEFAULT), "every host must own something"


def test_a_second_estate_partitions_only_across_its_own_hosts() -> None:
    """A declared roster owns every card, and the chi fleet owns none of them."""
    helpers = _load_functions("_resolve_rotation_hosts", "_partition_owner")
    resolve = helpers["_resolve_rotation_hosts"]
    owner = helpers["_partition_owner"]
    nor = ("noroc2027", "norwk01")
    hosts = resolve({}, nor)
    assert hosts == nor
    owners = {card_id: owner(card_id, hosts) for card_id in FROZEN_OWNERS}
    assert set(owners.values()) <= set(nor)
    assert not set(owners.values()) & set(FROZEN_DEFAULT)
    single = resolve({}, ["noroc2027"])
    assert {owner(card_id, single) for card_id in FROZEN_OWNERS} == {"noroc2027"}


def test_the_variable_overrides_the_estate_record_and_is_normalized() -> None:
    """A bootstrapping host may state the roster before its record has synced."""
    resolve = _load_functions("_resolve_rotation_hosts")["_resolve_rotation_hosts"]
    env = {"SKFLEET_ROTATION_HOSTS": " NOROC2027 , norwk01 "}
    assert resolve(env, ("chiap01",)) == ("noroc2027", "norwk01")


@pytest.mark.parametrize(
    "value",
    ["chiap01,chiap01", "chiap01,,chiap01", "chiap01,bad host", "chiap01,-nope", ","],
)
def test_a_malformed_roster_stops_the_cycle(value: str) -> None:
    """Repeats and malformed names refuse rather than silently reshuffling owners."""
    resolve = _load_functions("_resolve_rotation_hosts")["_resolve_rotation_hosts"]
    with pytest.raises(SystemExit) as excinfo:
        resolve({"SKFLEET_ROTATION_HOSTS": value}, None)
    assert "BLOCKED|SKFLEET_ROTATION_HOSTS" in str(excinfo.value)


def test_the_refusal_names_the_configured_fleet_not_a_stale_literal() -> None:
    """The old message claimed a three host fleet that had not existed for months."""
    source = ROTATE.read_text(encoding="utf-8")
    assert "chiap01-chiap03" not in source
    assert "host is outside this estate's worker fleet: %s" in source


def test_unset_authority_configuration_keeps_the_historic_publisher() -> None:
    """An estate that declares nothing publishes from the host it always did."""
    resolve = _load_functions("_resolve_authority_host")["_resolve_authority_host"]
    assert resolve({}, None) == "chiap08"
    assert resolve({"SKFLEET_AUTHORITY_HOST": ""}, None) == "chiap08"
    assert resolve({}, "") == "chiap08"
    source = ROTATE.read_text(encoding="utf-8")
    assert "AUTHORITY_HOST=_resolve_authority_host(declared=_estate_authority_host())" in source


def test_a_second_estate_names_its_own_publisher() -> None:
    """No estate hostname is baked into who may write the shared reports."""
    resolve = _load_functions("_resolve_authority_host")["_resolve_authority_host"]
    assert resolve({}, "noroc2027") == "noroc2027"
    assert resolve({}, " NorWK01 ") == "norwk01"


def test_the_variable_overrides_the_declared_publisher_and_is_normalized() -> None:
    """A bootstrapping host may state the publisher before its record has synced."""
    resolve = _load_functions("_resolve_authority_host")["_resolve_authority_host"]
    assert resolve({"SKFLEET_AUTHORITY_HOST": " NOROC2027 "}, "chiap08") == "noroc2027"


@pytest.mark.parametrize("value", ["bad host", "-nope", "a" * 65, "chiap01,chiap08"])
def test_a_malformed_publisher_stops_the_cycle(value: str) -> None:
    """A wrong publisher elects no writer, and the shared report then goes stale."""
    resolve = _load_functions("_resolve_authority_host")["_resolve_authority_host"]
    with pytest.raises(SystemExit) as excinfo:
        resolve({"SKFLEET_AUTHORITY_HOST": value}, None)
    assert "BLOCKED|SKFLEET_AUTHORITY_HOST" in str(excinfo.value)


def test_both_fleet_roles_resolve_the_same_way() -> None:
    """One pattern, not two near-identical helpers that drift apart later."""
    source = ROTATE.read_text(encoding="utf-8")
    for name in ("_resolve_rotation_hosts", "_resolve_authority_host"):
        assert "def %s(env=None, declared=None" % name in source
    assert 'if HOST != "chiap08"' not in source
