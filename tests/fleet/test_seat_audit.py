"""Two-seat detection by writer provenance, not by collision (drill gap G2).

The Syncthing conflict file is a COLLISION detector. Measured in the promotion
drill (card 4c32df6f): two seats writing inside one sync interval produce 1
conflict file; the same two seats with a sync between the writes produce 10
writes and ZERO conflict files. The interleaved case is the likely one, so a
quiet conflict directory is not evidence of a single writer.
"""

from __future__ import annotations

import json

from skcapstone.fleet import seat_audit, store


def _write(paths, kind, name, *, node, role="operator"):
    return store.write_spec(
        paths, kind, name, {}, writer=store.Writer(role=role, node=node, identity="")
    )


def test_a_single_seat_reads_clean(paths) -> None:
    _write(paths, "node", "node-a", node="node-noroc2027")
    _write(paths, "service", "skgateway", node="node-noroc2027")
    audit = seat_audit.audit_seats(paths)
    assert audit.ok
    assert audit.seats == ["node-noroc2027"]
    assert "one operator seat" in audit.summary()


def test_a_second_seat_is_detected(paths) -> None:
    """The case `find` cannot see: two seats, no conflict file anywhere."""
    _write(paths, "node", "node-a", node="node-noroc2027")
    _write(paths, "node", "node-b", node="node-41")
    assert not list((paths.objects / "node").glob("*.sync-conflict-*")), (
        "this scenario must contain NO conflict file, or it is not testing "
        "the gap the collision detector already covers"
    )
    audit = seat_audit.audit_seats(paths)
    assert not audit.ok
    assert audit.seats == ["node-41", "node-noroc2027"]
    assert "2 operator seats" in audit.summary()


def test_the_store_itself_refuses_a_non_operator_spec_write(paths) -> None:
    """Worth pinning: this is the first line of defence, ahead of the audit."""
    import pytest

    with pytest.raises(store.OwnershipError):
        _write(paths, "node", "node-b", node="node-41", role="sknoded")


def test_non_operator_writers_are_not_counted_as_seats(paths) -> None:
    """A non-operator writer block can still ARRIVE, even though write_spec
    refuses to create one: over Syncthing from a node running older code, or
    by a hand edit. Counting those as seats would report a normal fleet as a
    two-seat emergency, and a signal that cries wolf gets ignored.

    Written directly rather than through write_spec, because write_spec
    correctly refuses the role (asserted just above).
    """
    _write(paths, "node", "node-a", node="node-noroc2027")
    real = paths.spec_path("node", "node-a")
    payload = json.loads(real.read_text(encoding="utf-8"))
    for name, role, node in (
        ("node-b", "sknoded", "node-41"),
        ("node-c", "scheduler", "node-100"),
    ):
        other = dict(payload, name=name, writer=dict(payload["writer"], role=role, node=node))
        real.with_name(f"{name}.json").write_text(json.dumps(other), encoding="utf-8")
    audit = seat_audit.audit_seats(paths)
    assert audit.ok
    assert audit.seats == ["node-noroc2027"]


def test_a_conflict_copy_cannot_inflate_the_seat_count(paths) -> None:
    """The audit reports on objects the fleet OBEYS, not on discarded copies."""
    _write(paths, "node", "node-a", node="node-noroc2027")
    real = paths.spec_path("node", "node-a")
    payload = json.loads(real.read_text(encoding="utf-8"))
    payload["writer"]["node"] = "node-41"
    real.with_name("node-a.sync-conflict-20260816-120000-CIHSBZ4.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    audit = seat_audit.audit_seats(paths)
    assert audit.ok, "a discarded conflict copy was counted as a live second seat"
    assert audit.seats == ["node-noroc2027"]


def test_objects_with_no_writer_block_are_reported_separately(paths) -> None:
    """Unattributed is not the same as clean, and must not read as clean."""
    _write(paths, "node", "node-a", node="node-noroc2027")
    real = paths.spec_path("node", "node-a")
    payload = json.loads(real.read_text(encoding="utf-8"))
    payload.pop("writer")
    real.write_text(json.dumps(payload), encoding="utf-8")
    audit = seat_audit.audit_seats(paths)
    assert audit.unattributed == ["node/node-a"]
    assert audit.by_node == {}


def test_an_empty_tree_does_not_crash(paths) -> None:
    audit = seat_audit.audit_seats(paths)
    assert audit.ok
    assert audit.summary() == "no operator-seat writes found"
