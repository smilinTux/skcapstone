import importlib.util
import json
from pathlib import Path

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
            "links": {"verdict": "PASS", "pr": "org/repo#7", "commit": "a" * 40},
        },
    ]


def _reviewer():
    return {
        "schema": "skfleet.reviewer-identity/v1",
        "name": "Seraph",
        "seat": "seraph",
        "identity": "seraph@casey.skworld.io",
        "host": "chiap08",
        "session": "seraph-card-scoped",
        "workspace": "/work/skcapstone",
        "fingerprint": "a" * 64,
        "eligible": True,
    }


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


def test_single_source_missing_review_emits_exact_bounded_review_work(tmp_path):
    _home(tmp_path, "source01")
    out = mod.reconcile(
        [{"repository": "org/repo", "number": 7, "headRefOid": "a" * 40, "baseRefOid": "b" * 40}],
        [
            {
                "id": "source01",
                "title": "Implement PR #7",
                "owner": "builder",
                "links": {
                    "repository": "https://github.com/org/repo",
                    "base_ref": "main",
                },
            }
        ],
        tmp_path,
        reviewer_candidates=[_reviewer()],
    )
    assert out["records"] == {}
    assert out["coverage"]["unresolved"] == 1
    assert out["review_work_recommendations"] == [
        {
            "kind": "review-work",
            "reason": "missing_terminal_review",
            "repository": "org/repo",
            "workspace_repository": "https://github.com/org/repo",
            "base_ref": "main",
            "pr": 7,
            "head_revision": "a" * 40,
            "base_revision": "b" * 40,
            "source_card": "source01",
            "card_generation": out["review_work_recommendations"][0]["card_generation"],
            "source_owner": "builder",
            "reviewer_candidates": [_reviewer()],
        }
    ]


def test_stale_head_review_emits_work_but_orphan_and_ambiguity_do_not(tmp_path):
    _home(tmp_path, "source01", "source02", "review01")
    cards = _cards()
    cards[1]["links"]["commit"] = "c" * 40
    cards.append({"id": "source02", "title": "Implement PR #9", "labels": []})
    cards.append({"id": "source01", "title": "Also PR #9", "labels": []})
    out = mod.reconcile(
        [
            {
                "repository": "org/repo",
                "number": 7,
                "headRefOid": "a" * 40,
                "baseRefOid": "b" * 40,
            },
            {
                "repository": "org/repo",
                "number": 8,
                "headRefOid": "d" * 40,
                "baseRefOid": "e" * 40,
            },
            {
                "repository": "org/repo",
                "number": 9,
                "headRefOid": "f" * 40,
                "baseRefOid": "1" * 40,
            },
        ],
        cards,
        tmp_path,
        reviewer_candidates=[_reviewer()],
    )
    assert [(item["pr"], item["reason"]) for item in out["review_work_recommendations"]] == [
        (7, "review_not_bound_to_head")
    ]


def test_source_owner_cannot_be_its_reviewer(tmp_path):
    _home(tmp_path, "source01")
    out = mod.reconcile(
        [{"repository": "org/repo", "number": 7, "headRefOid": "a" * 40, "baseRefOid": "b" * 40}],
        [{"id": "source01", "title": "Implement PR #7", "owner": _reviewer()["identity"]}],
        tmp_path,
        reviewer_candidates=[_reviewer()],
    )
    assert out["review_work_recommendations"] == []


def test_source_originator_cannot_be_its_reviewer(tmp_path):
    _home(tmp_path, "source01")
    out = mod.reconcile(
        [
            {
                "repository": "org/repo",
                "number": 7,
                "headRefOid": "a" * 40,
                "baseRefOid": "b" * 40,
            }
        ],
        [
            {
                "id": "source01",
                "title": "Implement PR #7",
                "originator": _reviewer()["identity"],
                "owner": "different-current-owner",
                "created_by": "different-creator",
            }
        ],
        tmp_path,
        reviewer_candidates=[_reviewer()],
    )
    assert out["review_work_recommendations"] == []


def test_review_work_is_deterministically_bounded(tmp_path):
    ids = [f"{number:08x}" for number in range(60)]
    _home(tmp_path, *ids)
    prs = [
        {
            "repository": "org/repo",
            "number": number,
            "body": ids[number - 1],
            "headRefOid": f"{number:040x}",
            "baseRefOid": "b" * 40,
        }
        for number in range(1, 61)
    ]
    cards = [{"id": card_id, "title": "Source", "created_by": "builder"} for card_id in ids]
    out = mod.reconcile(prs, cards, tmp_path, reviewer_candidates=[_reviewer()])
    assert len(out["review_work_recommendations"]) == 50
    assert [item["pr"] for item in out["review_work_recommendations"]] == list(range(1, 51))


def test_malformed_cardstore_fails_closed(tmp_path):
    events = tmp_path / "cards" / "source01" / "events"
    events.mkdir(parents=True)
    (events / "001.jsonl").write_text("not-json\n")
    try:
        mod.reconcile(
            [{"repository": "org/repo", "number": 7, "headRefOid": "a" * 40}],
            _cards(),
            tmp_path,
        )
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
            "links": {"verdict": "PASS", "pr": "org/repo#9", "commit": "a" * 40},
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
            "links": {"verdict": "PASS", "pr": "org/repo#10", "commit": "a" * 40},
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
            "links": {"verdict": "FAIL", "pr": "org/repo#12", "commit": "a" * 40},
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
            "links": {"verdict": "PASS", "pr": "org/repo#13", "commit": "a" * 40},
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
            "links": {"verdict": "FAIL", "pr": "org/repo#14", "commit": "a" * 40},
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


def test_unique_terminal_review_bound_to_different_pr_is_unresolved(tmp_path):
    head = "a" * 40
    _home(tmp_path, "a1b2c3d4", "e5f6a7b8")
    cards = [
        {"id": "a1b2c3d4", "title": "Implement PR #16", "labels": []},
        {
            "id": "e5f6a7b8",
            "title": "[REVIEW] Different PR",
            "labels": ["parent-a1b2c3d4"],
            "status": "done",
            "links": {"verdict": "PASS", "pr": "org/repo#99", "commit": head},
        },
    ]
    out = mod.reconcile(
        [{"repository": "org/repo", "number": 16, "headRefOid": head}],
        cards,
        tmp_path,
    )
    assert out["records"] == {}
    assert out["coverage"]["unresolved"] == 1


def test_same_number_review_url_from_different_repository_is_unresolved(tmp_path):
    head = "a" * 40
    _home(tmp_path, "a1b2c3d4", "e5f6a7b8")
    cards = [
        {"id": "a1b2c3d4", "title": "Implement PR #16", "labels": []},
        {
            "id": "e5f6a7b8",
            "title": "[REVIEW] Wrong repository",
            "labels": ["parent-a1b2c3d4"],
            "status": "done",
            "links": {
                "verdict": "PASS",
                "pr": "https://github.com/evil/other/pull/16",
                "commit": head,
            },
        },
    ]
    out = mod.reconcile(
        [{"repository": "org/repo", "number": 16, "headRefOid": head}],
        cards,
        tmp_path,
    )
    assert out["records"] == {}
    assert out["coverage"]["unresolved"] == 1


def test_unique_terminal_review_bound_to_stale_head_is_unresolved(tmp_path):
    _home(tmp_path, "a1b2c3d4", "e5f6a7b8")
    cards = [
        {"id": "a1b2c3d4", "title": "Implement PR #17", "labels": []},
        {
            "id": "e5f6a7b8",
            "title": "[REVIEW] Stale head",
            "labels": ["parent-a1b2c3d4"],
            "status": "done",
            "links": {"verdict": "PASS", "pr": "org/repo#17", "commit": "b" * 40},
        },
    ]
    out = mod.reconcile(
        [{"repository": "org/repo", "number": 17, "headRefOid": "a" * 40}],
        cards,
        tmp_path,
    )
    assert out["records"] == {}
    assert out["coverage"]["unresolved"] == 1


def test_multiple_head_bound_terminal_reviews_are_unresolved(tmp_path):
    head = "a" * 40
    _home(tmp_path, "a1b2c3d4", "e5f6a7b8", "e5f6a7b9")
    cards = [
        {"id": "a1b2c3d4", "title": "Implement PR #18", "labels": []},
        *[
            {
                "id": card_id,
                "title": "[REVIEW] Bound review",
                "labels": ["parent-a1b2c3d4"],
                "status": "done",
                "links": {"verdict": verdict, "pr": "org/repo#18", "commit": head},
            }
            for card_id, verdict in (("e5f6a7b8", "PASS"), ("e5f6a7b9", "FAIL"))
        ],
    ]
    out = mod.reconcile(
        [{"repository": "org/repo", "number": 18, "headRefOid": head}],
        cards,
        tmp_path,
    )
    assert out["records"] == {}
    assert out["coverage"]["unresolved"] == 1
