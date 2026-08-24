"""Deterministic aggregate schedule forecasting.

This module deliberately does not consume or emit person-level data. It samples
aggregate throughput periods and returns a read-only, versioned artifact. Date-based
critical-path calculations remain a separate schedule method.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date
from statistics import fmean
from typing import Sequence

SCHEMA_VERSION = "1.0.0"
METHOD = "aggregate_throughput_bootstrap_monte_carlo"
SUPPORTED_TIMING_BASIS = "canonical_period"
QUANTILES = (50, 85, 95)
MAX_DEPENDENCY_PATHS = 256
MAX_ITEMS_PER_PATH = 64


@dataclass(frozen=True)
class ThroughputPeriod:
    """One aggregate service or cohort throughput period."""

    period_id: str
    start: date
    end: date
    completed: int
    timing_basis: str = SUPPORTED_TIMING_BASIS

    def __post_init__(self) -> None:
        if not self.period_id:
            raise ValueError("period_id is required")
        if self.end <= self.start:
            raise ValueError("period end must follow start")
        if self.completed < 0:
            raise ValueError("completed must be non-negative")


@dataclass(frozen=True)
class DependencyScenario:
    """Immutable aggregate sensitivity input, never an owner-system mutation."""

    scenario_id: str
    remaining_work_delta: int = 0
    blocked_periods: int = 0
    aggregate_capacity_multiplier: float = 1.0
    changed_item_ids: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.scenario_id:
            raise ValueError("scenario_id is required")
        if self.blocked_periods < 0:
            raise ValueError("blocked_periods must be non-negative")
        if self.aggregate_capacity_multiplier <= 0:
            raise ValueError("aggregate_capacity_multiplier must be positive")


@dataclass(frozen=True)
class DependencyPath:
    """One bounded path from the exact owner schedule projection."""

    path_id: str
    item_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.path_id:
            raise ValueError("path_id is required")
        if not self.item_ids or len(self.item_ids) > MAX_ITEMS_PER_PATH:
            raise ValueError(f"dependency paths require 1 to {MAX_ITEMS_PER_PATH} items")
        if any(not item_id for item_id in self.item_ids):
            raise ValueError("dependency path item IDs must be non-empty")


@dataclass(frozen=True)
class ScheduleProjection:
    """Exact SKCP-20A projection binding and bounded dependency topology."""

    projection_id: str
    projection_version: str
    projection_hash: str
    input_hash: str
    dependency_paths: tuple[DependencyPath, ...]

    def __post_init__(self) -> None:
        if not self.projection_id or not self.projection_version:
            raise ValueError("projection_id and projection_version are required")
        for name, value in (
            ("projection_hash", self.projection_hash),
            ("input_hash", self.input_hash),
        ):
            digest = value.removeprefix("sha256:")
            if (
                not value.startswith("sha256:")
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError(f"{name} must use sha256:<64 lowercase hex>")
        if not self.dependency_paths or len(self.dependency_paths) > MAX_DEPENDENCY_PATHS:
            raise ValueError(f"dependency topology requires 1 to {MAX_DEPENDENCY_PATHS} paths")
        path_ids = [path.path_id for path in self.dependency_paths]
        if len(path_ids) != len(set(path_ids)):
            raise ValueError("dependency path IDs must be unique")

    def binding(self) -> dict[str, str]:
        return {
            "projection_id": self.projection_id,
            "projection_version": self.projection_version,
            "projection_hash": self.projection_hash,
            "input_hash": self.input_hash,
        }


def _prepare_history(
    history: Sequence[ThroughputPeriod],
) -> tuple[list[ThroughputPeriod], list[dict[str, str]]]:
    ordered = sorted(history, key=lambda period: (period.start, period.end, period.period_id))
    included = [period for period in ordered if period.timing_basis == SUPPORTED_TIMING_BASIS]
    excluded = [
        {
            "period_id": period.period_id,
            "timing_basis": period.timing_basis,
            "reason": "non-canonical timing excluded from aggregate throughput sampling",
        }
        for period in ordered
        if period.timing_basis != SUPPORTED_TIMING_BASIS
    ]
    for previous, current in zip(included, included[1:]):
        if current.start < previous.end:
            raise ValueError("canonical throughput periods must not overlap")
    return included, excluded


def _cadence_days(periods: Sequence[ThroughputPeriod]) -> int | None:
    cadences = {(period.end - period.start).days for period in periods}
    return cadences.pop() if len(cadences) == 1 else None


def _nearest_rank(values: list[int], percentile: int) -> int:
    return sorted(values)[max(0, math.ceil(percentile / 100 * len(values)) - 1)]


def _simulate(
    counts: Sequence[int],
    remaining_work: int,
    *,
    seed: int,
    iterations: int,
    blocked_periods: int = 0,
    capacity_multiplier: float = 1.0,
) -> list[int]:
    rng = random.Random(seed)
    outcomes: list[int] = []
    max_delivery_periods = max(1000, remaining_work * 100)
    for _ in range(iterations):
        completed = 0
        delivery_periods = 0
        while completed < remaining_work and delivery_periods < max_delivery_periods:
            sampled = counts[rng.randrange(len(counts))]
            completed += max(0, round(sampled * capacity_multiplier))
            delivery_periods += 1
        if completed < remaining_work:
            raise ValueError("forecast horizon exhausted before remaining work completed")
        outcomes.append(blocked_periods + delivery_periods)
    return outcomes


def _quantiles(outcomes: list[int]) -> dict[str, int]:
    return {f"p{percentile}": _nearest_rank(outcomes, percentile) for percentile in QUANTILES}


def forecast(
    history: Sequence[ThroughputPeriod],
    *,
    cohort: str,
    scope: str,
    remaining_work: int,
    seed: int,
    iterations: int = 2000,
    minimum_sample: int = 6,
    milestone_period: int | None = None,
    assumptions: Sequence[str] = (),
) -> dict:
    """Forecast aggregate remaining work with reproducible bootstrap sampling."""

    if not cohort or not scope:
        raise ValueError("cohort and scope are required")
    if remaining_work <= 0:
        raise ValueError("remaining_work must be positive")
    if iterations <= 0 or minimum_sample <= 0:
        raise ValueError("iterations and minimum_sample must be positive")
    if milestone_period is not None and milestone_period < 0:
        raise ValueError("milestone_period must be non-negative")

    included, excluded = _prepare_history(history)
    cadence_days = _cadence_days(included)
    base = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "aggregate_schedule_forecast",
        "state": "ready",
        "method": METHOD,
        "calculation_owner": "deterministic_engine",
        "method_discrimination": {
            "throughput_forecast": "probabilistic aggregate flow in periods",
            "date_critical_path": "not calculated or blended by this artifact",
        },
        "cohort": cohort,
        "scope": scope,
        "history_window": {
            "start": included[0].start.isoformat() if included else None,
            "end": included[-1].end.isoformat() if included else None,
        },
        "sample_periods": len(included),
        "period_cadence_days": cadence_days,
        "remaining_work": remaining_work,
        "iterations": iterations,
        "seed": seed,
        "assumptions": list(assumptions),
        "exclusions": excluded,
        "individual_ranking_prohibited": True,
        "writes_owner_records": False,
    }
    abstention_reason = None
    if len(included) < minimum_sample:
        abstention_reason = f"fewer than {minimum_sample} canonical throughput periods"
    elif cadence_days is None:
        abstention_reason = "canonical throughput periods have mixed cadence"
    elif not any(period.completed for period in included):
        abstention_reason = "canonical history contains no completed work"
    if abstention_reason:
        return {
            **base,
            "state": "abstained",
            "abstention_reason": abstention_reason,
            "completion_quantiles_periods": {"p50": None, "p85": None, "p95": None},
            "milestone_confidence": None,
        }

    outcomes = _simulate(
        [period.completed for period in included],
        remaining_work,
        seed=seed,
        iterations=iterations,
    )
    return {
        **base,
        "abstention_reason": None,
        "completion_quantiles_periods": _quantiles(outcomes),
        "milestone_confidence": (
            None
            if milestone_period is None
            else sum(value <= milestone_period for value in outcomes) / len(outcomes)
        ),
    }


def simulate_dependencies(
    history: Sequence[ThroughputPeriod],
    *,
    cohort: str,
    scope: str,
    remaining_work: int,
    seed: int,
    scenarios: Sequence[DependencyScenario],
    schedule_projection: ScheduleProjection,
    iterations: int = 2000,
    minimum_sample: int = 6,
) -> dict:
    """Compare immutable dependency and aggregate service-capacity assumptions."""

    baseline = forecast(
        history,
        cohort=cohort,
        scope=scope,
        remaining_work=remaining_work,
        seed=seed,
        iterations=iterations,
        minimum_sample=minimum_sample,
    )
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "aggregate_dependency_sensitivity",
        "state": baseline["state"],
        "baseline": baseline,
        "schedule_projection": schedule_projection.binding(),
        "scenarios": [],
        "writes_owner_records": False,
        "individual_ranking_prohibited": True,
    }
    if baseline["state"] != "ready":
        return artifact

    included, _ = _prepare_history(history)
    counts = [period.completed for period in included]
    baseline_quantiles = baseline["completion_quantiles_periods"]
    topology_item_ids = {
        item_id for path in schedule_projection.dependency_paths for item_id in path.item_ids
    }
    for scenario in scenarios:
        scenario_work = remaining_work + scenario.remaining_work_delta
        if scenario_work <= 0:
            raise ValueError("scenario remaining work must be positive")
        unknown_items = set(scenario.changed_item_ids) - topology_item_ids
        if unknown_items:
            raise ValueError("scenario changed items must exist in the bound dependency topology")
        affected_paths = [
            path
            for path in schedule_projection.dependency_paths
            if not scenario.changed_item_ids
            or set(path.item_ids).intersection(scenario.changed_item_ids)
        ]
        changed_assumptions = {
            "remaining_work_delta": scenario.remaining_work_delta,
            "blocked_periods": scenario.blocked_periods,
            "aggregate_capacity_multiplier": scenario.aggregate_capacity_multiplier,
            "changed_item_ids": list(scenario.changed_item_ids),
            "assumptions": list(scenario.assumptions),
        }
        if not any(round(count * scenario.aggregate_capacity_multiplier) > 0 for count in counts):
            artifact["scenarios"].append(
                {
                    "scenario_id": scenario.scenario_id,
                    "state": "abstained",
                    "abstention_reason": "aggregate capacity assumption yields no completions",
                    "schedule_projection": schedule_projection.binding(),
                    "changed_assumptions": changed_assumptions,
                    "affected_paths": [
                        {
                            "path_id": path.path_id,
                            "item_ids": list(path.item_ids),
                            "sensitivity_periods": {
                                "p50": None,
                                "p85": None,
                                "p95": None,
                            },
                        }
                        for path in affected_paths
                    ],
                    "completion_quantiles_periods": {"p50": None, "p85": None, "p95": None},
                    "sensitivity_periods": {"p50": None, "p85": None, "p95": None},
                }
            )
            continue
        outcomes = _simulate(
            counts,
            scenario_work,
            seed=seed,
            iterations=iterations,
            blocked_periods=scenario.blocked_periods,
            capacity_multiplier=scenario.aggregate_capacity_multiplier,
        )
        quantiles = _quantiles(outcomes)
        delivery_quantiles = {
            key: value - scenario.blocked_periods for key, value in quantiles.items()
        }
        sensitivity = {key: quantiles[key] - baseline_quantiles[key] for key in quantiles}
        artifact["scenarios"].append(
            {
                "scenario_id": scenario.scenario_id,
                "state": "ready",
                "schedule_projection": schedule_projection.binding(),
                "changed_assumptions": changed_assumptions,
                "affected_paths": [
                    {
                        "path_id": path.path_id,
                        "item_ids": list(path.item_ids),
                        "sensitivity_periods": sensitivity,
                    }
                    for path in affected_paths
                ],
                "blocked_delay_periods": scenario.blocked_periods,
                "delivery_quantiles_periods": delivery_quantiles,
                "completion_quantiles_periods": quantiles,
                "sensitivity_periods": sensitivity,
            }
        )
    return artifact


def backtest(
    history: Sequence[ThroughputPeriod],
    *,
    cohort: str,
    scope: str,
    work_target: int,
    seed: int,
    iterations: int = 1000,
    minimum_sample: int = 6,
    minimum_backtests: int = 3,
    maximum_drift_factor: float = 2.0,
) -> dict:
    """Run rolling-origin calibration without using future periods as features."""

    if work_target <= 0:
        raise ValueError("work_target must be positive")
    if maximum_drift_factor <= 1:
        raise ValueError("maximum_drift_factor must be greater than one")
    included, exclusions = _prepare_history(history)
    cadence_days = _cadence_days(included)
    abstention_reason = None
    if cadence_days is None:
        abstention_reason = "canonical throughput periods have mixed cadence"
    elif len(included) >= minimum_sample and not any(
        period.completed for period in included[:minimum_sample]
    ):
        abstention_reason = "rolling training history contains no completed work"
    records = []
    for cutoff in range(minimum_sample, len(included)) if abstention_reason is None else ():
        future_total = 0
        actual_periods = 0
        for period in included[cutoff:]:
            future_total += period.completed
            actual_periods += 1
            if future_total >= work_target:
                break
        if future_total < work_target:
            continue
        outcomes = _simulate(
            [period.completed for period in included[:cutoff]],
            work_target,
            seed=seed + cutoff,
            iterations=iterations,
        )
        quantiles = _quantiles(outcomes)
        records.append(
            {
                "cutoff_period_id": included[cutoff - 1].period_id,
                "training_sample_periods": cutoff,
                "forecast_quantiles_periods": quantiles,
                "actual_periods": actual_periods,
                "covered": {key: actual_periods <= value for key, value in quantiles.items()},
                "missed_p95": actual_periods > quantiles["p95"],
            }
        )

    coverage = {
        key: (
            sum(record["covered"][key] for record in records) / len(records) if records else None
        )
        for key in ("p50", "p85", "p95")
    }
    midpoint = len(included) // 2
    earlier = [period.completed for period in included[:midpoint]]
    later = [period.completed for period in included[midpoint:]]
    drift_ratio = None
    if cadence_days is not None and earlier and later and fmean(earlier) > 0:
        drift_ratio = fmean(later) / fmean(earlier)
    material_drift = drift_ratio is not None and not (
        1 / maximum_drift_factor <= drift_ratio <= maximum_drift_factor
    )
    if abstention_reason is None and len(records) < minimum_backtests:
        abstention_reason = f"fewer than {minimum_backtests} leakage-free rolling backtests"
    if material_drift:
        abstention_reason = "material throughput calibration drift exceeds approved bounds"
    state = "abstained" if abstention_reason else "ready"
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "aggregate_forecast_backtest",
        "state": state,
        "abstention_reason": abstention_reason,
        "method": METHOD,
        "calculation_owner": "deterministic_engine",
        "cohort": cohort,
        "scope": scope,
        "work_target": work_target,
        "seed": seed,
        "sample_periods": len(included),
        "period_cadence_days": cadence_days,
        "exclusions": exclusions,
        "records": records,
        "coverage": coverage,
        "misses": sum(record["missed_p95"] for record in records),
        "drift": {
            "state": "available" if drift_ratio is not None else "unavailable",
            "later_to_earlier_throughput_ratio": drift_ratio,
            "maximum_approved_factor": maximum_drift_factor,
            "material": material_drift,
        },
        "writes_owner_records": False,
        "individual_ranking_prohibited": True,
    }
