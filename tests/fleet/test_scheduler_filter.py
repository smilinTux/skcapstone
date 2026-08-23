"""Tests for scheduler v1 filtering (Ready, cordon, selector, taints, headroom)."""

from __future__ import annotations

from skcapstone.fleet.node_controller import NodeView
from skcapstone.fleet.scheduler import Workload, feasible

ALLOC = {"cores": 4, "ram_gb": 8.0, "disk_gb": 50.0}


def _view(
    name="node-a", phase="Ready", cordoned=False, labels=None, taints=None, alloc=None
) -> NodeView:
    return NodeView(
        name=name,
        phase=phase,
        cordoned=cordoned,
        labels=labels or {},
        taints=taints or [],
        capacity=dict(ALLOC),
        allocatable=alloc or dict(ALLOC),
    )


def _wl(**kw) -> Workload:
    return Workload(kind="job", name="card-1", **kw)


def test_ready_and_cordon_filters() -> None:
    assert feasible(_view(), _wl()) is None
    assert feasible(_view(phase="NotReady"), _wl()) == "not Ready (phase=NotReady)"
    assert feasible(_view(phase="Dead"), _wl()) == "not Ready (phase=Dead)"
    assert feasible(_view(phase="Pending"), _wl()) == "not Ready (phase=Pending)"
    assert feasible(_view(cordoned=True), _wl()) == "cordoned"


def test_node_selector_exact_match_and() -> None:
    view = _view(labels={"heavy-build": "true", "tier": "core"})
    assert feasible(view, _wl(node_selector={"heavy-build": "true"})) is None
    assert feasible(view, _wl(node_selector={"heavy-build": "true", "tier": "core"})) is None
    assert feasible(view, _wl(node_selector={"gpu": "true"})) == "selector mismatch (gpu=true)"
    assert feasible(view, _wl(node_selector={"tier": "edge"})) == "selector mismatch (tier=edge)"


def test_noschedule_taints_require_toleration() -> None:
    tainted = _view(
        taints=[{"key": "dedicated", "value": "model-serving", "effect": "NoSchedule"}]
    )
    assert feasible(tainted, _wl()) == "untolerated NoSchedule taint dedicated=model-serving"
    exact = _wl(tolerations=({"key": "dedicated", "value": "model-serving"},))
    assert feasible(tainted, exact) is None
    key_only = _wl(tolerations=({"key": "dedicated"},))
    assert feasible(tainted, key_only) is None  # key-only tolerates any value
    wrong = _wl(tolerations=({"key": "dedicated", "value": "other"},))
    assert feasible(tainted, wrong) == "untolerated NoSchedule taint dedicated=model-serving"


def test_prefernoschedule_is_advisory_in_v1() -> None:
    travel = _view(taints=[{"key": "travel", "value": "true", "effect": "PreferNoSchedule"}])
    assert feasible(travel, _wl()) is None  # correctness filters only (spec 7)


def test_headroom_filter() -> None:
    small = _view(alloc={"cores": 1, "ram_gb": 1.0, "disk_gb": 50.0})
    assert feasible(small, _wl()) == "insufficient headroom (need cores>=1, ram_gb>=2.0)"
    assert feasible(small, _wl(requests={"cores": 1, "ram_gb": 0.5})) is None
