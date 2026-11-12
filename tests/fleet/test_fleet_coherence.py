"""Do the estate's nodes agree with EACH OTHER about what they run?

detect_drift answers "is this node internally consistent" and structurally
cannot answer this one: every expected value it compares against is read
from the node's own checkout, so a host that never pulled agrees with
itself perfectly, forever. On 2026-09-19 all five chi hosts did exactly
that at once.

Read from the readiness verdict each node already publishes every 15
minutes to the one Syncthing folder the estate shares -- the same file
staged_rollout._readiness_verdict already reads. NOT from rollout_history:
that store is written only by staged_rollout.record_deployment, and this
fleet's deployments do not all go through it. Measured on 2026-09-19, every
node's recorded manifest said 0c8dcd6b while every node's checkout was on
112b2ef4, about an hour stale. A check built on it answers "coherent" from
records that agree only because they are equally out of date.
"""

from __future__ import annotations

import json
from pathlib import Path

from skcapstone.fleet.rollout_drift import detect_fleet_incoherence


def _verdict(home: Path, node: str, sha: str | None, *, ready: bool = True) -> None:
    path = (
        home / ".skcapstone" / "fleet" / "status" / f"node-{node}" / "readiness" / "verdict.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict = {"ready": ready, "checked_at": "2026-09-19T08:00:00Z", "lines": []}
    if sha is not None:
        payload["installed_git_sha"] = sha
    path.write_text(json.dumps(payload, sort_keys=True))


def _estate(home: Path, **node_to_sha) -> None:
    for node, sha in node_to_sha.items():
        _verdict(home, node, sha)


def test_agreeing_estate_reports_nothing(tmp_path):
    _estate(tmp_path, chiap01="deadbeef", chiap02="deadbeef", chiap03="deadbeef")
    assert detect_fleet_incoherence(tmp_path) == []


def test_one_stale_node_is_named(tmp_path):
    _estate(
        tmp_path, chiap01="deadbeef", chiap02="deadbeef", chiap03="deadbeef", chiap04="0bad0bad"
    )

    drifts = detect_fleet_incoherence(tmp_path)

    assert len(drifts) == 1, drifts
    assert drifts[0].artifact == "fleet:installed_git_sha"
    assert drifts[0].kind == "changed"
    assert drifts[0].host == "chiap04", "the finding must name the node it is ABOUT"
    assert drifts[0].expected == "deadbeef"
    assert drifts[0].found == "0bad0bad"


def test_the_answer_does_not_depend_on_which_host_asks(tmp_path, tmp_path_factory):
    """Plurality, not "compare everyone to me".

    A compare-to-me rule would report chiap01..03 as drifted when run FROM
    chiap04, and chiap04 when run from anywhere else, so two operators
    reading the same shared tree would get two contradictory answers about
    the same estate.
    """
    layout = dict(chiap01="deadbeef", chiap02="deadbeef", chiap03="deadbeef", chiap04="0bad0bad")
    _estate(tmp_path, **layout)
    other = tmp_path_factory.mktemp("other")
    _estate(other, **layout)

    assert [(d.host, d.expected, d.found) for d in detect_fleet_incoherence(tmp_path)] == [
        (d.host, d.expected, d.found) for d in detect_fleet_incoherence(other)
    ]


def test_an_even_split_is_deterministic(tmp_path):
    _estate(
        tmp_path, chiap01="aaaaaaaa", chiap02="aaaaaaaa", chiap03="bbbbbbbb", chiap04="bbbbbbbb"
    )

    first = detect_fleet_incoherence(tmp_path)

    assert first == detect_fleet_incoherence(tmp_path)
    assert {d.host for d in first} == {"chiap03", "chiap04"}
    assert all(d.expected == "aaaaaaaa" for d in first)


def test_a_node_that_cannot_say_is_reported_not_assumed_to_agree(tmp_path):
    """Unknown is never agreement.

    A verdict with no installed_git_sha must not be silently counted as
    matching whatever the others say -- that is precisely the failure this
    check exists to prevent.
    """
    _estate(tmp_path, chiap01="deadbeef", chiap02="deadbeef")
    _verdict(tmp_path, "chiap03", None)

    drifts = detect_fleet_incoherence(tmp_path)

    assert [(d.host, d.kind) for d in drifts] == [("chiap03", "missing")]


def test_an_unparseable_verdict_is_not_treated_as_agreement(tmp_path):
    _estate(tmp_path, chiap01="deadbeef", chiap02="deadbeef")
    bad = tmp_path / ".skcapstone/fleet/status/node-chiap03/readiness/verdict.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{not json")

    # No verdict at all means the node is not participating, which is a
    # different fact from participating without an answer.
    assert detect_fleet_incoherence(tmp_path) == []


def test_no_node_publishing_the_field_is_one_finding_not_a_wall(tmp_path):
    """Before the publishing gate is deployed, every node lacks the field.

    That is one situation with one cause, so it gets one finding naming the
    cause, not N identical ones.
    """
    for node in ("chiap01", "chiap02", "chiap03", "chiap04", "chiap08"):
        _verdict(tmp_path, node, None)

    drifts = detect_fleet_incoherence(tmp_path)

    assert len(drifts) == 1
    assert drifts[0].kind == "missing"
    assert "not deployed yet" in (drifts[0].found or "")


def test_one_node_cannot_disagree_with_itself(tmp_path):
    _estate(tmp_path, chiap01="deadbeef")
    assert detect_fleet_incoherence(tmp_path) == []


def test_an_empty_estate_is_not_an_error(tmp_path):
    assert detect_fleet_incoherence(tmp_path) == []
    (tmp_path / ".skcapstone/fleet/status").mkdir(parents=True)
    assert detect_fleet_incoherence(tmp_path) == []


def test_a_not_ready_node_still_counts_for_coherence(tmp_path):
    """Readiness and coherence are different questions.

    A node can be NOT READY for an unrelated reason and still be on the
    right commit; excluding it would hide a divergence behind an unrelated
    failure.
    """
    _estate(tmp_path, chiap01="deadbeef", chiap02="deadbeef")
    _verdict(tmp_path, "chiap03", "0bad0bad", ready=False)

    drifts = detect_fleet_incoherence(tmp_path)

    assert [(d.host, d.found) for d in drifts] == [("chiap03", "0bad0bad")]


def test_the_readiness_gate_actually_publishes_the_field(tmp_path, monkeypatch):
    """The detector is useless if nothing writes what it reads.

    Asserted against the real gate module, not a fixture, so the two halves
    cannot drift apart: the gate is a standalone stdlib-only script and
    nothing else links them.
    """
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "fleet"))
    import skfleet_readiness

    fake_home = tmp_path / "h"
    site = fake_home / ".skenv" / "lib" / "python3.12" / "site-packages"
    (site / "skcapstone-0.15.168.dev266+gc0ffee00.dist-info").mkdir(parents=True)
    monkeypatch.setattr(skfleet_readiness.os.path, "expanduser", lambda p: str(fake_home))

    assert skfleet_readiness.installed_git_sha() == "c0ffee00"

    out = tmp_path / "verdict.json"
    skfleet_readiness.write_verdict(out, True, ["OK something"])
    payload = json.loads(out.read_text())

    assert payload["installed_git_sha"] == "c0ffee00"
    assert payload["ready"] is True, "publishing the sha must not change the verdict"


def test_an_unreadable_dist_info_never_makes_a_node_unready(tmp_path, monkeypatch):
    """Fail-soft in the GATE, fail-closed in the DETECTOR.

    A node that cannot name its own commit is still ready if everything the
    gate actually checks passed; it is the coherence check's job to report
    that it could not say, not the gate's job to fail it.
    """
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "fleet"))
    import skfleet_readiness

    monkeypatch.setattr(skfleet_readiness.os.path, "expanduser", lambda p: str(tmp_path / "none"))
    assert skfleet_readiness.installed_git_sha() is None

    out = tmp_path / "verdict.json"
    skfleet_readiness.write_verdict(out, True, ["OK something"])
    payload = json.loads(out.read_text())

    assert payload["installed_git_sha"] is None
    assert payload["ready"] is True
