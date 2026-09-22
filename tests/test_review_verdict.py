"""A review must record a verdict before it can be completed.

The measurement behind this: of 317 completed review cards on 2026-08-28, 39 had
recorded no verdict. Some are permanently silent; others were a race, where the
worker completed the card and wrote the verdict afterwards. a93fd881 was observed
doing exactly that, reading complete-with-no-verdict and then PASS a minute later.
Requiring the verdict first removes the window.
"""

import json

import pytest

from skcapstone.review_verdict import (
    is_review_card,
    recorded_verdict,
    validate_review_completion,
)

_SUCCESSFUL_CI = (
    ("ci_check_docs", "SUCCESS", "2026-08-28T03:01:00"),
    ("ci_check_gitleaks", "SUCCESS", "2026-08-28T03:01:00"),
    ("ci_check_lint", "SUCCESS", "2026-08-28T03:01:00"),
    ("ci_check_shim_imports", "SUCCESS", "2026-08-28T03:01:00"),
    ("ci_check_python311", "SUCCESS", "2026-08-28T03:01:00"),
    ("ci_check_python312", "SUCCESS", "2026-08-28T03:01:00"),
)


def _home(tmp_path, card_id, title, links=(), meta=None, labels=(), core_links=None):
    card = tmp_path / "cards" / card_id
    card.mkdir(parents=True)
    (card / "core.json").write_text(
        json.dumps(
            {
                "title": title,
                "meta": meta or {},
                "initial_labels": list(labels),
                "links": core_links or {},
            }
        )
    )
    ev = tmp_path / "coordination" / "card_events"
    ev.mkdir(parents=True)
    rows = []
    for link in links:
        k, v, ts, *writer = link
        rows.append(
            json.dumps(
                {
                    "card_id": card_id,
                    "action": "link",
                    "link_key": k,
                    "link_value": v,
                    "ts": ts,
                    "writer": writer[0] if writer else "fixture-writer",
                }
            )
        )
    (ev / "host.jsonl").write_text("\n".join(rows))
    return tmp_path


@pytest.mark.parametrize(
    "title",
    [
        "[SKW-X-01][S][REVIEW] Independently review the thing",
        "[REVIEW-119db735][S][REVIEW] Re-review",
        "[CARD-EVENT-SCHEMA-DESIGN-R1][REVIEW] Review provenance schema",
        "[SKW-X-01][S][REREVIEW] Independently rereview the thing",
        "[REREVIEW-119db735][S] Re-review",
    ],
)
def test_review_cards_are_recognised(title):
    assert is_review_card(title)


def test_source_only_applicability_receipt_replaces_hosted_ci(tmp_path):
    head = "2" * 40
    receipt = json.dumps(
        {
            "type": "source-only-applicability",
            "card_id": "239b24bf",
            "source_head": head,
            "reviewer": "reviewer@example",
            "evidence_digest": "a" * 64,
            "governed_pr_ci": False,
        },
        sort_keys=True,
    )
    home = _home(
        tmp_path,
        "239b24bf",
        "[REVIEW] source-only",
        [
            ("verdict", "PASS", "2026-08-28T03:00:00", "reviewer@example"),
            ("evidence", "review.md", "2026-08-28T03:01:00", "reviewer@example"),
            ("patch_sha256", "a" * 64, "2026-08-28T03:02:00", "reviewer@example"),
            ("applicability_receipt", receipt, "2026-08-28T03:03:00", "reviewer@example"),
        ],
        meta={"link_head_revision": head, "producer_identity": "producer@example"},
        labels=["source-only"],
    )
    validate_review_completion("239b24bf", "[REVIEW] source-only", home)


def test_source_only_supported_label_and_embedded_digest_activate_receipt(tmp_path):
    head = "2" * 40
    digest = "a" * 64
    receipt = json.dumps(
        {
            "type": "source-only-applicability",
            "card_id": "239b24bf",
            "source_head": head,
            "reviewer": "reviewer@example",
            "evidence_digest": digest,
            "governed_pr_ci": False,
        },
        sort_keys=True,
    )
    home = _home(
        tmp_path,
        "239b24bf",
        "[REVIEW] source-only",
        [
            ("verdict", "PASS", "2026-08-28T03:00:00", "reviewer@example"),
            (
                "evidence",
                f"review.md|sha256={digest}",
                "2026-08-28T03:01:00",
                "reviewer@example",
            ),
            ("applicability_receipt", receipt, "2026-08-28T03:03:00", "reviewer@example"),
        ],
        meta={"link_head_revision": head, "producer_identity": "producer@example"},
    )
    event_path = home / "coordination" / "card_events" / "host.jsonl"
    with event_path.open("a") as handle:
        handle.write(
            "\n"
            + json.dumps(
                {
                    "card_id": "239b24bf",
                    "action": "add_label",
                    "label": "source-only",
                    "ts": "2026-08-28T02:59:00",
                    "writer": "coordinator@example",
                }
            )
        )
    validate_review_completion("239b24bf", "[REVIEW] source-only", home)


def test_source_only_supported_remove_label_disables_receipt(tmp_path):
    head = "2" * 40
    digest = "a" * 64
    receipt = json.dumps(
        {
            "type": "source-only-applicability",
            "card_id": "239b24bf",
            "source_head": head,
            "reviewer": "reviewer@example",
            "evidence_digest": digest,
            "governed_pr_ci": False,
        },
        sort_keys=True,
    )
    home = _home(
        tmp_path,
        "239b24bf",
        "[REVIEW] source-only",
        [
            ("verdict", "PASS", "2026-08-28T03:00:00", "reviewer@example"),
            (
                "evidence",
                f"review.md|sha256={digest}",
                "2026-08-28T03:01:00",
                "reviewer@example",
            ),
            ("applicability_receipt", receipt, "2026-08-28T03:03:00", "reviewer@example"),
        ],
        meta={"link_head_revision": head, "producer_identity": "producer@example"},
        labels=["source-only"],
    )
    event_path = home / "coordination" / "card_events" / "host.jsonl"
    with event_path.open("a") as handle:
        handle.write(
            "\n"
            + json.dumps(
                {
                    "card_id": "239b24bf",
                    "action": "remove_label",
                    "label": "source-only",
                    "ts": "2026-08-28T03:04:00",
                    "writer": "coordinator@example",
                }
            )
        )
    with pytest.raises(ValueError, match="required checks"):
        validate_review_completion("239b24bf", "[REVIEW] source-only", home)


@pytest.mark.parametrize("remove_timestamp", ["not-a-time", "2026-08-28T03:04:00"])
def test_source_only_ambiguous_or_invalid_remove_label_fails_closed(tmp_path, remove_timestamp):
    head = "2" * 40
    digest = "a" * 64
    receipt = json.dumps(
        {
            "type": "source-only-applicability",
            "card_id": "239b24bf",
            "source_head": head,
            "reviewer": "reviewer@example",
            "evidence_digest": digest,
            "governed_pr_ci": False,
        },
        sort_keys=True,
    )
    home = _home(
        tmp_path,
        "239b24bf",
        "[REVIEW] source-only",
        [
            ("verdict", "PASS", "2026-08-28T03:00:00", "reviewer@example"),
            ("evidence", "review.md", "2026-08-28T03:01:00", "reviewer@example"),
            ("patch_sha256", digest, "2026-08-28T03:02:00", "reviewer@example"),
            ("applicability_receipt", receipt, "2026-08-28T03:03:00", "reviewer@example"),
        ],
        meta={"link_head_revision": head, "producer_identity": "producer@example"},
        labels=["source-only"],
    )
    event_path = home / "coordination" / "card_events" / "host.jsonl"
    events = [
        {
            "card_id": "239b24bf",
            "action": "remove_label",
            "label": "source-only",
            "ts": remove_timestamp,
            "writer": "coordinator@example",
            "seq": 1,
        }
    ]
    if remove_timestamp != "not-a-time":
        events.insert(
            0,
            {
                "card_id": "239b24bf",
                "action": "add_label",
                "label": "source-only",
                "ts": remove_timestamp,
                "writer": "coordinator@example",
                "seq": 1,
            },
        )
    with event_path.open("a") as handle:
        for event in events:
            handle.write("\n" + json.dumps(event))
    with pytest.raises(ValueError, match="required checks"):
        validate_review_completion("239b24bf", "[REVIEW] source-only", home)


def test_source_only_label_fold_uses_cardstore_raw_timestamp_order(tmp_path):
    head = "2" * 40
    digest = "a" * 64
    receipt = json.dumps(
        {
            "type": "source-only-applicability",
            "card_id": "239b24bf",
            "source_head": head,
            "reviewer": "reviewer@example",
            "evidence_digest": digest,
            "governed_pr_ci": False,
        },
        sort_keys=True,
    )
    home = _home(
        tmp_path,
        "239b24bf",
        "[REVIEW] source-only",
        [
            ("verdict", "PASS", "2026-08-28T03:30:00+00:00", "reviewer@example"),
            ("evidence", "review.md", "2026-08-28T03:31:00+00:00", "reviewer@example"),
            ("patch_sha256", digest, "2026-08-28T03:32:00+00:00", "reviewer@example"),
            (
                "applicability_receipt",
                receipt,
                "2026-08-28T04:00:00+00:00",
                "reviewer@example",
            ),
        ],
        meta={"link_head_revision": head, "producer_identity": "producer@example"},
    )
    event_path = home / "coordination" / "card_events" / "host.jsonl"
    with event_path.open("a") as handle:
        for event in (
            {
                "card_id": "239b24bf",
                "action": "add_label",
                "label": "source-only",
                "ts": "2026-08-28T02:30:00-01:00",
                "writer": "coordinator@example",
            },
            {
                "card_id": "239b24bf",
                "action": "remove_label",
                "label": "source-only",
                "ts": "2026-08-28T03:00:00+00:00",
                "writer": "coordinator@example",
            },
        ):
            handle.write("\n" + json.dumps(event))
    with pytest.raises(ValueError, match="required checks"):
        validate_review_completion("239b24bf", "[REVIEW] source-only", home)


def test_source_only_evidence_fold_uses_cardstore_raw_timestamp_order(tmp_path):
    head = "2" * 40
    digest = "a" * 64
    receipt = json.dumps(
        {
            "type": "source-only-applicability",
            "card_id": "239b24bf",
            "source_head": head,
            "reviewer": "reviewer@example",
            "evidence_digest": digest,
            "governed_pr_ci": False,
        },
        sort_keys=True,
    )
    home = _home(
        tmp_path,
        "239b24bf",
        "[REVIEW] source-only",
        [
            ("verdict", "PASS", "2026-08-28T03:30:00+00:00", "reviewer@example"),
            ("evidence", "review.md", "2026-08-28T03:31:00+00:00", "reviewer@example"),
            ("patch_sha256", digest, "2026-08-28T02:30:00-01:00", "reviewer@example"),
            ("patch_sha256", "b" * 64, "2026-08-28T03:00:00+00:00", "reviewer@example"),
            (
                "applicability_receipt",
                receipt,
                "2026-08-28T04:00:00+00:00",
                "reviewer@example",
            ),
        ],
        meta={"link_head_revision": head, "producer_identity": "producer@example"},
        labels=["source-only"],
    )
    with pytest.raises(ValueError, match="required checks"):
        validate_review_completion("239b24bf", "[REVIEW] source-only", home)


@pytest.mark.parametrize(
    "evidence_path",
    [
        "review.md|sha256=bogus",
        "review.md#sha256=bogus",
        f"review.md|sha256={'a' * 64}#sha256={'a' * 64}",
    ],
)
def test_source_only_malformed_or_duplicate_digest_marker_fails_closed(tmp_path, evidence_path):
    head = "2" * 40
    digest = "a" * 64
    receipt = json.dumps(
        {
            "type": "source-only-applicability",
            "card_id": "239b24bf",
            "source_head": head,
            "reviewer": "reviewer@example",
            "evidence_digest": digest,
            "governed_pr_ci": False,
        },
        sort_keys=True,
    )
    home = _home(
        tmp_path,
        "239b24bf",
        "[REVIEW] source-only",
        [
            ("verdict", "PASS", "2026-08-28T03:00:00", "reviewer@example"),
            ("evidence", evidence_path, "2026-08-28T03:01:00", "reviewer@example"),
            ("patch_sha256", digest, "2026-08-28T03:02:00", "reviewer@example"),
            ("applicability_receipt", receipt, "2026-08-28T03:03:00", "reviewer@example"),
        ],
        meta={"link_head_revision": head, "producer_identity": "producer@example"},
        labels=["source-only"],
    )
    with pytest.raises(ValueError, match="required checks"):
        validate_review_completion("239b24bf", "[REVIEW] source-only", home)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("patch_sha256", "b" * 64),
        ("evidence", "conflicting-review.md"),
        ("verdict", "PASS"),
    ],
)
def test_source_only_invalid_timestamp_support_mutation_fails_closed(tmp_path, key, value):
    head = "2" * 40
    digest = "a" * 64
    receipt = json.dumps(
        {
            "type": "source-only-applicability",
            "card_id": "239b24bf",
            "source_head": head,
            "reviewer": "reviewer@example",
            "evidence_digest": digest,
            "governed_pr_ci": False,
        },
        sort_keys=True,
    )
    home = _home(
        tmp_path,
        "239b24bf",
        "[REVIEW] source-only",
        [
            ("verdict", "PASS", "2026-08-28T03:00:00", "reviewer@example"),
            ("evidence", "review.md", "2026-08-28T03:01:00", "reviewer@example"),
            ("patch_sha256", digest, "2026-08-28T03:02:00", "reviewer@example"),
            ("applicability_receipt", receipt, "2026-08-28T03:03:00", "reviewer@example"),
            (key, value, "not-a-time", "reviewer@example"),
        ],
        meta={"link_head_revision": head, "producer_identity": "producer@example"},
        labels=["source-only"],
    )
    with pytest.raises(ValueError, match="required checks"):
        validate_review_completion("239b24bf", "[REVIEW] source-only", home)


@pytest.mark.parametrize("binding_key", ["pr", "open_pr"])
def test_source_only_receipt_is_rejected_when_pr_bound(tmp_path, binding_key):
    head = "3" * 40
    receipt = json.dumps(
        {
            "type": "source-only-applicability",
            "card_id": "7c1f0a2e",
            "source_head": head,
            "reviewer": "reviewer@example",
            "evidence_digest": "b" * 64,
            "governed_pr_ci": False,
        },
        sort_keys=True,
    )
    home = _home(
        tmp_path,
        "7c1f0a2e",
        "[REVIEW] source-only",
        [
            ("verdict", "PASS", "2026-08-28T03:00:00", "reviewer@example"),
            ("evidence", "review.md", "2026-08-28T03:01:00", "reviewer@example"),
            ("evidence_sha256", "b" * 64, "2026-08-28T03:02:00", "reviewer@example"),
            ("applicability_receipt", receipt, "2026-08-28T03:03:00", "reviewer@example"),
            (binding_key, "https://example.invalid/p/1", "2026-08-28T03:04:00"),
        ],
        meta={"link_head_revision": head, "producer_identity": "producer@example"},
        labels=["source-only"],
    )
    with pytest.raises(ValueError, match="required checks"):
        validate_review_completion("7c1f0a2e", "[REVIEW] source-only", home)


@pytest.mark.parametrize(
    ("receipt_update", "event_writer", "evidence_digest"),
    [
        ({"source_head": "4" * 40}, "reviewer@example", "a" * 64),
        ({"reviewer": "producer@example"}, "producer@example", "a" * 64),
        ({}, "forged@example", "a" * 64),
        ({"evidence_digest": "b" * 64}, "reviewer@example", "a" * 64),
        ({"governed_pr_ci": True}, "reviewer@example", "a" * 64),
    ],
)
def test_source_only_receipt_rejects_stale_or_forged_bindings(
    tmp_path, receipt_update, event_writer, evidence_digest
):
    head = "2" * 40
    receipt_data = {
        "type": "source-only-applicability",
        "card_id": "239b24bf",
        "source_head": head,
        "reviewer": "reviewer@example",
        "evidence_digest": "a" * 64,
        "governed_pr_ci": False,
    }
    receipt_data.update(receipt_update)
    home = _home(
        tmp_path,
        "239b24bf",
        "[REVIEW] source-only",
        [
            ("verdict", "PASS", "2026-08-28T03:00:00", "reviewer@example"),
            ("evidence", "review.md", "2026-08-28T03:01:00", "reviewer@example"),
            ("evidence_sha256", evidence_digest, "2026-08-28T03:02:00", "reviewer@example"),
            (
                "applicability_receipt",
                json.dumps(receipt_data, sort_keys=True),
                "2026-08-28T03:03:00",
                event_writer,
            ),
        ],
        meta={"link_head_revision": head, "producer_identity": "producer@example"},
        labels=["source-only"],
    )
    with pytest.raises(ValueError, match="required checks"):
        validate_review_completion("239b24bf", "[REVIEW] source-only", home)


def test_source_only_receipt_rejects_duplicate_or_stale_append(tmp_path):
    head = "2" * 40
    receipt = json.dumps(
        {
            "type": "source-only-applicability",
            "card_id": "239b24bf",
            "source_head": head,
            "reviewer": "reviewer@example",
            "evidence_digest": "a" * 64,
            "governed_pr_ci": False,
        },
        sort_keys=True,
    )
    home = _home(
        tmp_path,
        "239b24bf",
        "[REVIEW] source-only",
        [
            ("verdict", "PASS", "2026-08-28T03:00:00", "reviewer@example"),
            ("evidence", "review.md", "2026-08-28T03:01:00", "reviewer@example"),
            ("applicability_receipt", receipt, "2026-08-28T03:02:00", "reviewer@example"),
            ("evidence_sha256", "a" * 64, "2026-08-28T03:03:00", "reviewer@example"),
            ("applicability_receipt", receipt, "2026-08-28T03:04:00", "reviewer@example"),
        ],
        meta={"link_head_revision": head, "producer_identity": "producer@example"},
        labels=["source-only"],
    )
    with pytest.raises(ValueError, match="required checks"):
        validate_review_completion("239b24bf", "[REVIEW] source-only", home)


def test_source_only_receipt_rejects_pr_binding_in_card_metadata(tmp_path):
    head = "2" * 40
    receipt = json.dumps(
        {
            "type": "source-only-applicability",
            "card_id": "239b24bf",
            "source_head": head,
            "reviewer": "reviewer@example",
            "evidence_digest": "a" * 64,
            "governed_pr_ci": False,
        },
        sort_keys=True,
    )
    home = _home(
        tmp_path,
        "239b24bf",
        "[REVIEW] source-only",
        [
            ("verdict", "PASS", "2026-08-28T03:00:00", "reviewer@example"),
            ("evidence", "review.md", "2026-08-28T03:01:00", "reviewer@example"),
            ("evidence_sha256", "a" * 64, "2026-08-28T03:02:00", "reviewer@example"),
            ("applicability_receipt", receipt, "2026-08-28T03:03:00", "reviewer@example"),
        ],
        meta={
            "link_head_revision": head,
            "producer_identity": "producer@example",
            "pull_request": "https://example.invalid/p/1",
        },
        labels=["source-only"],
    )
    with pytest.raises(ValueError, match="required checks"):
        validate_review_completion("239b24bf", "[REVIEW] source-only", home)


@pytest.mark.parametrize("binding_key", ["open_pr", "pr_head", "pull_request"])
def test_source_only_receipt_rejects_pr_binding_in_core_links(tmp_path, binding_key):
    head = "2" * 40
    receipt = json.dumps(
        {
            "type": "source-only-applicability",
            "card_id": "239b24bf",
            "source_head": head,
            "reviewer": "reviewer@example",
            "evidence_digest": "a" * 64,
            "governed_pr_ci": False,
        },
        sort_keys=True,
    )
    home = _home(
        tmp_path,
        "239b24bf",
        "[REVIEW] source-only",
        [
            ("verdict", "PASS", "2026-08-28T03:00:00", "reviewer@example"),
            ("evidence", "review.md", "2026-08-28T03:01:00", "reviewer@example"),
            ("patch_sha256", "a" * 64, "2026-08-28T03:02:00", "reviewer@example"),
            ("applicability_receipt", receipt, "2026-08-28T03:03:00", "reviewer@example"),
        ],
        meta={"producer_identity": "producer@example"},
        labels=["source-only"],
        core_links={"link_head_revision": head, binding_key: "https://example.invalid/p/1"},
    )
    with pytest.raises(ValueError, match="required checks"):
        validate_review_completion("239b24bf", "[REVIEW] source-only", home)


def test_source_only_receipt_requires_authoritative_head(tmp_path):
    receipt = json.dumps(
        {
            "type": "source-only-applicability",
            "card_id": "239b24bf",
            "source_head": "2" * 40,
            "reviewer": "reviewer@example",
            "evidence_digest": "a" * 64,
            "governed_pr_ci": False,
        },
        sort_keys=True,
    )
    home = _home(
        tmp_path,
        "239b24bf",
        "[REVIEW] source-only",
        [
            ("verdict", "PASS", "2026-08-28T03:00:00", "reviewer@example"),
            ("evidence", "review.md", "2026-08-28T03:01:00", "reviewer@example"),
            ("patch_sha256", "a" * 64, "2026-08-28T03:02:00", "reviewer@example"),
            ("applicability_receipt", receipt, "2026-08-28T03:03:00", "reviewer@example"),
        ],
        meta={"producer_identity": "producer@example"},
        labels=["source-only"],
    )
    with pytest.raises(ValueError, match="required checks"):
        validate_review_completion("239b24bf", "[REVIEW] source-only", home)


@pytest.mark.parametrize(
    ("producer", "evidence_writer"),
    [
        ("Reviewer_Example", "reviewer-example"),
        ("producer@example", "producer@example"),
    ],
)
def test_source_only_receipt_rejects_same_principal_or_foreign_evidence(
    tmp_path, producer, evidence_writer
):
    head = "2" * 40
    receipt = json.dumps(
        {
            "type": "source-only-applicability",
            "card_id": "239b24bf",
            "source_head": head,
            "reviewer": "reviewer_example",
            "evidence_digest": "a" * 64,
            "governed_pr_ci": False,
        },
        sort_keys=True,
    )
    home = _home(
        tmp_path,
        "239b24bf",
        "[REVIEW] source-only",
        [
            ("verdict", "PASS", "2026-08-28T03:00:00", "reviewer-example"),
            ("evidence", "review.md", "2026-08-28T03:01:00", evidence_writer),
            ("patch_sha256", "a" * 64, "2026-08-28T03:02:00", evidence_writer),
            ("applicability_receipt", receipt, "2026-08-28T03:03:00", "reviewer-example"),
        ],
        meta={"link_head_revision": head, "producer_identity": producer},
        labels=["source-only"],
    )
    with pytest.raises(ValueError, match="required checks"):
        validate_review_completion("239b24bf", "[REVIEW] source-only", home)


def test_source_only_receipt_uses_governed_seat_independence(tmp_path):
    head = "2" * 40
    receipt = json.dumps(
        {
            "type": "source-only-applicability",
            "card_id": "239b24bf",
            "source_head": head,
            "reviewer": "pi-seraph-reviewer",
            "evidence_digest": "a" * 64,
            "governed_pr_ci": False,
        },
        sort_keys=True,
    )
    home = _home(
        tmp_path,
        "239b24bf",
        "[REVIEW] source-only",
        [
            ("verdict", "PASS", "2026-08-28T03:00:00", "pi-seraph-reviewer"),
            ("evidence", "review.md", "2026-08-28T03:01:00", "pi-seraph-reviewer"),
            ("patch_sha256", "a" * 64, "2026-08-28T03:02:00", "pi-seraph-reviewer"),
            (
                "applicability_receipt",
                receipt,
                "2026-08-28T03:03:00",
                "pi-seraph-reviewer",
            ),
        ],
        meta={"link_head_revision": head, "producer_identity": "pi-seraph-producer"},
        labels=["source-only"],
    )
    with pytest.raises(ValueError, match="required checks"):
        validate_review_completion("239b24bf", "[REVIEW] source-only", home)


@pytest.mark.parametrize(
    "malformation",
    [
        "wrong_action",
        "missing_timestamp",
        "equal_timestamp",
        "invalid_pr_timestamp",
        "conflicting_digest_timestamp",
    ],
)
def test_source_only_receipt_rejects_malformed_or_unordered_events(tmp_path, malformation):
    head = "2" * 40
    receipt = json.dumps(
        {
            "type": "source-only-applicability",
            "card_id": "239b24bf",
            "source_head": head,
            "reviewer": "reviewer@example",
            "evidence_digest": "a" * 64,
            "governed_pr_ci": False,
        },
        sort_keys=True,
    )
    home = _home(
        tmp_path,
        "239b24bf",
        "[REVIEW] source-only",
        [
            ("verdict", "PASS", "2026-08-28T03:00:00", "reviewer@example"),
            ("evidence", "review.md", "2026-08-28T03:01:00", "reviewer@example"),
            ("patch_sha256", "a" * 64, "2026-08-28T03:02:00", "reviewer@example"),
            (
                "applicability_receipt",
                receipt,
                (
                    "2026-08-28T03:02:00"
                    if malformation == "equal_timestamp"
                    else "2026-08-28T03:03:00"
                ),
                "reviewer@example",
            ),
        ],
        meta={"link_head_revision": head, "producer_identity": "producer@example"},
        labels=["source-only"],
    )
    event_path = home / "coordination" / "card_events" / "host.jsonl"
    rows = [json.loads(line) for line in event_path.read_text().splitlines()]
    if malformation == "wrong_action":
        rows[-1]["action"] = "verdict"
    elif malformation == "missing_timestamp":
        rows[-1].pop("ts")
    elif malformation == "invalid_pr_timestamp":
        rows.append(
            {
                "card_id": "239b24bf",
                "action": "link",
                "link_key": "open_pr",
                "link_value": "https://example.invalid/p/1",
                "ts": "not-a-time",
                "writer": "reviewer@example",
            }
        )
    elif malformation == "conflicting_digest_timestamp":
        rows.append(
            {
                "card_id": "239b24bf",
                "action": "link",
                "link_key": "patch_sha256",
                "link_value": "b" * 64,
                "ts": "2026-08-28T03:02:00",
                "writer": "reviewer@example",
            }
        )
    event_path.write_text("\n".join(json.dumps(row) for row in rows))
    with pytest.raises(ValueError, match="required checks"):
        validate_review_completion("239b24bf", "[REVIEW] source-only", home)


@pytest.mark.parametrize(
    "title",
    ["[SKGW-STRAT-06A][HUMAN] Approve cutover", "[FLEET-MON-01][P1] Restart monitors"],
)
def test_non_review_cards_are_left_alone(tmp_path, title):
    home = _home(tmp_path, "aaaaaaaa", title)
    assert not is_review_card(title)
    validate_review_completion("aaaaaaaa", title, home)


def test_completing_a_silent_review_is_refused(tmp_path):
    """The exact shape of a93fd881: claim, claim, complete, zero evidence rows."""
    home = _home(tmp_path, "a93fd881", "[REVIEW-bc69afd9][S][REVIEW] Independently review")
    with pytest.raises(ValueError) as err:
        validate_review_completion("a93fd881", "[REVIEW][S][REVIEW] x", home)
    assert "recorded no verdict" in str(err.value)


def test_terminal_pass_with_complete_ci_satisfies_it(tmp_path):
    home = _home(
        tmp_path,
        "bbbbbbbb",
        "[X][REVIEW] review",
        [("verdict", "PASS", "2026-08-28T03:00:00"), *_SUCCESSFUL_CI],
    )
    validate_review_completion("bbbbbbbb", "[X][REVIEW] review", home)


def test_node_review_accepts_complete_hosted_checks_at_exact_head(tmp_path):
    head = "7" * 40
    home = _home(
        tmp_path,
        "bbbbbbbb",
        "[X][REVIEW] review",
        [
            ("verdict", "PASS", "2026-08-28T03:00:00"),
            ("hosted_checks", f"6/6 SUCCESS at exact head {head}", "2026-08-28T03:01:00"),
        ],
        meta={
            "repository": "https://github.com/smilinTux/skgateway",
            "link_head_revision": head,
        },
    )
    validate_review_completion("bbbbbbbb", "[X][REVIEW] review", home)


@pytest.mark.parametrize(
    "hosted_checks",
    [
        None,
        "5/6 SUCCESS at exact head {head}",
        "6/6 PENDING at exact head {head}",
        "6/6 SUCCESS at exact head " + "8" * 40,
    ],
)
def test_node_review_fails_closed_without_complete_exact_head_checks(tmp_path, hosted_checks):
    head = "7" * 40
    links = [("verdict", "PASS", "2026-08-28T03:00:00")]
    if hosted_checks is not None:
        links.append(
            (
                "hosted_checks",
                hosted_checks.format(head=head),
                "2026-08-28T03:01:00",
            )
        )
    home = _home(
        tmp_path,
        "bbbbbbbb",
        "[X][REVIEW] review",
        links,
        meta={
            "repository": "https://github.com/smilinTux/skgateway",
            "link_head_revision": head,
        },
    )
    with pytest.raises(ValueError, match="hosted checks"):
        validate_review_completion("bbbbbbbb", "[X][REVIEW] review", home)


@pytest.mark.parametrize("verdict", ["FAIL", "BLOCKED blocked_on=card referent=inc-01"])
def test_terminal_negative_verdict_satisfies_review_completion(tmp_path, verdict):
    home = _home(
        tmp_path,
        "bbbbbbbb",
        "[X][REREVIEW] review",
        [("verdict", verdict, "2026-08-28T03:00:00")],
    )
    validate_review_completion("bbbbbbbb", "[X][REREVIEW] review", home)


@pytest.mark.parametrize(
    "verdict",
    [
        "PASS_FOR_REVIEW",
        "PASS_FOR_REREVIEW",
        "PASS_BOGUS",
        "PASS extra",
        "PASSING",
        "UNKNOWN",
        "pass",
        "FAILURE",
        "BLOCKED",
        "BLOCKED blocked_on=card",
        "BLOCKED referent=inc-01",
    ],
)
def test_provisional_verdict_cannot_complete_review(tmp_path, verdict):
    home = _home(
        tmp_path,
        "bbbbbbbb",
        "[X][REVIEW] review",
        [("verdict", verdict, "2026-08-28T03:00:00")],
    )
    with pytest.raises(ValueError, match="nonterminal verdict"):
        validate_review_completion("bbbbbbbb", "[X][REVIEW] review", home)


def test_pending_required_check_blocks_terminal_pass(tmp_path):
    home = _home(
        tmp_path,
        "bbbbbbbb",
        "[X][REVIEW] review",
        [
            ("verdict", "PASS", "2026-08-28T03:00:00"),
            *_SUCCESSFUL_CI,
            ("ci_check_python312", "pending", "2026-08-28T03:02:00"),
        ],
    )
    with pytest.raises(ValueError, match="not successful"):
        validate_review_completion("bbbbbbbb", "[X][REVIEW] review", home)


@pytest.mark.parametrize(
    "state",
    [
        "pending",
        "queued",
        "in_progress",
        "waiting",
        "missing",
        "cancelled",
        "skipped",
        "failure",
        "timed_out",
        "",
        "unknown",
    ],
)
def test_every_non_success_required_check_state_blocks_completion(tmp_path, state):
    home = _home(
        tmp_path,
        "bbbbbbbb",
        "[X][REVIEW] review",
        [
            ("verdict", "PASS", "2026-08-28T03:00:00"),
            *_SUCCESSFUL_CI,
            ("ci_check_python312", state, "2026-08-28T03:02:00"),
        ],
    )
    with pytest.raises(ValueError, match="not successful"):
        validate_review_completion("bbbbbbbb", "[X][REVIEW] review", home)


@pytest.mark.parametrize("title", ["[X][REVIEW] review", "[X][REREVIEW] rereview"])
def test_exact_canonical_success_allows_completion(tmp_path, title):
    home = _home(
        tmp_path,
        "bbbbbbbb",
        title,
        [
            ("verdict", "PASS", "2026-08-28T03:00:00"),
            *_SUCCESSFUL_CI[:-1],
            ("ci_check_python312", "SUCCESS", "2026-08-28T03:02:00"),
        ],
    )
    validate_review_completion("bbbbbbbb", title, home)


@pytest.mark.parametrize("title", ["[X][REVIEW] review", "[X][REREVIEW] rereview"])
@pytest.mark.parametrize(
    "state",
    [
        "PASS",
        "PASSED",
        "SUCCESSFUL",
        "SUCCESS extra",
        "SUCCESSFUL_PREFIX",
        "success",
        "Success",
        " success ",
        "UNKNOWN",
        "",
        "IN_PROGRESS",
        "PENDING",
        "QUEUED",
        "WAITING",
        "FAILURE",
        "CANCELLED",
        "SKIPPED",
        "TIMED_OUT",
    ],
)
def test_noncanonical_required_check_states_fail_closed(tmp_path, title, state):
    home = _home(
        tmp_path,
        "bbbbbbbb",
        title,
        [
            ("verdict", "PASS", "2026-08-28T03:00:00"),
            *_SUCCESSFUL_CI[:-1],
            ("ci_check_python312", state, "2026-08-28T03:02:00"),
        ],
    )
    with pytest.raises(ValueError, match="ci_check_python312"):
        validate_review_completion("bbbbbbbb", title, home)


def test_later_green_check_allows_terminal_pass(tmp_path):
    home = _home(
        tmp_path,
        "bbbbbbbb",
        "[X][REVIEW] review",
        [
            ("verdict", "PASS", "2026-08-28T03:00:00"),
            *_SUCCESSFUL_CI[:-1],
            ("ci_check_python312", "pending", "2026-08-28T03:01:00"),
            ("ci_check_python312", "SUCCESS", "2026-08-28T03:02:00"),
        ],
    )
    validate_review_completion("bbbbbbbb", "[X][REVIEW] review", home)


@pytest.mark.parametrize("title", ["[X][REVIEW] review", "[X][REREVIEW] rereview"])
def test_pass_without_required_ci_links_fails_closed(tmp_path, title):
    home = _home(
        tmp_path,
        "bbbbbbbb",
        title,
        [("verdict", "PASS", "2026-08-28T03:00:00")],
    )
    with pytest.raises(ValueError, match="not successful") as err:
        validate_review_completion("bbbbbbbb", title, home)
    assert "ci_check_python311" in str(err.value)
    assert "ci_check_python312" in str(err.value)


def test_pass_with_partial_required_ci_links_fails_closed(tmp_path):
    home = _home(
        tmp_path,
        "bbbbbbbb",
        "[X][REREVIEW] rereview",
        [("verdict", "PASS", "2026-08-28T03:00:00"), *_SUCCESSFUL_CI[:-1]],
    )
    with pytest.raises(ValueError, match="ci_check_python312"):
        validate_review_completion("bbbbbbbb", "[X][REREVIEW] rereview", home)


def test_an_empty_verdict_value_does_not_count(tmp_path):
    home = _home(
        tmp_path,
        "cccccccc",
        "[X][REVIEW] review",
        [("verdict", "   ", "2026-08-28T03:00:00")],
    )
    with pytest.raises(ValueError):
        validate_review_completion("cccccccc", "[X][REVIEW] review", home)


def test_a_non_outcome_link_does_not_count(tmp_path):
    """A PR link is not a judgement."""
    home = _home(
        tmp_path,
        "dddddddd",
        "[X][REVIEW] review",
        [("pr", "https://github.com/x/y/pull/1", "2026-08-28T03:00:00")],
    )
    with pytest.raises(ValueError):
        validate_review_completion("dddddddd", "[X][REVIEW] review", home)


def test_latest_verdict_is_returned(tmp_path):
    home = _home(
        tmp_path,
        "eeeeeeee",
        "[X][REVIEW] review",
        [
            ("verdict", "BLOCKED", "2026-08-28T01:00:00"),
            ("verdict", "PASS", "2026-08-28T02:00:00"),
        ],
    )
    assert recorded_verdict("eeeeeeee", home) == "PASS"
