"""Released custody remains usable across the real guarded authority CLI."""

import pytest

from skcapstone.fleet import production_review_finish as finish
from tests.fleet.test_production_review_finish import pair  # noqa: F401
from tests.fleet.test_released_source_acceptance import release
from tests.fleet.test_source_bundle import source  # noqa: F401


@pytest.mark.host_systemd
@pytest.mark.parametrize("applied", [False, True])
def test_pending_source_link_accepts_only_release_and_finishes_once(pair, applied):  # noqa: F811
    home, directory, context, store, *_ = pair
    stopped = []

    def command(home, args):
        if args[:3] == ["link", context["source"]["card"], "verdict"] and not stopped:
            stopped.append(True)
            if applied:
                finish.native_command(home, args)
            raise OSError("interrupted source PASS")
        return finish.native_command(home, args)

    with pytest.raises(OSError, match="interrupted source PASS"):
        finish.finish_pair(home, directory, context, guard=lambda: None, command=command)
    assert store.fold(context["review"]["card"]).status.value == "done"
    release(pair)
    result = finish.finish_pair(home, directory, context, guard=lambda: None)
    assert result["accepted"]
    events = {role: store._read_events(context[role]["card"]) for role in ("source", "review")}
    assert (
        sum(
            e.get("link_key") == "verdict" and e.get("link_value") == "PASS"
            for e in events["source"]
        )
        == 1
    )
    assert all(sum(e["action"] == "complete" for e in rows) == 1 for rows in events.values())
    assert finish.finish_pair(home, directory, context, guard=lambda: None) == result
    assert all(store._read_events(context[role]["card"]) == rows for role, rows in events.items())
