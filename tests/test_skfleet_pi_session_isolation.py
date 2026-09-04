"""Pi conversation state is isolated from stable fleet ownership."""

import ast
import re
from pathlib import Path

import pytest

ROTATE = Path(__file__).parents[1] / "scripts/fleet/skfleet-rotate.py"
REVISION_A = "a" * 32
REVISION_B = "b" * 32


def helper():
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "_pi_conversation_name"
    )
    namespace = {"re": re}
    exec(compile(ast.Module([node], []), str(ROTATE), "exec"), namespace)
    return namespace["_pi_conversation_name"]


def test_same_card_retry_uses_distinct_conversation_identity():
    make = helper()
    first = make("chiap01", "364a5f47", "glm", REVISION_A)
    second = make("chiap01", "364a5f47", "glm", REVISION_B)
    assert first != second
    assert first == "pi-run-chiap01-364a5f47-glm-" + REVISION_A


@pytest.mark.parametrize("lane", ["qwen", "glm", "codex", "escalate"])
def test_every_lane_is_generation_bound(lane):
    assert helper()("chiap08", "1234abcd", lane, REVISION_A).endswith(REVISION_A)


def test_host_card_and_lane_each_separate_conversations():
    make = helper()
    baseline = make("chiap01", "1234abcd", "codex", REVISION_A)
    variants = {
        make("chiap02", "1234abcd", "codex", REVISION_A),
        make("chiap01", "87654321", "codex", REVISION_A),
        make("chiap01", "1234abcd", "glm", REVISION_A),
    }
    assert baseline not in variants
    assert len(variants) == 3


@pytest.mark.parametrize(
    "values",
    [
        ("bad host", "1234abcd", "codex", REVISION_A),
        ("chiap01", "../card", "codex", REVISION_A),
        ("chiap01", "1234abcd", "bad/lane", REVISION_A),
        ("chiap01", "1234abcd", "codex", "short"),
    ],
)
def test_malformed_identity_fails_before_launch(values):
    with pytest.raises(ValueError):
        helper()(*values)


def test_launch_keeps_stable_owner_and_seat_attribution():
    source = ROTATE.read_text(encoding="utf-8")
    assert "env SKAGENT=%s SKCAPSTONE_AGENT=%s SKFLEET_WORKSPACE=%s" in source
    assert "PI,\n              conversation_name,model" in source
    assert 'claim=subprocess.run([SKC,"coord","claim",cid,"--agent",name]' in source
    assert '"--owner",name' in source
    assert 'name = _worker_owner(_LANE["name"], cid, _seat)' in source


def test_conversation_name_is_built_only_after_exact_claim_readback():
    source = ROTATE.read_text(encoding="utf-8")
    claim = source.index('claim=subprocess.run([SKC,"coord","claim"')
    readback = source.index("claimed_revision=_current_claim_identity_fresh(cid)", claim)
    conversation = source.index("conversation_name = _pi_conversation_name", readback)
    launch = source.index("inner=shlex.join", conversation)
    assert claim < readback < conversation < launch
