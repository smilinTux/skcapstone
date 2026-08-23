"""`skfleet label` merges; `skfleet apply` replaces (drill gap G1).

Labels are what the scheduler actually filters on. `scheduler.feasible` never
reads `spec.role`, so a node's labels decide whether anything can be placed on
it, and until now there was no way to change one without risking the rest of
the object: the only available tool was `skfleet apply`, which replaces the
whole spec from the supplied document.

During the promotion drill a label-only apply silently dropped `taints`,
`cordoned` and `address`, un-cordoning the node, exit 0. The documented fix
for a label corrupted the spec it was fixing.
"""

from __future__ import annotations

import pytest

from skcapstone.fleet import node_controller, store

FULL_SPEC = {
    "role": "builder-standby",
    "cordoned": True,
    "taints": [{"key": "travel", "value": "true", "effect": "NoSchedule"}],
    "address": {"tailscale": "100.86.156.5"},
    "identity": "capauth:node-41@skworld.io",
}


@pytest.fixture
def node(paths, operator):
    store.write_spec(
        paths, "node", "node-41", dict(FULL_SPEC), writer=operator, labels={"heavy-build": "true"}
    )
    return "node-41"


def test_adding_a_label_preserves_every_other_spec_field(paths, operator, node) -> None:
    """The load-bearing assertion: this is exactly what apply got wrong."""
    spec = node_controller.set_labels(paths, node, add={"gpu": "true"}, writer=operator)
    assert spec["labels"] == {"heavy-build": "true", "gpu": "true"}
    for field, expected in FULL_SPEC.items():
        assert spec["spec"][field] == expected, f"{field} was lost by a label-only change"
    assert spec["spec"]["cordoned"] is True, "a label change must never un-cordon a node"


def test_removing_a_label_preserves_every_other_spec_field(paths, operator, node) -> None:
    spec = node_controller.set_labels(paths, node, remove=("heavy-build",), writer=operator)
    assert spec["labels"] == {}
    assert spec["spec"] == FULL_SPEC


def test_add_overwrites_an_existing_key(paths, operator, node) -> None:
    spec = node_controller.set_labels(paths, node, add={"heavy-build": "false"}, writer=operator)
    assert spec["labels"] == {"heavy-build": "false"}


def test_removing_an_absent_key_is_a_silent_no_op(paths, operator, node) -> None:
    spec = node_controller.set_labels(paths, node, remove=("never-set",), writer=operator)
    assert spec["labels"] == {"heavy-build": "true"}


def test_the_same_key_in_add_and_remove_is_refused(paths, operator, node) -> None:
    """Resolving this silently either way would make the result order-dependent."""
    with pytest.raises(ValueError, match="both"):
        node_controller.set_labels(
            paths, node, add={"gpu": "true"}, remove=("gpu",), writer=operator
        )


@pytest.mark.parametrize("bad", ["../escape", "UPPER", "", "has/slash"])
def test_invalid_label_keys_are_refused(paths, operator, node, bad) -> None:
    with pytest.raises(ValueError):
        node_controller.set_labels(paths, node, add={bad: "true"}, writer=operator)


def test_generation_bumps_by_exactly_one(paths, operator, node) -> None:
    before = store.read_spec(paths, "node", node)["generation"]
    spec = node_controller.set_labels(paths, node, add={"gpu": "true"}, writer=operator)
    assert spec["generation"] == before + 1


def test_labelling_an_unknown_node_raises(paths, operator) -> None:
    with pytest.raises(LookupError):
        node_controller.set_labels(paths, "no-such-node", add={"gpu": "true"}, writer=operator)


def test_a_promoted_seat_becomes_schedulable_once_labelled(paths, operator, node) -> None:
    """The end-to-end point of the card: role alone does not place work.

    26 live objects select on always-on or control-plane. Setting role=control
    changes nothing about placement; labelling is what makes the promoted seat
    feasible. This asserts both halves so a regression in either is visible.
    """
    from skcapstone.fleet.scheduler import NodeView, Workload, feasible

    def view(labels):
        return NodeView(
            name=node,
            phase="Ready",
            labels=labels,
            taints=(),
            cordoned=False,
            capacity={"cores": 20, "ram_gb": 24.4, "disk_gb": 735.5},
            allocatable={"cores": 19, "ram_gb": 23.0, "disk_gb": 700.0},
            heartbeat_age_s=1.0,
        )

    workload = Workload(
        kind="cronjob",
        name="skcapstone-backup-gfs",
        node_selector={"control-plane": "true"},
        tolerations=(),
        requests={},
    )

    node_controller.set_role(paths, node, "control", writer=operator)
    assert feasible(view({"heavy-build": "true"}), workload) is not None, (
        "role=control alone should NOT make the seat feasible; if this passes, "
        "the scheduler started reading spec.role and this card's premise changed"
    )

    spec = node_controller.set_labels(paths, node, add={"control-plane": "true"}, writer=operator)
    assert feasible(view(spec["labels"]), workload) is None
