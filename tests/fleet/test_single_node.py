"""Right-sized complexity invariant (spec 3.6): 1 box works, zero costs zero."""

from __future__ import annotations

from skcapstone.fleet import store

ALL_KINDS = ["node", "service", "cronjob", "agent", "modelserver", "config"]


def test_zero_object_kinds_cost_nothing(paths) -> None:
    for kind in ALL_KINDS:
        assert store.list_specs(paths, kind) == []
        assert store.merged(paths, kind, "anything") is None
    assert not paths.root.exists()  # reads created no directories at all


def test_one_box_fleet_is_complete(paths, operator) -> None:
    solo = store.Writer(role="sknoded", node="node-solo", identity="")
    store.write_spec(paths, "node", "node-solo", {"cordoned": False}, writer=operator)
    store.write_node_file(paths, solo, "heartbeat.json", {"ts": "t"}, if_changed=False)
    store.write_status(
        paths,
        "node",
        "node-solo",
        node="node-solo",
        status={"capacity": {"cores": 4}},
        conditions=[],
        observed_generation=1,
        writer=solo,
    )
    m = store.merged(paths, "node", "node-solo")
    assert m["spec"]["generation"] == 1
    assert m["statuses"][0]["stale"] is False
    # the whole tree is exactly the files this one node needs, nothing more
    files = sorted(str(p.relative_to(paths.root)) for p in paths.root.rglob("*") if p.is_file())
    assert files == [
        "objects/node/node-solo.json",
        "status/node-solo/heartbeat.json",
        "status/node-solo/node/node-solo.json",
    ]


def test_kinds_with_zero_objects_stay_no_op_after_use(paths, operator) -> None:
    store.write_spec(paths, "node", "node-solo", {}, writer=operator)
    assert store.list_specs(paths, "service") == []
    assert not (paths.objects / "service").exists()
    assert not paths.placements.exists()
