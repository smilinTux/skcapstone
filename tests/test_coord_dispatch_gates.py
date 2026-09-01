"""The gate that cost us: a title marker is permanent, a label is not."""

from skcapstone.coord_dispatch_gates import blocking, evaluate, permanent


def by_name(gates, name):
    return next(g for g in gates if g.name == name)


def test_human_in_title_is_permanent():
    g = by_name(evaluate("[SKGW-06A6][S][HUMAN] Authorize a lifecycle", []), "human-gate")
    assert g.blocks is True
    assert g.removable is False, "a title marker must be reported as unfixable"


def test_human_gate_label_is_removable():
    g = by_name(evaluate("[SKGW-06A6][S] Authorize a lifecycle", ["human-gate"]), "human-gate")
    assert g.blocks is True
    assert g.removable is True


def test_removing_the_label_clears_it():
    g = by_name(evaluate("[SKGW-06A6][S] Authorize a lifecycle", []), "human-gate")
    assert g.blocks is False


def test_do_not_claim_blocks_and_is_removable():
    g = by_name(evaluate("Anything", ["do-not-claim"]), "not-claimable")
    assert (g.blocks, g.removable) == (True, True)


def test_sensitive_category_matches_a_bare_word_in_the_title():
    # The regex is textual on purpose: "release" alone is enough.
    g = by_name(evaluate("Fix the release notes typo", []), "sensitive-category")
    assert g.blocks is True
    g2 = by_name(evaluate("Fix the release notes typo", ["dispatch-approved"]),
                 "sensitive-category")
    assert g2.blocks is False


def test_sensitive_category_matches_labels_too():
    g = by_name(evaluate("Harmless title", ["credential"]), "sensitive-category")
    assert g.blocks is True


def test_governed_class_needs_exactly_one_parent():
    assert by_name(evaluate("[X][REPAIR] thing", []), "governed-class").blocks is True
    assert by_name(evaluate("[X][REPAIR] thing", ["parent-aaaa1111"]),
                   "governed-class").blocks is False
    assert by_name(evaluate("[X][REPAIR] thing", ["parent-aaaa1111", "parent-bbbb2222"]),
                   "governed-class").blocks is True


def test_the_real_card_that_caused_this():
    """983336c1 exactly as authored on 2026-09-01.

    Note what is NOT here. It was claimed during the incident that this card was
    also sensitive-category "because it mentions credentials". It does not: the
    title says service-token, and neither the title nor any label matches the
    regex. The card was gated by the [HUMAN] title and do-not-claim, and by
    nothing else. This test exists partly to keep that correction honest.
    """
    gates = evaluate(
        "[SKGW-AUTHZ-06A6-LC][S][HUMAN] Authorize a replacement service-token "
        "lifecycle for the qualification consumer",
        ["parent-9acf44e2", "skgateway", "sklegal", "authz", "service-token",
         "human-approval-required", "prerequisite", "do-not-claim"],
    )
    names = {g.name for g in blocking(gates)}
    assert names == {"human-gate", "not-claimable"}
    assert by_name(gates, "sensitive-category").blocks is False
    assert [g.name for g in permanent(gates)] == ["human-gate"], \
        "only the title-derived gate is unfixable"


def test_a_credential_card_IS_sensitive_category():
    """The sibling card really does trip it, which is why the two look alike."""
    gates = evaluate(
        "[SKGW-AUTHZ-06A5-DBLC][L] Provision one fresh isolated qualification "
        "database credential lifecycle",
        ["skgateway", "sklegal", "authz", "database-credential"],
    )
    assert by_name(gates, "sensitive-category").blocks is True


def test_a_clean_card_is_dispatchable():
    assert blocking(evaluate("[SKX-01][S] Add a nav icon", ["skdashboard"])) == []
