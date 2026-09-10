from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from skcapstone.skrsi_evaluator import (
    GUARDRAIL_PRECEDENCE,
    Cohort,
    EvaluationPolicy,
    Guardrail,
    evaluate_cohorts,
)
from skcapstone.skrsi_registry import SKRSIError


def cohort(name: str, values: tuple[float | None, ...], *, revision: int = 1) -> Cohort:
    return Cohort(name, revision, 3, "delivery-score", values, (name[0] * 64))


def policy(**changes: object) -> EvaluationPolicy:
    values = {
        "version": "quality-policy.v1",
        "minimum_samples": 4,
        "maximum_missing_fraction": 0.20,
        "confidence_level": 0.95,
        "minimum_effect": 1.0,
        "maximum_samples": 100,
    }
    values.update(changes)
    return EvaluationPolicy(**values)


def gates(**changes: bool) -> tuple[Guardrail, ...]:
    return tuple(
        Guardrail(name, changes.get(name, True), f"sha256:{index:064x}")
        for index, name in enumerate(GUARDRAIL_PRECEDENCE, 1)
    )


def evaluate(base: Cohort, treatment: Cohort, **changes: object):
    args = {
        "policy": policy(),
        "guardrails": gates(),
        "evaluator_version": "cohort-evaluator.v1",
        "evaluator": "atlas",
        "independent_reviewer": "lumina",
    }
    args.update(changes)
    return evaluate_cohorts(base, treatment, **args)


def test_positive_result_is_hash_bound_and_requires_independent_review():
    result = evaluate(
        cohort("baseline", (1.0, 1.1, 0.9, 1.0, 1.05)),
        cohort("treatment", (3.0, 3.1, 2.9, 3.0, 3.05)),
    )

    assert result.outcome == "positive"
    assert result.proposed_decision == "ACCEPT_FOR_INDEPENDENT_REVIEW"
    assert result.stop_reason == "adequate-positive-confidence-bound"
    assert result.independent_review_handoff["reviewer"] == "lumina"
    assert len(result.content_hash) == 64
    payload = result.to_payload()
    assert payload["authority"] == "proposal-only"
    assert payload["forbidden_actions"] == [
        "activate",
        "merge",
        "deploy",
        "reroute",
        "grant-capability",
    ]
    with pytest.raises(TypeError):
        result.independent_review_handoff["reviewer"] = "atlas"


def test_guardrails_outrank_throughput_in_fixed_precedence():
    result = evaluate(
        cohort("baseline", (1.0, 1.0, 1.0, 1.0)),
        cohort("treatment", (100.0, 100.0, 100.0, 100.0)),
        guardrails=gates(privacy=False, throughput=True, quality=True),
    )

    assert [check[0] for check in result.deterministic_checks] == list(GUARDRAIL_PRECEDENCE)
    assert result.proposed_decision == "REJECT"
    assert result.stop_reason == "guardrail-failure:privacy"
    assert "guardrail:privacy" in result.negative_findings


def test_missingness_and_sample_adequacy_are_explicit_and_preserved():
    result = evaluate(
        cohort("baseline", (1.0, None, None, 1.0)),
        cohort("treatment", (2.0, None, 2.0, None)),
    )

    assert result.outcome == "inconclusive"
    assert result.proposed_decision == "CONTINUE"
    assert result.baseline.total_slots == 4
    assert result.baseline.missing_count == 2
    assert result.baseline.missing_fraction == 0.5
    assert not result.sample_adequacy
    assert not result.missingness_adequacy
    assert result.confidence_interval is None
    assert result.negative_findings == ("excessive-missingness", "inadequate-sample-size")


def test_null_and_negative_outcomes_are_not_discarded():
    null = evaluate(
        cohort("baseline", (10.0, 10.1, 9.9, 10.0)),
        cohort("treatment", (10.1, 10.2, 10.0, 10.1)),
    )
    negative = evaluate(
        cohort("baseline", (10.0, 10.1, 9.9, 10.0)),
        cohort("treatment", (8.0, 8.1, 7.9, 8.0)),
    )

    assert null.outcome == "null"
    assert "null-outcome" in null.negative_findings
    assert negative.outcome == "negative"
    assert negative.proposed_decision == "REJECT"
    assert negative.stop_reason == "harm-confidence-bound"
    assert "negative-observed-delta" in negative.negative_findings


def test_versioned_inputs_and_results_are_immutable_and_deterministic():
    source = [1.0, 1.1, 0.9, 1.0]
    base = cohort("baseline", tuple(source), revision=7)
    treatment = cohort("treatment", (2.0, 2.1, 1.9, 2.0), revision=9)
    first = evaluate(base, treatment)
    source[0] = 999.0
    second = evaluate(base, treatment)

    assert base.ref == "baseline@7"
    assert treatment.ref == "treatment@9"
    assert base.samples[0] == 1.0
    assert first.to_payload() == second.to_payload()
    with pytest.raises(FrozenInstanceError):
        base.revision = 8
    with pytest.raises(FrozenInstanceError):
        first.outcome = "positive"


def test_fail_closed_on_mismatched_versions_gates_and_reviewer():
    base = cohort("baseline", (1.0, 1.0, 1.0, 1.0))
    wrong_target = Cohort("treatment", 1, 4, "delivery-score", (2.0,) * 4, "c" * 64)
    with pytest.raises(SKRSIError, match="target revision"):
        evaluate(base, wrong_target)
    with pytest.raises(SKRSIError, match="one evidence-backed"):
        evaluate(base, cohort("treatment", (2.0,) * 4), guardrails=gates()[:-1])
    with pytest.raises(SKRSIError, match="must differ"):
        evaluate(base, cohort("treatment", (2.0,) * 4), independent_reviewer="atlas")


def test_maximum_sample_stopping_rule_is_deterministic():
    result = evaluate(
        cohort("baseline", (1.0, 2.0, 1.0, 2.0)),
        cohort("treatment", (1.1, 2.1, 1.1, 2.1)),
        policy=policy(maximum_samples=4),
    )
    assert result.outcome == "null"
    assert result.proposed_decision == "REJECT"
    assert result.stop_reason == "maximum-sample-limit"
