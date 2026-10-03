"""Pure, deterministic cohort evaluation for SKRSI.

The evaluator consumes immutable aggregate observations and emits a hash-bound
proposal for independent review. It has no runtime or capability interfaces and
cannot enact its proposal.
"""

from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from .skrsi_registry import SKRSIError, canonical_json

EVALUATOR_SCHEMA = "skrsi.cohort-evaluation.v1"
GUARDRAIL_PRECEDENCE = (
    "quality",
    "security",
    "privacy",
    "authorization",
    "reproducibility",
    "user-visible-delivery",
    "throughput",
)
PROPOSALS = frozenset({"REJECT", "CONTINUE", "ACCEPT_FOR_INDEPENDENT_REVIEW"})
OUTCOMES = frozenset({"positive", "null", "negative", "inconclusive"})


def _finite(value: float, name: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise SKRSIError(f"{name} must be finite")
    return number


@dataclass(frozen=True)
class Cohort:
    """A versioned cohort whose source values cannot change after construction."""

    cohort_id: str
    revision: int
    target_revision: int
    metric: str
    samples: tuple[float | None, ...]
    source_hash: str

    def __post_init__(self) -> None:
        if not self.cohort_id or not self.metric or not self.source_hash:
            raise SKRSIError("cohort_id, metric, and source_hash are required")
        if self.revision < 1 or self.target_revision < 1:
            raise SKRSIError("cohort and target revisions must be positive")
        frozen = tuple(
            None if value is None else _finite(value, "sample") for value in self.samples
        )
        if not frozen:
            raise SKRSIError("a cohort must preserve at least one sample slot")
        object.__setattr__(self, "samples", frozen)

    @property
    def ref(self) -> str:
        return f"{self.cohort_id}@{self.revision}"


@dataclass(frozen=True)
class Guardrail:
    name: str
    passed: bool
    evidence_ref: str

    def __post_init__(self) -> None:
        if self.name not in GUARDRAIL_PRECEDENCE:
            raise SKRSIError(f"unknown guardrail: {self.name}")
        if not self.evidence_ref:
            raise SKRSIError("guardrail evidence_ref is required")


@dataclass(frozen=True)
class EvaluationPolicy:
    version: str
    minimum_samples: int
    maximum_missing_fraction: float
    confidence_level: float
    minimum_effect: float
    maximum_samples: int
    harm_threshold: float = 0.0
    stopping_rules: tuple[str, ...] = (
        "guardrail-failure",
        "harm-confidence-bound",
        "adequate-positive-confidence-bound",
        "maximum-sample-limit",
    )

    def __post_init__(self) -> None:
        if not self.version or self.minimum_samples < 2:
            raise SKRSIError("version and minimum_samples >= 2 are required")
        if self.maximum_samples < self.minimum_samples:
            raise SKRSIError("maximum_samples cannot be below minimum_samples")
        if not 0.0 <= self.maximum_missing_fraction < 1.0:
            raise SKRSIError("maximum_missing_fraction must be in [0, 1)")
        if not 0.5 < self.confidence_level < 1.0:
            raise SKRSIError("confidence_level must be in (0.5, 1)")
        if self.minimum_effect < 0 or self.harm_threshold < 0:
            raise SKRSIError("effect thresholds cannot be negative")
        allowed = {
            "guardrail-failure",
            "harm-confidence-bound",
            "adequate-positive-confidence-bound",
            "maximum-sample-limit",
        }
        rules = tuple(self.stopping_rules)
        if not rules or len(rules) != len(set(rules)) or not set(rules) <= allowed:
            raise SKRSIError("stopping_rules must be unique recognized rules")
        object.__setattr__(self, "stopping_rules", rules)


@dataclass(frozen=True)
class CohortSummary:
    cohort_ref: str
    source_hash: str
    total_slots: int
    sample_size: int
    missing_count: int
    missing_fraction: float
    mean: float | None
    variance: float | None


@dataclass(frozen=True)
class EvaluationResult:
    """Immutable decision proposal. No activation authority is represented."""

    evaluator_version: str
    policy_version: str
    baseline: CohortSummary
    treatment: CohortSummary
    confidence_level: float
    confidence_interval: tuple[float, float] | None
    observed_delta: float | None
    sample_adequacy: bool
    missingness_adequacy: bool
    outcome: str
    proposed_decision: str
    stop_reason: str | None
    deterministic_checks: tuple[tuple[str, bool, str], ...]
    negative_findings: tuple[str, ...]
    independent_review_handoff: Mapping[str, str]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES or self.proposed_decision not in PROPOSALS:
            raise SKRSIError("invalid outcome or proposal")
        handoff = MappingProxyType(dict(self.independent_review_handoff))
        if not handoff.get("reviewer") or not handoff.get("evidence_hash"):
            raise SKRSIError("an independent review handoff is required")
        object.__setattr__(self, "independent_review_handoff", handoff)
        payload = self.to_payload(include_hash=False)
        object.__setattr__(
            self, "content_hash", hashlib.sha256(canonical_json(payload)).hexdigest()
        )

    def to_payload(self, *, include_hash: bool = True) -> dict[str, Any]:
        def summary(value: CohortSummary) -> dict[str, Any]:
            return {
                "cohort_ref": value.cohort_ref,
                "source_hash": value.source_hash,
                "total_slots": value.total_slots,
                "sample_size": value.sample_size,
                "missing_count": value.missing_count,
                "missing_fraction": value.missing_fraction,
                "mean": value.mean,
                "variance": value.variance,
            }

        payload: dict[str, Any] = {
            "schema": EVALUATOR_SCHEMA,
            "evaluator_version": self.evaluator_version,
            "policy_version": self.policy_version,
            "baseline": summary(self.baseline),
            "treatment": summary(self.treatment),
            "confidence_level": self.confidence_level,
            "confidence_interval": (
                list(self.confidence_interval) if self.confidence_interval else None
            ),
            "observed_delta": self.observed_delta,
            "sample_adequacy": self.sample_adequacy,
            "missingness_adequacy": self.missingness_adequacy,
            "outcome": self.outcome,
            "proposed_decision": self.proposed_decision,
            "stop_reason": self.stop_reason,
            "deterministic_checks": [
                {"name": name, "passed": passed, "evidence_ref": evidence}
                for name, passed, evidence in self.deterministic_checks
            ],
            "negative_findings": list(self.negative_findings),
            "independent_review_handoff": dict(self.independent_review_handoff),
            "authority": "proposal-only",
            "forbidden_actions": ["activate", "merge", "deploy", "reroute", "grant-capability"],
        }
        if include_hash:
            payload["content_hash"] = self.content_hash
        return payload


def _summarize(cohort: Cohort) -> CohortSummary:
    observed = tuple(value for value in cohort.samples if value is not None)
    missing = len(cohort.samples) - len(observed)
    return CohortSummary(
        cohort_ref=cohort.ref,
        source_hash=cohort.source_hash,
        total_slots=len(cohort.samples),
        sample_size=len(observed),
        missing_count=missing,
        missing_fraction=missing / len(cohort.samples),
        mean=statistics.fmean(observed) if observed else None,
        variance=statistics.variance(observed) if len(observed) > 1 else None,
    )


def evaluate_cohorts(
    baseline: Cohort,
    treatment: Cohort,
    policy: EvaluationPolicy,
    guardrails: tuple[Guardrail, ...],
    *,
    evaluator_version: str,
    evaluator: str,
    independent_reviewer: str,
) -> EvaluationResult:
    """Evaluate two frozen cohorts and propose, but never enact, a decision."""
    if not evaluator_version or not evaluator or not independent_reviewer:
        raise SKRSIError("evaluator version and identities are required")
    if evaluator == independent_reviewer:
        raise SKRSIError("independent reviewer must differ from evaluator")
    if (
        baseline.metric != treatment.metric
        or baseline.target_revision != treatment.target_revision
    ):
        raise SKRSIError("cohorts must share metric and target revision")

    by_name = {item.name: item for item in tuple(guardrails)}
    if set(by_name) != set(GUARDRAIL_PRECEDENCE) or len(guardrails) != len(by_name):
        raise SKRSIError("exactly one evidence-backed result per guardrail is required")
    ordered = tuple(by_name[name] for name in GUARDRAIL_PRECEDENCE)
    checks = tuple((item.name, item.passed, item.evidence_ref) for item in ordered)

    base = _summarize(baseline)
    treat = _summarize(treatment)
    sample_ok = min(base.sample_size, treat.sample_size) >= policy.minimum_samples
    missing_ok = (
        max(base.missing_fraction, treat.missing_fraction) <= policy.maximum_missing_fraction
    )
    delta: float | None = None
    interval: tuple[float, float] | None = None
    if base.mean is not None and treat.mean is not None:
        delta = treat.mean - base.mean
    if (
        sample_ok
        and base.variance is not None
        and treat.variance is not None
        and delta is not None
    ):
        standard_error = math.sqrt(
            base.variance / base.sample_size + treat.variance / treat.sample_size
        )
        z = statistics.NormalDist().inv_cdf((1.0 + policy.confidence_level) / 2.0)
        interval = (delta - z * standard_error, delta + z * standard_error)

    failed = tuple(item.name for item in ordered if not item.passed)
    negatives: list[str] = [f"guardrail:{name}" for name in failed]
    if not missing_ok:
        negatives.append("excessive-missingness")
    if not sample_ok:
        negatives.append("inadequate-sample-size")
    if delta is not None and delta < 0:
        negatives.append("negative-observed-delta")

    if delta is None or not sample_ok or not missing_ok:
        outcome = "inconclusive"
    elif delta < 0:
        outcome = "negative"
    elif abs(delta) < policy.minimum_effect:
        outcome = "null"
        negatives.append("null-outcome")
    else:
        outcome = "positive"

    # Safety and delivery checks are evaluated in declared precedence. Throughput
    # can never compensate for an earlier failure.
    stop_reason: str | None = None
    proposal = "CONTINUE"
    if failed and "guardrail-failure" in policy.stopping_rules:
        proposal, stop_reason = "REJECT", f"guardrail-failure:{failed[0]}"
    elif (
        interval
        and interval[1] < -policy.harm_threshold
        and "harm-confidence-bound" in policy.stopping_rules
    ):
        proposal, stop_reason = "REJECT", "harm-confidence-bound"
    elif (
        interval
        and interval[0] >= policy.minimum_effect
        and sample_ok
        and missing_ok
        and "adequate-positive-confidence-bound" in policy.stopping_rules
    ):
        proposal, stop_reason = (
            "ACCEPT_FOR_INDEPENDENT_REVIEW",
            "adequate-positive-confidence-bound",
        )
    elif (
        max(base.sample_size, treat.sample_size) >= policy.maximum_samples
        and "maximum-sample-limit" in policy.stopping_rules
    ):
        proposal, stop_reason = "REJECT", "maximum-sample-limit"

    evidence_material = {
        "baseline": {"ref": base.cohort_ref, "hash": base.source_hash},
        "treatment": {"ref": treat.cohort_ref, "hash": treat.source_hash},
        "policy": policy.version,
        "checks": checks,
    }
    evidence_hash = hashlib.sha256(canonical_json(evidence_material)).hexdigest()
    return EvaluationResult(
        evaluator_version=evaluator_version,
        policy_version=policy.version,
        baseline=base,
        treatment=treat,
        confidence_level=policy.confidence_level,
        confidence_interval=interval,
        observed_delta=delta,
        sample_adequacy=sample_ok,
        missingness_adequacy=missing_ok,
        outcome=outcome,
        proposed_decision=proposal,
        stop_reason=stop_reason,
        deterministic_checks=checks,
        negative_findings=tuple(negatives),
        independent_review_handoff={
            "reviewer": independent_reviewer,
            "evaluator": evaluator,
            "evidence_hash": evidence_hash,
            "required_action": "independent-review",
        },
    )
