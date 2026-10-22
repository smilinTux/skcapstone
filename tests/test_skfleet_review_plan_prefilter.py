"""Historical review candidates must not repeatedly rebuild custody indexes."""

import ast
from pathlib import Path

import pytest

from tests.fleet.test_production_custody import review_candidate  # noqa: F401
from tests.fleet.test_source_bundle import source  # noqa: F401

ROTATE = Path(__file__).resolve().parents[1] / "scripts/fleet/skfleet-rotate.py"


def load_check(state, events):
    tree = ast.parse(ROTATE.read_text())
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_production_review_candidate_shape"
    ]
    namespace = {
        "Path": Path,
        "CARDS": "/unused/cards",
        "_authoritative_card_state": lambda _parent: ({}, state),
        "event_rows": lambda _parent: events,
    }
    if not nodes:
        # The baseline passes every historical candidate to the expensive plan.
        return lambda _parent, _candidate: True
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace["_production_review_candidate_shape"]


def test_remote_request_preserves_candidate_for_full_native_gate(tmp_path):
    state, _ = facts()
    check = load_check(state, [])
    check.__globals__["CARDS"] = str(tmp_path / "cards")
    request = tmp_path / "fleet" / "dispatch" / "node-a" / "abcd1234.json"
    request.parent.mkdir(parents=True)
    request.write_text('{"not_authorized": true}')
    assert check("abcd1234", ("producer",)) is True
    request.unlink()
    assert check("abcd1234", ("producer",)) is False


def facts():
    return (
        {
            "owner": "producer",
            "claim_revision": "claim-a",
            "status": "doing",
            "archived": False,
            "labels": ["source-only"],
        },
        [
            {
                "action": "production_assignment_launch",
                "schema": "skfleet.production-assignment-launch/v1",
                "writer": "producer",
                "worker": "producer",
                "claim_revision": "claim-a",
                "launched": True,
            }
        ],
    )


def test_historical_nonproduction_outcome_stops_before_custody_work():
    state, _ = facts()
    assert load_check(state, [])("abcd1234", ("producer",)) is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner", "other-producer"),
        ("claim_revision", "claim-b"),
        ("archived", True),
        ("status", "done"),
        ("labels", []),
        ("labels", ["source-only", "review"]),
    ],
)
def test_stale_or_ineligible_custody_never_enters_plan(field, value):
    state, events = facts()
    state[field] = value
    assert load_check(state, events)("abcd1234", ("producer",)) is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("worker", "other-producer"),
        ("writer", "other-producer"),
        ("claim_revision", "old-claim"),
        ("launched", False),
        ("schema", "unknown/v1"),
        ("action", "claim"),
    ],
)
def test_wrong_launch_generation_never_enters_plan(field, value):
    state, events = facts()
    events[0][field] = value
    assert load_check(state, events)("abcd1234", ("producer",)) is False


def test_exact_launch_remains_candidate_for_full_fresh_validation():
    state, events = facts()
    assert load_check(state, events)("abcd1234", ("producer",)) is True
    # Missing source binding does not make this prefilter an authorization gate.
    # The existing full plan must still validate binding, evidence and custody.
    tree = ast.parse(ROTATE.read_text())
    plan = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_production_review_plan"
    )
    calls = [
        n.func.id
        for n in ast.walk(plan)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    assert "reviewable_source_candidate" in calls


def test_prefilter_runs_before_gateway_construction():
    tree = ast.parse(ROTATE.read_text())
    plan = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "_production_review_plan"
    )
    calls = {
        n.func.id: n.lineno
        for n in ast.walk(plan)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert (
        calls.get("_production_review_candidate_shape", float("inf"))
        < calls["LiveCardStoreGateway"]
    )


@pytest.mark.parametrize("source", ["https"], indirect=True)
def test_real_remote_candidate_still_passes_native_custody_gate(
    review_candidate, tmp_path  # noqa: F811
):
    from tests.fleet.test_production_custody import candidate_allowed
    from tests.test_skfleet_provisional_opener import _namespace

    value = review_candidate
    rows = value["store"]._read_events(value["card"])
    ns = _namespace(value["home"] / "cards", tmp_path / "refusals")
    ns.update(
        Path=Path,
        PRODUCTION_POLICY=value["policy"],
        HOST="control",
        event_rows=lambda _card: rows,
        _card_process_snapshot=lambda _card: {"sessions": [], "units": []},
    )
    candidate = (
        value["owner"],
        str(value["shared"]),
        value["outcome"]["candidate_sha256"],
        value["head"],
        value["tree"],
        value["outcome"]["candidate_ref"],
    )
    assert ns["_production_review_candidate_shape"](value["card"], candidate)
    assert candidate_allowed(value)
    value["status"]["claim_revision"] = "f" * 32
    import json

    value["status_path"].write_text(json.dumps(value["status"]))
    assert ns["_production_review_candidate_shape"](value["card"], candidate)
    assert candidate_allowed(value) is False
