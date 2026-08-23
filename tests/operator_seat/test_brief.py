"""Tests for the O4a operator brief builder (pure)."""

from __future__ import annotations

from skcapstone.operator_seat.brief import build_brief


def test_all_healthy_is_quiet():
    observations = {
        "app-a": [{"type": "Ready", "status": "True"}],
    }
    result = build_brief(observations, problem_types=set())
    assert result["quiet"] is True
    assert result["firing"] == []
    assert result["stale"] == []
    assert result["counts"] == {"firing": 0, "stale": 0}
    assert result["apps"] == ["app-a"]


def test_problem_type_true_is_firing():
    observations = {
        "app-a": [{"type": "CrashLooping", "status": "True"}],
    }
    result = build_brief(observations, problem_types={"CrashLooping"})
    assert result["firing"] == [{"app": "app-a", "type": "CrashLooping", "status": "True"}]
    assert result["quiet"] is False
    assert result["counts"] == {"firing": 1, "stale": 0}


def test_health_type_false_is_firing():
    observations = {
        "app-a": [{"type": "Ready", "status": "False"}],
    }
    result = build_brief(observations, problem_types={"CrashLooping"})
    assert result["firing"] == [{"app": "app-a", "type": "Ready", "status": "False"}]
    assert result["quiet"] is False
    assert result["counts"] == {"firing": 1, "stale": 0}


def test_unknown_is_stale_not_firing():
    observations = {
        "app-a": [{"type": "Ready", "status": "Unknown"}],
    }
    result = build_brief(observations, problem_types={"CrashLooping"})
    assert result["firing"] == []
    assert result["stale"] == [{"app": "app-a", "type": "Ready", "status": "Unknown"}]
    assert result["quiet"] is False
    assert result["counts"] == {"firing": 0, "stale": 1}


def test_multi_app_aggregation_and_counts():
    observations = {
        "app-b": [
            {"type": "Ready", "status": "True"},
            {"type": "CrashLooping", "status": "Unknown"},
        ],
        "app-a": [
            {"type": "CrashLooping", "status": "True"},
            {"type": "Ready", "status": "False"},
        ],
    }
    result = build_brief(observations, problem_types={"CrashLooping"})
    assert result["apps"] == ["app-a", "app-b"]
    assert result["firing"] == [
        {"app": "app-a", "type": "CrashLooping", "status": "True"},
        {"app": "app-a", "type": "Ready", "status": "False"},
    ]
    assert result["stale"] == [{"app": "app-b", "type": "CrashLooping", "status": "Unknown"}]
    assert result["counts"] == {"firing": 2, "stale": 1}
    assert result["quiet"] is False


def test_firing_entry_carries_object_identity():
    from skcapstone.operator_seat import brief as briefmod

    obs = {"fleet": [{"type": "MissedRun", "status": "True", "object": "nightly"}]}
    b = briefmod.build_brief(obs, {"MissedRun"})
    assert b["firing"][0]["object"] == "nightly"
