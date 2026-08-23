"""A Syncthing conflict copy must never be READ as spec (drill gap G3).

Syncthing writes `<stem>.sync-conflict-<ts>-<device>.json` beside a file when
two nodes edit it between syncs. That name matches `*.json`, so a naive glob
loads it, and because the readers key on the `name` field INSIDE the payload
rather than on the filename, the conflict copy overwrites the real object.

The store then serves the version Syncthing DISCARDED. Found by executing the
promotion runbook: a cordoned node read as schedulable and a demoted node read
as control.
"""

from __future__ import annotations

import json

from skcapstone.fleet import node_controller, store


def _conflict_beside(paths, kind: str, name: str, **overrides) -> None:
    """Drop a conflict sibling carrying different spec values."""
    real = paths.spec_path(kind, name)
    payload = json.loads(real.read_text(encoding="utf-8"))
    payload["spec"].update(overrides)
    real.with_name(f"{name}.sync-conflict-20260816-120000-CIHSBZ4.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_a_conflict_copy_does_not_override_the_real_object(paths, operator) -> None:
    store.write_spec(
        paths, "node", "node-x", {"role": "builder-standby", "cordoned": True}, writer=operator
    )
    _conflict_beside(paths, "node", "node-x", role="control", cordoned=False)

    views = {v.name: v for v in node_controller.node_views(paths)}
    assert len(views) == 1, "the conflict copy was counted as a second node"
    assert views["node-x"].role == "builder-standby"
    assert views["node-x"].cordoned is True, "a cordoned node read as schedulable"


def test_list_specs_skips_conflict_copies(paths, operator) -> None:
    store.write_spec(paths, "node", "node-x", {"role": "control"}, writer=operator)
    _conflict_beside(paths, "node", "node-x", role="worker-gpu")
    specs = store.list_specs(paths, "node")
    assert len(specs) == 1
    assert specs[0]["spec"]["role"] == "control"


def test_the_conflict_file_is_still_present_on_disk(paths, operator) -> None:
    """Skipping it is not deleting it. The SyncConflict condition reports these,
    so they must remain visible as a FINDING while ceasing to be obeyed as DATA."""
    store.write_spec(paths, "node", "node-x", {"role": "control"}, writer=operator)
    _conflict_beside(paths, "node", "node-x", role="worker-gpu")
    assert list((paths.objects / "node").glob("*.sync-conflict-*"))


def test_a_normal_name_containing_json_is_not_mistaken_for_a_conflict(paths, operator) -> None:
    """Negative control on the matcher: only the conflict marker is skipped."""
    store.write_spec(paths, "node", "node-x", {"role": "control"}, writer=operator)
    store.write_spec(paths, "node", "node-y", {"role": "worker-gpu"}, writer=operator)
    assert len(store.list_specs(paths, "node")) == 2


def test_placements_also_skip_conflict_copies(paths) -> None:
    scheduler_writer = store.Writer(role="scheduler", node="node-x", identity="")
    store.write_placement(
        paths, "service", "svc", node="node-x", reason="test", writer=scheduler_writer
    )
    real = paths.placement_path("service", "svc")
    payload = json.loads(real.read_text(encoding="utf-8"))
    payload["node"] = "node-wrong"
    real.with_name("svc.sync-conflict-20260816-120000-CIHSBZ4.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    records = store.list_placements(paths, "service")
    assert len(records) == 1
    assert records[0]["node"] == "node-x"
