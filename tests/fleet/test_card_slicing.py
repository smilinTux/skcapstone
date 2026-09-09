from skcapstone.fleet.card_slicing import classify_card_scope, recommend_decomposition


def test_small_card_is_bounded():
    result = recommend_decomposition({"id": "small", "acceptance_criteria": ["one"]})
    assert result.decision == "bounded"
    assert result.leaves == ()


def test_oversized_card_has_two_to_five_dependency_linked_leaves():
    card = {
        "id": "big",
        "title": "Large",
        "repository": "https://github.com/example/project",
        "base_ref": "main",
        "dependencies": ["prerequisite-b", "prerequisite-a"],
        "acceptance_criteria": ["a"] * 8,
        "repositories": ["one", "two"],
        "mutation_boundaries": ["x", "y"],
        "verification_surfaces": ["unit", "integration", "e2e"],
    }
    result = recommend_decomposition(card)
    assert result.decision == "reject"
    assert 2 <= len(result.leaves) <= 5
    assert result.leaves[0].depends_on == ("prerequisite-a", "prerequisite-b")
    assert result.leaves[1].depends_on == (
        "prerequisite-a",
        "prerequisite-b",
        result.leaves[0].id,
    )
    assert result.custody == "composition"
    assert result.parent_dependencies == tuple(leaf.id for leaf in result.leaves)


def test_recommended_leaves_preserve_checkout_identity_and_complete_dependencies():
    card = {
        "id": "identity",
        "title": "Identity",
        "links": {
            "repository": "https://github.com/example/project",
            "base_ref": "release",
        },
        "dependencies": ["prerequisite-b", "prerequisite-a", "prerequisite-a"],
        "deliverables": [1, 2],
        "verification_surfaces": [1, 2],
    }
    result = recommend_decomposition(card)

    assert result.decision == "reject"
    for ordinal, leaf in enumerate(result.leaves):
        assert leaf.repository == "https://github.com/example/project"
        assert leaf.base_ref == "release"
        expected = ("prerequisite-a", "prerequisite-b")
        if ordinal:
            expected += (result.leaves[ordinal - 1].id,)
        assert leaf.depends_on == expected


def test_oversized_card_without_checkout_identity_fails_closed():
    result = recommend_decomposition(
        {
            "id": "missing-checkout",
            "deliverables": [1, 2],
            "verification_surfaces": [1, 2],
        }
    )

    assert result.decision == "advisory"
    assert result.reason == "repository and base_ref are required for leaf recommendations"
    assert result.leaves == ()


def test_oversized_card_with_invalid_dependency_fails_closed():
    result = recommend_decomposition(
        {
            "id": "invalid-dependency",
            "repository": "https://github.com/example/project",
            "base_ref": "main",
            "dependencies": ["valid", ""],
            "deliverables": [1, 2],
            "verification_surfaces": [1, 2],
        }
    )

    assert result.decision == "advisory"
    assert result.reason == "dependencies must be non-empty card ids"
    assert result.leaves == ()


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
            "repository": "https://github.com/example/project",
            "base_ref": "main",
            "deliverables": [1, 2, 3],
            "verification_surfaces": [1, 2],
        }
    )
    assert review.decision == "advisory"
    assert epic.decision == "reject"
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
        "repository": "https://github.com/example/project",
        "base_ref": "main",
        "deliverables": [1, 2],
        "repositories": [1, 2],
        "verification_surfaces": [1, 2],
    }
    assert recommend_decomposition(card) == recommend_decomposition(card)


def test_duplicate_successors_are_not_recommended_on_rerun():
    card = {
        "id": "stable",
        "repository": "https://github.com/example/project",
        "base_ref": "main",
        "deliverables": [1, 2],
        "repositories": [1, 2],
        "verification_surfaces": [1, 2],
    }
    first = recommend_decomposition(card)
    rerun_card = {
        **card,
        "successors": [first.leaves[0].id, first.leaves[0].id],
    }

    rerun = recommend_decomposition(rerun_card)

    assert len({leaf.id for leaf in rerun.leaves}) == len(rerun.leaves)
    assert first.leaves[0].id not in {leaf.id for leaf in rerun.leaves}
    assert rerun.parent_dependencies == first.parent_dependencies
    assert rerun == recommend_decomposition(rerun_card)


def test_parent_waits_for_leaves_without_parent_leaf_cycle():
    result = recommend_decomposition(
        {
            "id": "acyclic",
            "repository": "https://github.com/example/project",
            "base_ref": "main",
            "deliverables": [1, 2],
            "verification_surfaces": [1, 2],
        }
    )

    assert result.decision == "reject"
    assert result.parent_dependencies == tuple(leaf.id for leaf in result.leaves)
    assert all("acyclic" not in leaf.depends_on for leaf in result.leaves)


def test_leaf_count_is_always_bounded_between_two_and_five():
    card = {
        "id": "bounded-count",
        "repository": "https://github.com/example/project",
        "base_ref": "main",
        "deliverables": list(range(20)),
        "verification_surfaces": list(range(20)),
    }

    assert len(recommend_decomposition(card, max_leaves=1).leaves) == 2
    assert len(recommend_decomposition(card, max_leaves=99).leaves) <= 5
