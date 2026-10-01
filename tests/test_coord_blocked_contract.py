"""Both native verdict write paths enforce the controller's BLOCKED contract."""

import pytest

from tests.test_guarded_candidate_verdict import setup_guard, verdicts
from tests.test_provisional_verdict_producer import _run


@pytest.mark.parametrize("guarded", [False, True])
@pytest.mark.parametrize(
    "outcome",
    [
        "BLOCKED",
        "BLOCKED blocked_on=git_identity referent=git user.name absent",
        "BLOCKED blocked_on=capability",
    ],
)
def test_invalid_blocked_never_writes_event(tmp_path, guarded, outcome):
    store, card, owner, artifact, args = setup_guard(tmp_path)
    args[2] = outcome
    if not guarded:
        args = args[: args.index("--expected-source-revision")]
    before = store.fold(card)
    result = _run(tmp_path, *args)
    assert result.exit_code != 0
    assert "blocked_on" in result.output
    assert verdicts(tmp_path, card) == []
    assert store.fold(card).owner == before.owner
    assert store.fold(card).meta["_claim_revision"] == before.meta["_claim_revision"]


@pytest.mark.parametrize("guarded", [False, True])
def test_valid_capability_blocked_preserves_typed_candidate(tmp_path, guarded):
    store, card, owner, artifact, args = setup_guard(tmp_path)
    args[2] = "BLOCKED blocked_on=capability referent=git-author-identity unavailable"
    if not guarded:
        args = args[: args.index("--expected-source-revision")]
    result = _run(tmp_path, *args)
    assert result.exit_code == 0, result.output
    assert verdicts(tmp_path, card)[0]["verdict"] == args[2]
    assert verdicts(tmp_path, card)[0]["candidate_commit"] == "a" * 40
