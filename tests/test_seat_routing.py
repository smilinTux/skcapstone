"""Seat routing: a card labelled seat-<name> runs under that seat's identity.

Both functions are lifted verbatim out of the shipped script, so this tests the
source that runs rather than a paraphrase of it.

Run: python3 tests/test_seat_routing.py
"""

import ast
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

SRC = os.path.join(os.path.dirname(__file__), "..", "scripts", "fleet", "skfleet-rotate.py")


HOSTS = ("chiap01", "chiap02", "chiap03", "chiap04", "chiap08")


def _load(home, placement=None, placement_error=None):
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    names = {
        "_partition_owner",
        "_load_seat_placement",
        "seat_for",
        "_seat_is_provisioned",
        "_seat_owner",
        "_pool_v2_owner_map",
        "_active_dispatcher_snapshot",
        "_worker_owner",
    }
    fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(fns) == len(names), "expected seat placement functions in shipped script"
    assigns = [
        n
        for n in tree.body
        if isinstance(n, ast.Assign)
        and getattr(n.targets[0], "id", "") in ("_SEAT_LABEL_PREFIX", "_SEAT_RE")
    ]
    labels = {}
    ns = {
        "re": re,
        "json": json,
        "hashlib": hashlib,
        "os": os,
        "Path": Path,
        "ROTATION_HOSTS": HOSTS,
        "folded_labels": lambda cid, core: labels.get(cid, []),
        "log": lambda *a, **k: None,
        "d": None,
        "HOST": "testhost",
        "HOME": home,
        "_SEAT_PLACEMENT_PATH": str(Path(home) / "seat-placement.json"),
        "_SEAT_PLACEMENT": placement or {},
        "_SEAT_PLACEMENT_ERROR": placement_error,
    }
    exec(compile(ast.Module(body=assigns + fns, type_ignores=[]), SRC, "exec"), ns)
    return ns, labels


def test_capacity_requires_an_active_local_dispatcher() -> None:
    ns, _ = _load("/tmp")
    valid = {
        "dispatcher": {
            "schema_version": 1,
            "active": True,
            "host": "chiap03",
            "mode": "skfleet-rotate",
        }
    }
    assert ns["_active_dispatcher_snapshot"](valid, "chiap03") is True
    for key, value in (("active", False), ("host", "chiap08"), ("mode", "publisher")):
        candidate = {"dispatcher": dict(valid["dispatcher"], **{key: value})}
        assert ns["_active_dispatcher_snapshot"](candidate, "chiap03") is False


def test_worker_owner_keeps_the_standard_pi_prefix() -> None:
    """Seat identity stays compatible with fleet parsers and the reaper."""
    tree = ast.parse(open(SRC, encoding="utf-8").read())
    fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_worker_owner"
    )
    namespace = {"HOST": "chiap02"}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), SRC, "exec"), namespace)
    assert namespace["_worker_owner"]("codex", "deadbeef", "link") == ("pi-link-chiap02-deadbeef")
    assert namespace["_worker_owner"]("codex", "deadbeef") == ("pi-codex-chiap02-deadbeef")


def test_public_manifest_is_strict_and_bounded(tmp_path: Path) -> None:
    ns, _ = _load(str(tmp_path))
    manifest = tmp_path / "seat-placement.json"
    manifest.write_text(
        json.dumps({"schema_version": 1, "seats": {"link": ["chiap08"]}}),
        encoding="utf-8",
    )
    assert ns["_load_seat_placement"](manifest) == ({"link": ("chiap08",)}, None)

    manifest.write_text(
        json.dumps({"schema_version": 1, "seats": {"link": ["not-a-host"]}}),
        encoding="utf-8",
    )
    placement, error = ns["_load_seat_placement"](manifest)
    assert placement == {}
    assert error == "manifest-hosts:link"


def test_exact_seat_link_card_shape_is_preserved(tmp_path: Path) -> None:
    ns, labels = _load(str(tmp_path), {"link": ("chiap08",)})
    labels["9e467869"] = ["skcapstone", "skcoord", "seat-link", "source-only"]
    assert ns["seat_for"]("9e467869", {}) == "link"


def test_seat_owner_one_multiple_missing_pin_and_ordinary(tmp_path: Path) -> None:
    ns, _ = _load(str(tmp_path))
    owner = ns["_seat_owner"]
    assert owner("9e467869", "link", placement={"link": ("chiap08",)}) == (
        "chiap08",
        "seat:link",
    )

    placement = {"link": ("chiap01", "chiap03", "chiap08")}
    first = owner("9e467869", "link", placement=placement)
    assert first == owner("9e467869", "link", placement=placement)
    assert first[0] in placement["link"]

    assert owner("9e467869", "link", placement={}) == (
        None,
        "seat-unprovisioned:link",
    )
    assert owner(
        "9e467869",
        "link",
        placement={},
        placement_error="manifest-schema",
    ) == (None, "seat-manifest:manifest-schema")
    assert owner("9e467869", "link", pinned_host="chiap04", placement={"link": ("chiap08",)}) == (
        None,
        "seat-pin-conflict:link:chiap04",
    )
    assert owner("9e467869", "link", pinned_host="chiap08", placement={"link": ("chiap08",)}) == (
        "chiap08",
        "seat-pin:link:chiap08",
    )

    ordinary = owner("ordinary-card", None, placement={})
    assert ordinary[0] in HOSTS
    assert ordinary[1] == "ordinary"


def test_elastic_review_owner_requires_one_clean_niobe_placement(tmp_path: Path) -> None:
    ns, _ = _load(str(tmp_path), {"niobe": ("chiap08",)})
    card_id = "64c201a1"
    row = [2, 4, card_id, {}, [], 0]
    admission = {"elastic_review_admitted": True, "host_pin": None}
    ns["_POOL_V2_ADMISSIONS"] = {card_id: admission}

    assert ns["_partition_owner"](card_id, HOSTS) == "chiap03"
    assert ns["_pool_v2_owner_map"]([row], "chiap08", set()) == (
        {card_id: "chiap08"},
        {},
    )
    admission["host_pin"] = "chiap08"
    assert ns["_pool_v2_owner_map"]([row], "chiap08", set()) == (
        {card_id: "chiap08"},
        {},
    )
    admission["host_pin"] = None

    for placement, error, reason in (
        ({}, None, "seat-unprovisioned:niobe"),
        ({"niobe": ("chiap03", "chiap08")}, None, "seat-nonunique:niobe"),
        ({"niobe": ("chiap08",)}, "manifest-schema", "seat-manifest:manifest-schema"),
    ):
        ns["_SEAT_PLACEMENT"] = placement
        ns["_SEAT_PLACEMENT_ERROR"] = error
        assert ns["_pool_v2_owner_map"]([row], "chiap08", set()) == (
            {card_id: "unassigned:" + reason},
            {card_id: reason},
        )

    ns["_SEAT_PLACEMENT"] = {"niobe": ("chiap08",)}
    ns["_SEAT_PLACEMENT_ERROR"] = None
    admission["host_pin"] = "chiap03"
    assert ns["_pool_v2_owner_map"]([row], "chiap08", set()) == (
        {card_id: "unassigned:seat-pin-conflict:niobe:chiap03"},
        {card_id: "seat-pin-conflict:niobe:chiap03"},
    )


def test_pool_owner_map_preserves_ordinary_and_seat_ownership(tmp_path: Path) -> None:
    ns, labels = _load(str(tmp_path), {"link": ("chiap08",), "niobe": ("chiap08",)})
    labels["00000001"] = ["seat-link"]
    rows = [
        [2, 4, "00000001", {}, [], 0],
        [2, 4, "00000002", {}, [], 0],
        [2, 4, "00000003", {}, [], 0],
    ]
    ns["_POOL_V2_ADMISSIONS"] = {}

    owners, blocked = ns["_pool_v2_owner_map"](rows, "chiap08", {"00000003"})

    assert owners["00000001"] == "chiap08"
    assert owners["00000002"] == ns["_partition_owner"]("00000002", HOSTS)
    assert owners["00000003"] == "chiap08"
    assert blocked == {}

    capacity_owners, blocked = ns["_pool_v2_owner_map"](
        rows,
        "chiap08",
        {"00000003"},
        {"chiap03": 5, "chiap08": 0},
    )
    assert capacity_owners["00000001"] == "chiap08"
    assert capacity_owners["00000003"] == "chiap08"
    assert blocked == {}


def test_host_neutral_owner_uses_existing_live_free_capacity(tmp_path: Path) -> None:
    """A free host owns work that the stable all-host hash strands elsewhere."""
    ns, _ = _load(str(tmp_path))
    card_id = "cdf59956"
    rows = [[2, 4, card_id, {}, [], 0]]
    ns["_POOL_V2_ADMISSIONS"] = {}

    assert ns["_partition_owner"](card_id, HOSTS) == "chiap03"
    assert ns["_pool_v2_owner_map"](
        rows,
        "chiap08",
        set(),
        {"chiap03": 0, "chiap08": 5},
    ) == ({card_id: "chiap08"}, {})


def test_host_neutral_capacity_keeps_equal_and_all_zero_partitioning(tmp_path: Path) -> None:
    """Equal positive capacity and no capacity preserve the stable host bucket."""
    ns, _ = _load(str(tmp_path))
    card_id = "cdf59956"
    rows = [[2, 4, card_id, {}, [], 0]]
    ns["_POOL_V2_ADMISSIONS"] = {}
    expected = ns["_partition_owner"](card_id, HOSTS)

    assert (
        ns["_pool_v2_owner_map"](
            rows,
            "chiap08",
            set(),
            {host: 2 for host in HOSTS},
        )[
            0
        ][card_id]
        == expected
    )
    assert (
        ns["_pool_v2_owner_map"](
            rows,
            "chiap08",
            set(),
            {host: 0 for host in HOSTS},
        )[
            0
        ][card_id]
        == expected
    )


def test_runtime_places_seats_before_generic_partitioning() -> None:
    source = Path(SRC).read_text(encoding="utf-8")
    ownership = source.index("_OWNER_BY_ID, _SEAT_BLOCKED = _pool_v2_owner_map(")
    assert source.index("seat_for(cid, core)", source.index("def _pool_v2_owner_map")) < ownership
    assert ownership < source.index("owned=[x for x in pool")
    assert "SEAT_PLACEMENT_BLOCKED|%s|%s|%s" in source
    assert "falling back to lane naming" not in source


def main():
    home = tempfile.mkdtemp()
    ns, labels = _load(home, {"link": ("chiap08",), "mero": ("chiap08",)})
    seat_for = ns["seat_for"]

    cases = [
        (["seat-link", "trunk"], "link", "provisioned seat is used"),
        (["seat-mero"], "mero", "the mechanism is generic, not link-specific"),
        (["trunk", "integrator"], None, "no seat label means lane naming, unchanged"),
        (["SEAT-Link"], "link", "case is normalised, labels are not case sensitive"),
        (["seat-lnik"], "lnik", "valid seat requests reach fail-closed placement"),
        (["seat-lumina"], "lumina", "missing seats never fall back to a lane"),
        (["seat-"], None, "empty seat name is rejected, not interpolated"),
        (["seat-a b"], None, "a space would reach a shell command line: rejected"),
        (["seat-x;rm -rf /"], None, "shell metacharacters are rejected"),
        (["seat-../../etc"], None, "path traversal is rejected"),
        (["seat-" + "z" * 40], None, "over-long seat name is rejected"),
        (["seat-9lives"], None, "must start with a letter"),
        (["seat-lnik", "seat-link"], "lnik", "first valid seat label wins"),
        (["seat-mero", "seat-link"], "mero", "first valid label wins, deterministically"),
    ]
    failed = 0
    for i, (labs, want, why) in enumerate(cases):
        cid = "card%02d" % i
        labels[cid] = labs
        got = seat_for(cid, {})
        ok = got == want
        failed += not ok
        print(
            "  %-30s got=%-6s want=%-6s %s  %s"
            % (str(labs)[:30], got, want, "PASS" if ok else "FAIL", why)
        )
    print("FAILED" if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
