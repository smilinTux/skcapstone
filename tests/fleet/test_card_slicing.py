from skcapstone.fleet.card_slicing import classify_card_scope, recommend_decomposition


def _card(count: int, **overrides):
    card = {
        "id": f"parent-{count}",
        "title": "Partition this work",
        "repository": "https://github.com/example/project",
        "base_ref": "main",
        "deliverables": [f"deliverable-{i}" for i in range(count)],
        "acceptance_criteria": [f"criterion-{i}" for i in range(count)],
        "verification_surfaces": [f"tests/surface_{i}.py" for i in range(count)],
        "focused_gates": [f"python -m pytest -q tests/surface_{i}.py" for i in range(count)],
        "mutation_boundaries": [f"src/part_{i}.py" for i in range(count)],
    }
    card.update(overrides)
    return card


def _flatten(leaves, field):
    return tuple(item for leaf in leaves for item in getattr(leaf, field))


def _assert_exact_partition(result, card):
    contract = result.composition_verification
    assert contract is not None
    assert contract.leaf_ids == tuple(leaf.id for leaf in result.leaves)
    for field, source in (
        ("deliverables", "deliverables"),
        ("acceptance_criteria", "acceptance_criteria"),
        ("verification_scope", "verification_surfaces"),
        ("focused_gates", "focused_gates"),
    ):
        assigned = _flatten(result.leaves, field)
        expected = tuple(card[source])
        assert sorted(assigned) == sorted(expected)
        assert len(assigned) == len(set(assigned))
        assert getattr(contract, field) == expected
    assert len(contract.coverage_sha256) == 64
    assert "parent composition and full-suite verification pass" in contract.checks


def test_small_card_is_bounded():
    result = recommend_decomposition({"id": "small", "acceptance_criteria": ["one"]})
    assert result.decision == "bounded"
    assert result.leaves == ()


def test_two_leaf_partition_has_concrete_bounded_contracts():
    card = _card(2, dependencies=["prerequisite-b", "prerequisite-a"])
    result = recommend_decomposition(card)
    assert result.decision == "reject"
    assert len(result.leaves) == 2
    assert result.parent_dependencies == tuple(leaf.id for leaf in result.leaves)
    assert all(leaf.depends_on == ("prerequisite-a", "prerequisite-b") for leaf in result.leaves)
    assert all(leaf.deliverables and leaf.acceptance_criteria for leaf in result.leaves)
    assert all(leaf.verification_scope and leaf.focused_gates for leaf in result.leaves)
    _assert_exact_partition(result, card)


def test_five_leaf_partition_covers_parent_without_overlap_or_omission():
    card = _card(
        5,
        repositories=[f"repo-{i}" for i in range(5)],
        external_effects=[f"effect-{i}" for i in range(5)],
    )
    result = recommend_decomposition(card)
    assert len(result.leaves) == 5
    _assert_exact_partition(result, card)
    assert sorted(_flatten(result.leaves, "mutation_boundaries")) == sorted(
        card["mutation_boundaries"]
    )
    assert sorted(_flatten(result.leaves, "external_effects")) == sorted(card["external_effects"])


def test_partition_is_deterministic():
    card = _card(5, dependencies=["z", "a"])
    assert recommend_decomposition(card) == recommend_decomposition(card)


def test_unsliceable_card_is_rejected_without_placeholder_leaves():
    card = _card(3)
    card["focused_gates"] = ["python -m pytest -q tests/all.py"]
    result = recommend_decomposition(card)
    assert result.decision == "reject"
    assert result.reason.startswith("card is unsliceable")
    assert result.leaves == ()
    assert result.composition_verification is None


def test_missing_checkout_identity_fails_closed():
    card = _card(2)
    del card["base_ref"]
    result = recommend_decomposition(card)
    assert result.decision == "advisory"
    assert result.reason == "repository and base_ref are required for leaf recommendations"


def test_invalid_dependency_fails_closed():
    result = recommend_decomposition(_card(2, dependencies=["valid", ""]))
    assert result.decision == "advisory"
    assert result.reason == "dependencies must be non-empty card ids"


def test_active_and_review_work_are_advisory():
    assert recommend_decomposition(_card(2, status="doing", owner="agent")).decision == "advisory"
    assert recommend_decomposition(_card(2, kind="review")).decision == "advisory"


def test_epic_retains_composition_custody():
    result = recommend_decomposition(_card(2, labels=["epic"]))
    assert result.decision == "reject"
    assert result.custody == "composition"


def test_classification_uses_structural_signals_not_model_family():
    signals = classify_card_scope(
        {"model": "large", "deliverables": ["a", "b"], "external_effects": ["x", "y"]}
    )
    assert signals.deliverables == 2
    assert signals.external_effects == 2
    assert signals.independent_axes == 2


def test_existing_successor_is_not_recommended_but_parent_contract_is_stable():
    card = _card(2)
    first = recommend_decomposition(card)
    rerun = recommend_decomposition({**card, "successors": [first.leaves[0].id]})
    assert tuple(leaf.id for leaf in rerun.leaves) == (first.leaves[1].id,)
    assert rerun.parent_dependencies == first.parent_dependencies
    assert rerun.composition_verification == first.composition_verification


def test_no_artificial_sibling_dependencies_or_parent_leaf_cycle():
    result = recommend_decomposition(_card(5, dependencies=["real-prerequisite"]))
    leaf_ids = {leaf.id for leaf in result.leaves}
    assert all(leaf.depends_on == ("real-prerequisite",) for leaf in result.leaves)
    assert all(not leaf_ids.intersection(leaf.depends_on) for leaf in result.leaves)
    assert all("parent-5" not in leaf.depends_on for leaf in result.leaves)


def test_leaf_count_is_always_bounded_between_two_and_five():
    assert len(recommend_decomposition(_card(2), max_leaves=1).leaves) == 2
    assert len(recommend_decomposition(_card(10), max_leaves=99).leaves) == 5
