import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/fleet/link-lineage.py"
spec = importlib.util.spec_from_file_location("link_lineage", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def _home(tmp_path, *ids):
    for card_id in ids:
        events = tmp_path / "cards" / card_id / "events"
        events.mkdir(parents=True)
        (events / "001.jsonl").write_text(json.dumps({"event_id": f"rev-{card_id}"}) + "\n")


def _cards():
    return [
        {"id": "source01", "title": "Implement PR #7", "labels": []},
        {
            "id": "review01",
            "title": "Independent review PR #7",
            "labels": ["review", "parent-source01"],
            "status": "done",
            "links": {"verdict": "PASS"},
        },
    ]


def test_complete_emits_producer_contract(tmp_path):
    _home(tmp_path, "source01", "review01")
    out = mod.reconcile(
        [{"repository": "org/repo", "number": 7, "headRefOid": "a" * 40, "baseRefOid": "b" * 40}],
        _cards(),
        tmp_path,
    )
    assert out["schema"] == "skfleet.link-lineage/v1"
    assert out["records"]["org/repo#7"]["review_card_id"] == "review01"
    assert len(out["records"]["org/repo#7"]["card_generation"]) == 64
    assert out["records"]["org/repo#7"]["base_revision"] == "b" * 40


def test_incomplete_is_not_emitted_as_record(tmp_path):
    _home(tmp_path, "source01")
    out = mod.reconcile(
        [{"repository": "org/repo", "number": 7}],
        [{"id": "source01", "title": "Implement PR #7"}],
        tmp_path,
    )
    assert out["records"] == {}
    assert out["coverage"]["unresolved"] == 1


def test_malformed_cardstore_fails_closed(tmp_path):
    events = tmp_path / "cards" / "source01" / "events"
    events.mkdir(parents=True)
    (events / "001.jsonl").write_text("not-json\n")
    try:
        mod.reconcile([{"repository": "org/repo", "number": 7}], _cards(), tmp_path)
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("malformed CardStore evidence was accepted")


def test_output_is_deterministic(tmp_path):
    _home(tmp_path, "source01", "review01")
    prs = [{"repository": "org/repo", "number": 7, "headRefOid": "a" * 40}]
    a = mod.reconcile(prs, _cards(), tmp_path)
    b = mod.reconcile(list(reversed(prs)), list(reversed(_cards())), tmp_path)
    assert json.dumps(a, sort_keys=True, separators=(",", ":")) == json.dumps(
        b, sort_keys=True, separators=(",", ":")
    )


def test_missing_repository_is_rejected(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="missing repository"):
        mod.reconcile([{"number": 1}], [], tmp_path)


def test_authoritative_reviewer_contract_is_consumed(tmp_path):
    candidate = {
        "schema": "skfleet.reviewer-identity/v1",
        "name": "Seraph",
        "seat": "seraph",
        "identity": "seraph@casey.skworld.io",
        "host": "chiap08",
        "session": "seraph-card-scoped",
        "workspace": "/home/skuser01/work/skcapstone",
        "fingerprint": "a" * 64,
    }
    report = mod.reconcile([], [], tmp_path, reviewer_candidates=[candidate])
    assert report["reviewer_candidates"][0]["identity"] == candidate["identity"]


def test_malformed_reviewer_identity_is_rejected(tmp_path):
    import pytest

    bad = {
        "schema": "skfleet.reviewer-identity/v1",
        "name": "",
        "seat": "seraph",
        "identity": "seraph@casey.skworld.io",
        "host": "chiap08",
        "session": "seraph-card-scoped",
        "workspace": "/home/skuser01/work/skcapstone",
        "fingerprint": "bad",
    }
    with pytest.raises(ValueError, match="malformed|required"):
        mod.reconcile([], [], tmp_path, reviewer_candidates=[bad])


def test_empty_authoritative_reviewer_input_is_rejected(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="non-empty"):
        mod.reconcile([], [], tmp_path, reviewer_candidates=[])


def test_non_boolean_reviewer_eligibility_is_rejected(tmp_path):
    import pytest

    candidate = {
        "schema": "skfleet.reviewer-identity/v1",
        "name": "Seraph",
        "seat": "seraph",
        "identity": "seraph@casey.skworld.io",
        "host": "chiap08",
        "session": "seraph-card-scoped",
        "workspace": "/home/skuser01/work/skcapstone",
        "fingerprint": "a" * 64,
        "eligible": "false",
    }
    with pytest.raises(ValueError, match="eligibility"):
        mod.reconcile([], [], tmp_path, reviewer_candidates=[candidate])


def test_jarvis_name_spoof_with_seraph_seat_is_rejected(tmp_path):
    import pytest

    candidate = {
        "schema": "skfleet.reviewer-identity/v1",
        "name": "Jarvis",
        "seat": "seraph",
        "identity": "seraph@casey.skworld.io",
        "host": "chiap08",
        "session": "seraph-card-scoped",
        "workspace": "/home/skuser01/work/skcapstone",
        "fingerprint": "a" * 64,
        "eligible": True,
    }
    with pytest.raises(ValueError, match="malformed"):
        mod.reconcile([], [], tmp_path, reviewer_candidates=[candidate])


def test_pr_body_card_references_resolve_lineage_without_pr_number(tmp_path):
    _home(tmp_path, "a1b2c3d4", "e5f6a7b8")
    cards = [
        {"id": "a1b2c3d4", "title": "Implement source change", "labels": []},
        {
            "id": "e5f6a7b8",
            "title": "Independent review",
            "labels": ["review", "parent-a1b2c3d4"],
            "status": "done",
            "links": {"verdict": "PASS"},
        },
    ]
    out = mod.reconcile(
        [
            {
                "repository": "org/repo",
                "number": 9,
                "body": "Source card a1b2c3d4; review e5f6a7b8",
                "headRefOid": "a" * 40,
                "baseRefOid": "b" * 40,
            }
        ],
        cards,
        tmp_path,
    )
    assert out["coverage"]["lineage-complete"] == 1
    assert out["records"]["org/repo#9"]["source_card"] == "a1b2c3d4"


def test_legacy_review_title_is_not_treated_as_source(tmp_path):
    _home(tmp_path, "a1b2c3d4", "e5f6a7b8")
    cards = [
        {"id": "a1b2c3d4", "title": "Implement source", "labels": []},
        {
            "id": "e5f6a7b8",
            "title": "[REVIEW] Check source",
            "labels": ["parent-a1b2c3d4"],
            "status": "done",
            "links": {"verdict": "PASS"},
        },
    ]
    out = mod.reconcile(
        [
            {
                "repository": "org/repo",
                "number": 10,
                "body": "a1b2c3d4",
                "headRefOid": "a" * 40,
                "baseRefOid": "b" * 40,
            }
        ],
        cards,
        tmp_path,
    )
    assert out["coverage"]["lineage-complete"] == 1


def test_pass_for_review_is_not_terminal_pass(tmp_path):
    _home(tmp_path, "a1b2c3d4", "e5f6a7b8")
    cards = [
        {"id": "a1b2c3d4", "title": "Implement source", "labels": []},
        {
            "id": "e5f6a7b8",
            "title": "[REVIEW] Check source",
            "labels": ["review", "parent-a1b2c3d4"],
            "status": "done",
            "links": {"verdict": "PASS_FOR_REVIEW"},
        },
    ]
    out = mod.reconcile(
        [
            {
                "repository": "org/repo",
                "number": 11,
                "body": "a1b2c3d4",
                "headRefOid": "a" * 40,
                "baseRefOid": "b" * 40,
            }
        ],
        cards,
        tmp_path,
    )
    assert out["coverage"]["unresolved"] == 1


def test_terminal_fail_is_reconciled_but_not_a_pass(tmp_path):
    _home(tmp_path, "a1b2c3d4", "e5f6a7b8")
    cards = [
        {"id": "a1b2c3d4", "title": "Implement source", "labels": []},
        {
            "id": "e5f6a7b8",
            "title": "[REVIEW] Check source",
            "labels": ["review", "parent-a1b2c3d4"],
            "status": "done",
            "links": {"verdict": "FAIL"},
        },
    ]
    out = mod.reconcile(
        [
            {
                "repository": "org/repo",
                "number": 12,
                "body": "a1b2c3d4",
                "headRefOid": "a" * 40,
                "baseRefOid": "b" * 40,
            }
        ],
        cards,
        tmp_path,
    )
    assert out["coverage"]["lineage-complete"] == 1
    assert out["records"]["org/repo#12"]["review_verdict"] == "FAIL"


def test_review_required_source_label_is_not_reviewer(tmp_path):
    _home(tmp_path, "a1b2c3d4", "e5f6a7b8")
    cards = [
        {"id": "a1b2c3d4", "title": "Implement source", "labels": ["review-required"]},
        {
            "id": "e5f6a7b8",
            "title": "Independent review",
            "labels": ["review", "parent-a1b2c3d4"],
            "status": "done",
            "links": {"verdict": "PASS"},
        },
    ]
    out = mod.reconcile(
        [
            {
                "repository": "org/repo",
                "number": 13,
                "body": "a1b2c3d4",
                "headRefOid": "a" * 40,
                "baseRefOid": "b" * 40,
            }
        ],
        cards,
        tmp_path,
    )
    assert out["coverage"]["lineage-complete"] == 1


def test_unique_terminal_review_wins_over_historical_nonterminal_review(tmp_path):
    _home(tmp_path, "a1b2c3d4", "e5f6a7b8", "e5f6a7b9")
    cards = [
        {"id": "a1b2c3d4", "title": "Implement PR #14", "labels": []},
        {
            "id": "e5f6a7b8",
            "title": "[REVIEW] Historical review",
            "labels": ["parent-a1b2c3d4"],
            "status": "done",
            "links": {"verdict": "PASS_FOR_REVIEW"},
        },
        {
            "id": "e5f6a7b9",
            "title": "[REVIEW] Current review",
            "labels": ["parent-a1b2c3d4"],
            "status": "done",
            "links": {"verdict": "FAIL"},
        },
    ]
    out = mod.reconcile(
        [{"repository": "org/repo", "number": 14, "headRefOid": "a" * 40, "baseRefOid": "b" * 40}],
        cards,
        tmp_path,
    )
    assert out["records"]["org/repo#14"]["review_card_id"] == "e5f6a7b9"


def test_exact_pr_and_head_binding_resolves_multiple_terminal_reviews(tmp_path):
    head = "a" * 40
    _home(tmp_path, "a1b2c3d4", "e5f6a7b8", "e5f6a7b9")
    cards = [
        {"id": "a1b2c3d4", "title": "Implement PR #15", "labels": []},
        {
            "id": "e5f6a7b8",
            "title": "[REVIEW] Historical review",
            "labels": ["parent-a1b2c3d4"],
            "status": "done",
            "links": {"verdict": "PASS"},
        },
        {
            "id": "e5f6a7b9",
            "title": "[REVIEW] Current review",
            "labels": ["parent-a1b2c3d4"],
            "status": "done",
            "links": {
                "verdict": "FAIL",
                "pr": "https://github.com/org/repo/pull/15",
                "commit": head,
            },
        },
    ]
    out = mod.reconcile(
        [{"repository": "org/repo", "number": 15, "headRefOid": head, "baseRefOid": "b" * 40}],
        cards,
        tmp_path,
    )
    record = out["records"]["org/repo#15"]
    assert record["review_card_id"] == "e5f6a7b9"
    assert record["review_verdict"] == "FAIL"


def test_review_bound_to_different_pr_or_head_is_not_reused(tmp_path):
    head = "a" * 40
    _home(tmp_path, "a1b2c3d4", "e5f6a7b8")
    cards = [
        {"id": "a1b2c3d4", "title": "Implement PR #16", "labels": []},
        {
            "id": "e5f6a7b8",
            "title": "[REVIEW] Different PR review",
            "labels": ["parent-a1b2c3d4"],
            "status": "done",
            "links": {
                "verdict": "PASS",
                "pr": "https://github.com/org/repo/pull/99",
                "commit": "b" * 40,
            },
        },
    ]
    out = mod.reconcile(
        [{"repository": "org/repo", "number": 16, "headRefOid": head}],
        cards,
        tmp_path,
    )
    assert out["coverage"]["unresolved"] == 1
    assert out["records"] == {}


def _linked_source(source_id, repository="org/repo", number=17, commit=None):
    return {
        "id": source_id,
        "title": "Source card without a PR title reference",
        "labels": [],
        "links": {
            "pr": f"https://github.com/{repository}/pull/{number}",
            "commit": commit or "a" * 40,
        },
    }


def _linked_review(review_id, source_id):
    return {
        "id": review_id,
        "title": "Independent review",
        "labels": ["review", f"parent-{source_id}"],
        "status": "done",
        "links": {"verdict": "PASS"},
    }


def test_exact_folded_source_links_resolve_lineage(tmp_path):
    head = "a" * 40
    _home(tmp_path, "source01", "review01")
    out = mod.reconcile(
        [{"repository": "org/repo", "number": 17, "headRefOid": head}],
        [_linked_source("source01", commit=head), _linked_review("review01", "source01")],
        tmp_path,
    )
    assert out["coverage"]["lineage-complete"] == 1
    assert out["records"]["org/repo#17"]["source_card"] == "source01"


@pytest.mark.parametrize(
    "links",
    [
        {"pr": "https://github.com/org/repo/pull/17"},
        {"pr": "https://github.com/org/repo/pull/17", "commit": "a" * 7},
        {"pr": "https://github.com/other/repo/pull/17", "commit": "a" * 40},
        {"pr": "https://github.com/org/repo/pull/17", "commit": "b" * 40},
        {"pr": "not-a-pull-request", "commit": "a" * 40},
    ],
)
def test_folded_source_links_fail_closed(tmp_path, links):
    head = "a" * 40
    _home(tmp_path, "source01", "review01")
    source = _linked_source("source01")
    source["links"] = links
    out = mod.reconcile(
        [{"repository": "org/repo", "number": 17, "headRefOid": head}],
        [source, _linked_review("review01", "source01")],
        tmp_path,
    )
    assert out["coverage"]["unresolved"] == 1
    assert out["records"] == {}


def test_multiple_exact_folded_source_links_remain_unresolved(tmp_path):
    head = "a" * 40
    _home(tmp_path, "source01", "source02", "review01")
    out = mod.reconcile(
        [{"repository": "org/repo", "number": 17, "headRefOid": head}],
        [
            _linked_source("source01", commit=head),
            _linked_source("source02", commit=head),
            _linked_review("review01", "source01"),
        ],
        tmp_path,
    )
    assert out["coverage"]["unresolved"] == 1
    assert out["records"] == {}


def _exclusion(classification="unmanaged", **changes):
    record = {
        "repository": "org/repo",
        "pr": 17,
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "owner": "link",
        "reason": "bounded evidence-backed exclusion",
        "classification": classification,
        "source_evidence_sha256": "c" * 64,
        "expires_at": "2099-01-01T00:00:00+00:00",
    }
    record.update(changes)
    return {"schema": "skfleet.link-lineage-exclusion/v1", "records": [record]}


@pytest.mark.parametrize("classification", ["unmanaged", "review-artifact", "superseded"])
def test_structured_exclusion_classes_are_accepted(tmp_path, classification):
    out = mod.reconcile(
        [{"repository": "org/repo", "number": 17, "headRefOid": "a" * 40, "baseRefOid": "b" * 40}],
        [],
        tmp_path,
        _exclusion(classification),
    )
    assert out["coverage"] == {"excluded": 1, "unresolved": 0, "lineage-complete": 0}
    assert out["diagnostics"][0]["exclusion_classification"] == classification


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"head_sha": "d" * 40}, "head SHA"),
        ({"base_sha": "d" * 40}, "base SHA"),
        ({"expires_at": "2000-01-01T00:00:00+00:00"}, "future"),
        ({"source_evidence_sha256": "bad"}, "hash"),
        ({"classification": "unknown"}, "unknown"),
        ({"repository": "other/repo"}, "repository"),
    ],
)
def test_exclusion_validation_fails_closed(tmp_path, changes, message):
    import pytest

    with pytest.raises(ValueError, match=message):
        mod.reconcile(
            [
                {
                    "repository": "org/repo",
                    "number": 17,
                    "headRefOid": "a" * 40,
                    "baseRefOid": "b" * 40,
                }
            ],
            [],
            tmp_path,
            _exclusion(**changes),
        )


def test_duplicate_exclusion_pr_fails_closed(tmp_path):
    import pytest

    exclusion = _exclusion()
    exclusion["records"].append(dict(exclusion["records"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        mod.reconcile(
            [
                {
                    "repository": "org/repo",
                    "number": 17,
                    "headRefOid": "a" * 40,
                    "baseRefOid": "b" * 40,
                }
            ],
            [],
            tmp_path,
            exclusion,
        )


def test_unknown_exclusion_pr_fails_closed(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="unknown"):
        mod.reconcile(
            [
                {
                    "repository": "org/repo",
                    "number": 17,
                    "headRefOid": "a" * 40,
                    "baseRefOid": "b" * 40,
                }
            ],
            [],
            tmp_path,
            _exclusion(pr=999),
        )


def test_legacy_reason_map_is_rejected(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="structured"):
        mod.reconcile(
            [
                {
                    "repository": "org/repo",
                    "number": 17,
                    "headRefOid": "a" * 40,
                    "baseRefOid": "b" * 40,
                }
            ],
            [],
            tmp_path,
            {"17": "legacy reason"},
        )
