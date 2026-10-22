"""A retry must requalify terminal source before another native mutation."""

import pytest

from tests.test_fiber_lifecycle_connection import Refused
from tests.test_fiber_lifecycle_connection import rig as rig


@pytest.mark.parametrize("change", ["commit", "execution", "accepted", "restarted"])
@pytest.mark.parametrize("acknowledged", [False, True])
def test_recovered_intent_refuses_new_terminal_state(rig, change, acknowledged):
    """Retained intent is not a replacement for the current unit and candidate."""
    r = rig

    def interrupted(intent):
        if acknowledged:
            r["record"](intent)
        raise Refused("synthetic interrupted write")

    with pytest.raises(Refused, match="interrupted write"):
        r["run"](record_verdict=interrupted)
    before = r["store"]._read_events(r["card"])

    def changed(run):
        proof = r["collect"](run)
        if change == "commit":
            proof["candidate"]["commit"] = "7" * 40
        elif change == "execution":
            proof["execution_sha256"] = "7" * 64
        elif change == "accepted":
            proof["accepted"] = True
        else:
            raise Refused("candidate-not-exact-terminal")
        return proof

    with pytest.raises(Refused, match="recovered-candidate-changed|not-exact-terminal"):
        r["run"](collect_candidate=changed)
    assert r["store"]._read_events(r["card"]) == before
    assert r["store"].list_card_ids() == [r["card"]]
