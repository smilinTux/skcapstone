from skcapstone.fleet.card_slicing import classify_card_scope, recommend_decomposition


def test_small_card_is_bounded():
    result = recommend_decomposition({"id": "small", "acceptance_criteria": ["one"]})
    assert result.decision == "bounded"
    assert result.leaves == ()


def test_oversized_card_has_two_to_five_dependency_linked_leaves():
    card = {
        "id": "big",
        "title": "Large",
        "acceptance_criteria": ["a"] * 8,
        "repositories": ["one", "two"],
        "mutation_boundaries": ["x", "y"],
        "verification_surfaces": ["unit", "integration", "e2e"],
    }
    result = recommend_decomposition(card)
    assert result.decision == "recommend"
    assert 2 <= len(result.leaves) <= 5
    assert result.leaves[0].depends_on == ("big",)
    assert result.leaves[1].depends_on == (result.leaves[0].id,)
    assert result.custody == "composition"


def test_active_claimed_work_is_advisory():
    result = recommend_decomposition(
        {
            "id": "active",
            "status": "doing",
            "owner": "agent",
            "deliverables": [1, 2, 3],
            "repositories": [1, 2],
        }
    )
    assert result.decision == "advisory"
    assert result.leaves == ()


def test_review_and_composition_custody_are_not_auto_split():
    review = recommend_decomposition(
        {
            "id": "review",
            "kind": "review",
            "deliverables": [1, 2, 3],
            "verification_surfaces": [1, 2],
        }
    )
    epic = recommend_decomposition(
        {
            "id": "epic",
            "labels": ["epic"],
            "deliverables": [1, 2, 3],
            "verification_surfaces": [1, 2],
        }
    )
    assert review.decision == "advisory"
    assert epic.decision == "recommend"
    assert epic.custody == "composition"


def test_classification_uses_structural_signals_not_model_family():
    card = {"model": "large", "deliverables": ["a", "b"], "external_effects": ["mail", "deploy"]}
    signals = classify_card_scope(card)
    assert signals.deliverables == 2
    assert signals.external_effects == 2
    assert signals.independent_axes == 2


def test_reruns_are_idempotent():
    card = {
        "id": "stable",
        "deliverables": [1, 2],
        "repositories": [1, 2],
        "verification_surfaces": [1, 2],
    }
    assert recommend_decomposition(card) == recommend_decomposition(card)
