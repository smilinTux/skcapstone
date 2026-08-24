from __future__ import annotations

from datetime import date, timedelta

import pytest

from skdashboard.forecast import (
    DependencyPath,
    DependencyScenario,
    ScheduleProjection,
    ThroughputPeriod,
    backtest,
    forecast,
    simulate_dependencies,
)


def _projection():
    return ScheduleProjection(
        projection_id="skcp-20a:portfolio-schedule",
        projection_version="7",
        projection_hash="sha256:" + "a" * 64,
        input_hash="sha256:" + "b" * 64,
        dependency_paths=(
            DependencyPath("path:release", ("card:upstream", "card:release")),
            DependencyPath("path:service", ("card:service", "card:release")),
        ),
    )


def _history(counts=(2, 3, 1, 4, 2, 3, 2, 5, 1, 3, 4, 2)):
    start = date(2026, 5, 4)
    return tuple(
        ThroughputPeriod(
            period_id=f"week-{index + 1}",
            start=start + timedelta(days=index * 7),
            end=start + timedelta(days=(index + 1) * 7),
            completed=count,
        )
        for index, count in enumerate(counts)
    )


def _walk_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def test_forecast_is_reproducible_truthful_and_range_only():
    kwargs = {
        "cohort": "approved-skdashboard-flow",
        "scope": "service:skdashboard",
        "remaining_work": 18,
        "seed": 3021,
        "iterations": 500,
        "milestone_period": 8,
        "assumptions": ("aggregate weekly throughput remains representative",),
    }
    first = forecast(_history(), **kwargs)
    second = forecast(_history(), **kwargs)

    assert first == second
    assert first["schema_version"] == "1.0.0"
    assert first["state"] == "ready"
    assert first["calculation_owner"] == "deterministic_engine"
    assert first["method_discrimination"]["date_critical_path"].startswith("not calculated")
    assert first["history_window"] == {"start": "2026-05-04", "end": "2026-07-27"}
    assert first["sample_periods"] == 12
    assert (
        first["completion_quantiles_periods"]["p50"]
        <= first["completion_quantiles_periods"]["p85"]
        <= first["completion_quantiles_periods"]["p95"]
    )
    assert 0 <= first["milestone_confidence"] <= 1
    assert "completion_date" not in set(_walk_keys(first))


def test_migrated_administrative_and_mixed_clock_periods_are_excluded():
    history = list(_history())
    history.extend(
        [
            ThroughputPeriod(
                "migration-import",
                date(2026, 8, 3),
                date(2026, 8, 10),
                90,
                "migrated_administrative",
            ),
            ThroughputPeriod(
                "mixed-clock",
                date(2026, 8, 10),
                date(2026, 8, 17),
                40,
                "mixed_clock",
            ),
        ]
    )
    result = forecast(
        history,
        cohort="approved-skdashboard-flow",
        scope="service:skdashboard",
        remaining_work=18,
        seed=7,
        iterations=100,
    )

    assert result["sample_periods"] == 12
    assert {item["timing_basis"] for item in result["exclusions"]} == {
        "migrated_administrative",
        "mixed_clock",
    }
    assert all("excluded" in item["reason"] for item in result["exclusions"])


def test_excluded_administrative_overlap_cannot_reject_canonical_history():
    history = list(_history())
    history.append(
        ThroughputPeriod(
            "migration-overlap",
            history[0].start,
            history[-1].end,
            999,
            "migrated_administrative",
        )
    )

    result = forecast(
        history,
        cohort="approved-flow",
        scope="portfolio:control-plane",
        remaining_work=4,
        seed=1,
    )

    assert result["state"] == "ready"
    assert result["sample_periods"] == 12
    assert [item["period_id"] for item in result["exclusions"]] == ["migration-overlap"]


def test_mixed_period_cadence_abstains_instead_of_pooling_counts():
    history = list(_history((1,) * 6))
    history.append(ThroughputPeriod("month-1", date(2026, 8, 1), date(2026, 8, 31), 20))

    result = forecast(
        history,
        cohort="approved-flow",
        scope="portfolio:control-plane",
        remaining_work=4,
        seed=1,
    )

    assert result["state"] == "abstained"
    assert result["period_cadence_days"] is None
    assert "mixed cadence" in result["abstention_reason"]


def test_low_sample_and_zero_throughput_abstain():
    low_sample = forecast(
        _history((1, 2, 3)),
        cohort="approved-flow",
        scope="portfolio:control-plane",
        remaining_work=4,
        seed=1,
    )
    no_completions = forecast(
        _history((0,) * 6),
        cohort="approved-flow",
        scope="portfolio:control-plane",
        remaining_work=4,
        seed=1,
    )

    assert low_sample["state"] == "abstained"
    assert "fewer than 6" in low_sample["abstention_reason"]
    assert no_completions["state"] == "abstained"
    assert "no completed work" in no_completions["abstention_reason"]
    assert low_sample["completion_quantiles_periods"] == {
        "p50": None,
        "p85": None,
        "p95": None,
    }


def test_dependency_sensitivity_is_read_only_and_aggregate():
    history = _history()
    history_before = tuple(history)
    scenarios = (
        DependencyScenario(
            "dependency-slip",
            remaining_work_delta=2,
            blocked_periods=2,
            changed_item_ids=("card:upstream",),
            assumptions=("upstream dependency remains blocked for two periods",),
        ),
        DependencyScenario(
            "service-capacity",
            aggregate_capacity_multiplier=0.5,
            changed_item_ids=("card:service",),
            assumptions=("aggregate service capacity is reduced by half",),
        ),
    )
    result = simulate_dependencies(
        history,
        cohort="approved-flow",
        scope="portfolio:control-plane",
        remaining_work=18,
        seed=44,
        iterations=300,
        scenarios=scenarios,
        schedule_projection=_projection(),
    )

    assert history == history_before
    assert result["writes_owner_records"] is False
    assert result["schedule_projection"] == {
        "projection_id": "skcp-20a:portfolio-schedule",
        "projection_version": "7",
        "projection_hash": "sha256:" + "a" * 64,
        "input_hash": "sha256:" + "b" * 64,
    }
    assert [path["path_id"] for path in result["scenarios"][0]["affected_paths"]] == [
        "path:release"
    ]
    assert result["scenarios"][0]["schedule_projection"] == result["schedule_projection"]
    assert result["scenarios"][0]["sensitivity_periods"]["p50"] >= 2
    assert result["scenarios"][1]["sensitivity_periods"]["p85"] > 0


def test_zero_aggregate_capacity_scenario_abstains_instead_of_promising():
    result = simulate_dependencies(
        _history((1,) * 8),
        cohort="approved-flow",
        scope="portfolio:control-plane",
        remaining_work=4,
        seed=4,
        iterations=20,
        scenarios=(
            DependencyScenario("zero-rounded-capacity", aggregate_capacity_multiplier=0.1),
        ),
        schedule_projection=_projection(),
    )

    assert result["scenarios"][0]["state"] == "abstained"
    assert result["scenarios"][0]["completion_quantiles_periods"]["p95"] is None


def test_large_blocked_delay_is_separate_from_delivery_horizon():
    result = simulate_dependencies(
        _history((2,) * 8),
        cohort="approved-flow",
        scope="portfolio:control-plane",
        remaining_work=4,
        seed=4,
        iterations=20,
        scenarios=(
            DependencyScenario(
                "long-block",
                blocked_periods=1000,
                changed_item_ids=("card:upstream",),
            ),
        ),
        schedule_projection=_projection(),
    )

    scenario = result["scenarios"][0]
    assert scenario["state"] == "ready"
    assert scenario["blocked_delay_periods"] == 1000
    assert scenario["completion_quantiles_periods"]["p50"] == (
        1000 + scenario["delivery_quantiles_periods"]["p50"]
    )


def test_scenario_cannot_name_items_outside_bounded_projection_topology():
    with pytest.raises(ValueError, match="must exist in the bound dependency topology"):
        simulate_dependencies(
            _history(),
            cohort="approved-flow",
            scope="portfolio:control-plane",
            remaining_work=4,
            seed=4,
            iterations=20,
            scenarios=(DependencyScenario("spoofed-path", changed_item_ids=("card:unbound",)),),
            schedule_projection=_projection(),
        )

    with pytest.raises(ValueError, match="sha256:<64 lowercase hex>"):
        ScheduleProjection(
            projection_id="skcp-20a:portfolio-schedule",
            projection_version="7",
            projection_hash="not-a-hash",
            input_hash="sha256:" + "b" * 64,
            dependency_paths=(DependencyPath("path:release", ("card:release",)),),
        )
    with pytest.raises(ValueError, match="input_hash"):
        ScheduleProjection(
            projection_id="skcp-20a:portfolio-schedule",
            projection_version="7",
            projection_hash="sha256:" + "a" * 64,
            input_hash="b" * 64,
            dependency_paths=(DependencyPath("path:release", ("card:release",)),),
        )


def test_projection_binding_accepts_released_schedule_contract_hash_shape():
    released_projection = {
        "projection_id": "schedule-1",
        "projection_version": "projection-v1",
        "projection_hash": "sha256:" + "a" * 64,
    }
    projection = ScheduleProjection(
        **released_projection,
        input_hash="sha256:" + "b" * 64,
        dependency_paths=(DependencyPath("path:release", ("card:release",)),),
    )

    assert projection.binding() == {
        **released_projection,
        "input_hash": "sha256:" + "b" * 64,
    }


def test_backtest_records_calibration_misses_drift_and_has_no_leakage():
    history = _history()
    result = backtest(
        history,
        cohort="approved-flow",
        scope="portfolio:control-plane",
        work_target=4,
        seed=81,
        iterations=300,
    )
    changed_future = history[:-1] + (
        ThroughputPeriod(
            "week-12",
            history[-1].start,
            history[-1].end,
            30,
        ),
    )
    changed = backtest(
        changed_future,
        cohort="approved-flow",
        scope="portfolio:control-plane",
        work_target=4,
        seed=81,
        iterations=300,
    )

    assert result["state"] == "ready"
    assert set(result["coverage"]) == {"p50", "p85", "p95"}
    assert isinstance(result["misses"], int)
    assert result["drift"]["state"] == "available"
    assert (
        result["records"][0]["forecast_quantiles_periods"]
        == changed["records"][0]["forecast_quantiles_periods"]
    )


def test_backtest_truthfully_abstains_when_outcomes_are_insufficient():
    result = backtest(
        _history((2, 2, 2, 2, 2, 2, 0)),
        cohort="approved-flow",
        scope="portfolio:control-plane",
        work_target=4,
        seed=9,
    )

    assert result["state"] == "abstained"
    assert "leakage-free rolling backtests" in result["abstention_reason"]


def test_backtest_zero_training_and_material_drift_return_typed_abstention():
    zero_training = backtest(
        _history((0, 0, 0, 0, 0, 0, 3, 3, 3, 3)),
        cohort="approved-flow",
        scope="portfolio:control-plane",
        work_target=3,
        seed=9,
    )
    drifted = backtest(
        _history((1, 1, 1, 1, 1, 1, 8, 8, 8, 8, 8, 8)),
        cohort="approved-flow",
        scope="portfolio:control-plane",
        work_target=4,
        seed=10,
    )

    assert zero_training["state"] == "abstained"
    assert "no completed work" in zero_training["abstention_reason"]
    assert zero_training["records"] == []
    assert drifted["state"] == "abstained"
    assert drifted["drift"]["material"] is True
    assert "calibration drift" in drifted["abstention_reason"]


def test_backtest_mixed_cadence_never_calculates_or_reports_drift():
    history = list(_history((1,) * 6))
    history.append(ThroughputPeriod("month-1", date(2026, 8, 1), date(2026, 8, 31), 20))

    result = backtest(
        history,
        cohort="approved-flow",
        scope="portfolio:control-plane",
        work_target=4,
        seed=10,
    )

    assert result["state"] == "abstained"
    assert result["abstention_reason"] == "canonical throughput periods have mixed cadence"
    assert result["records"] == []
    assert result["coverage"] == {"p50": None, "p85": None, "p95": None}
    assert result["drift"] == {
        "state": "unavailable",
        "later_to_earlier_throughput_ratio": None,
        "maximum_approved_factor": 2.0,
        "material": False,
    }


def test_artifacts_never_expose_individual_ranking_fields():
    artifact = simulate_dependencies(
        _history(),
        cohort="approved-flow",
        scope="portfolio:control-plane",
        remaining_work=12,
        seed=12,
        iterations=50,
        scenarios=(DependencyScenario("baseline-capacity"),),
        schedule_projection=_projection(),
    )
    forbidden = {"person", "user", "assignee", "individual_capacity", "productivity_score"}

    assert forbidden.isdisjoint(_walk_keys(artifact))
    assert artifact["individual_ranking_prohibited"] is True


def test_invalid_or_overlapping_inputs_fail_closed():
    with pytest.raises(ValueError, match="must not overlap"):
        forecast(
            (
                ThroughputPeriod("one", date(2026, 1, 1), date(2026, 1, 8), 1),
                ThroughputPeriod("two", date(2026, 1, 7), date(2026, 1, 14), 1),
            ),
            cohort="approved-flow",
            scope="portfolio:control-plane",
            remaining_work=2,
            seed=1,
        )
