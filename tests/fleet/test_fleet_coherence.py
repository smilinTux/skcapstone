"""Do the estate's nodes agree with EACH OTHER about what they run?

detect_drift answers "is this node internally consistent" and structurally
cannot answer this one: a node whose checkout, package and deployed
artifacts all agree with each other is perfectly self-consistent while
being the only host in the estate on last week's commit. On 2026-09-19 all
five chi hosts sat on a commit none of them had any way to notice was not
the one being rolled out.

Answered from the rollout history every node already publishes to its own
node-scoped path under the one Syncthing folder the estate shares. No ssh,
no new publishing step, and the findings come back as ordinary Drift
records so every existing reader handles them unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

from skcapstone.fleet.rollout_drift import detect_fleet_incoherence
from skcapstone.fleet.rollout_history import current_manifest_by_node


def _record(home: Path, node: str, *entries: dict) -> None:
    path = home / ".skcapstone" / "fleet" / "status" / f"node-{node}" / "rollout" / "history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in entries))


def _fleet(home: Path, **node_to_sha: str) -> None:
    for node, sha in node_to_sha.items():
        _record(home, node, {"git_sha": sha, "units": []})


def test_agreeing_fleet_reports_nothing(tmp_path):
    _fleet(tmp_path, chiap01="deadbeef", chiap02="deadbeef", chiap03="deadbeef")
    assert detect_fleet_incoherence(tmp_path) == []


def test_one_stale_node_is_named(tmp_path):
    _fleet(
        tmp_path, chiap01="deadbeef", chiap02="deadbeef", chiap03="deadbeef", chiap04="0bad0bad"
    )

    drifts = detect_fleet_incoherence(tmp_path)

    assert len(drifts) == 1
    assert drifts[0].artifact == "fleet:git_sha"
    assert drifts[0].host == "chiap04", "the finding must name the node it is ABOUT"
    assert drifts[0].expected == "deadbeef"
    assert drifts[0].found == "0bad0bad"


def test_the_answer_does_not_depend_on_which_host_asks(tmp_path, tmp_path_factory):
    """Plurality, not "compare everyone to me".

    A "compare peers to this host" rule would report chiap01..03 as drifted
    when run FROM chiap04 and chiap04 as drifted when run from anywhere
    else, so two operators would get two contradictory answers about the
    same estate. Every host shares the same tree, so every host must reach
    the same conclusion from it.
    """
    _fleet(
        tmp_path, chiap01="deadbeef", chiap02="deadbeef", chiap03="deadbeef", chiap04="0bad0bad"
    )
    first = detect_fleet_incoherence(tmp_path)

    other_home = tmp_path_factory.mktemp("other")
    _fleet(
        other_home,
        chiap01="deadbeef",
        chiap02="deadbeef",
        chiap03="deadbeef",
        chiap04="0bad0bad",
    )
    second = detect_fleet_incoherence(other_home)

    assert [(d.host, d.expected, d.found) for d in first] == [
        (d.host, d.expected, d.found) for d in second
    ]


def test_an_even_split_is_deterministic(tmp_path):
    """A tie must not be resolved by dict ordering."""
    _fleet(
        tmp_path, chiap01="aaaaaaaa", chiap02="aaaaaaaa", chiap03="bbbbbbbb", chiap04="bbbbbbbb"
    )

    first = detect_fleet_incoherence(tmp_path)
    second = detect_fleet_incoherence(tmp_path)

    assert first == second
    assert {d.host for d in first} == {"chiap03", "chiap04"}
    assert all(d.expected == "aaaaaaaa" for d in first)


def test_a_node_that_never_deployed_is_not_reported(tmp_path):
    """Never having deployed is a different fact from having deployed the
    wrong thing, and this must not invent the difference."""
    _fleet(tmp_path, chiap01="deadbeef", chiap02="deadbeef")
    (tmp_path / ".skcapstone" / "fleet" / "status" / "node-chiap09" / "rollout").mkdir(
        parents=True
    )

    assert detect_fleet_incoherence(tmp_path) == []
    assert "chiap09" not in current_manifest_by_node(tmp_path)


def test_one_node_cannot_disagree_with_itself(tmp_path):
    _fleet(tmp_path, chiap01="deadbeef")
    assert detect_fleet_incoherence(tmp_path) == []


def test_an_empty_estate_is_not_an_error(tmp_path):
    assert detect_fleet_incoherence(tmp_path) == []
    assert current_manifest_by_node(tmp_path) == {}


def test_the_newest_entry_wins_even_after_a_rollback(tmp_path):
    """What a node is running now is its LAST entry, however it got there."""
    _record(
        tmp_path,
        "chiap01",
        {"git_sha": "deadbeef", "units": []},
        {"git_sha": "0bad0bad", "kind": "rollback", "units": []},
    )
    _fleet(tmp_path, chiap02="deadbeef", chiap03="deadbeef")

    drifts = detect_fleet_incoherence(tmp_path)

    assert [d.host for d in drifts] == ["chiap01"]
    assert drifts[0].found == "0bad0bad"


def test_a_corrupt_line_costs_only_itself(tmp_path):
    path = (
        tmp_path
        / ".skcapstone"
        / "fleet"
        / "status"
        / "node-chiap01"
        / "rollout"
        / "history.jsonl"
    )
    path.parent.mkdir(parents=True)
    path.write_text('{"git_sha": "deadbeef", "units": []}\nnot json at all\n')
    _fleet(tmp_path, chiap02="deadbeef")

    # The corrupt tail is skipped, so chiap01's last VALID entry still counts.
    assert current_manifest_by_node(tmp_path)["chiap01"]["git_sha"] == "deadbeef"
    assert detect_fleet_incoherence(tmp_path) == []
