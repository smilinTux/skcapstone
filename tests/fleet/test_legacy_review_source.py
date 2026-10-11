"""Legacy routing needs exact native history and real evidence, not a provider guess."""

import hashlib
from types import SimpleNamespace

import pytest

from skcapstone.fleet import legacy_review_source as legacy


@pytest.fixture
def binding(tmp_path, monkeypatch):
    source_id = "1234abcd"
    head = "a" * 40
    root = tmp_path / "evidence/work" / source_id
    root.mkdir(parents=True)
    receipt = root / "receipt.md"
    receipt.write_text("Historical candidate receipt\n")
    bundle = root / "candidate.bundle"
    bundle.write_bytes(b"Historical candidate bytes")

    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    repository = "https://forge.example/org/project.git"
    source = SimpleNamespace(
        id=source_id,
        owner=None,
        archived=False,
        status=SimpleNamespace(value="review"),
        labels=["source-only"],
        meta={"repository": repository, "base_revision": "b" * 40, "base_ref": "main"},
        links={
            "evidence": str(receipt) + "#sha256=" + sha(receipt),
            "pr": "https://forge.example/org/project/pulls/18",
        },
    )
    event = dict(
        ts="2026-09-23T16:13:39+00:00",
        action="verdict",
        verdict="PASS_FOR_REVIEW",
        writer="jarvis",
        candidate_commit=head,
        candidate_tree="c" * 40,
        candidate_path=str(bundle),
        candidate_sha256=sha(bundle),
    )
    events = [event]
    store = SimpleNamespace(
        fold=lambda cid: source if cid == source_id else None,
        _read_events=lambda cid: events,
        _legacy_events=lambda cid: [],
    )
    monkeypatch.setattr(legacy, "CardStore", lambda home: store)
    core = dict(
        id="5678abcd",
        labels=["review", "review-distinct-agent", "seat-seraph"],
        meta={
            **source.meta,
            "link_source_card": source_id,
            "link_head_revision": head,
            "producer_identity": "jarvis",
            "candidate_evidence_sha256": sha(receipt),
        },
        links={"pr": source.links["pr"]},
    )
    return tmp_path, core, source, events, receipt, bundle


def test_released_legacy_source_binds_exact_candidate_without_provider(binding):
    home, core, *_ = binding
    assert legacy.verified_legacy_review_source(home, core, "jarvis")


@pytest.mark.parametrize(
    "change",
    [
        "receipt_changed",
        "bundle_changed",
        "missing_receipt",
        "redirected_receipt",
        "superseded_head",
        "failed_outcome",
        "wrong_writer",
        "active_owner",
        "conflict",
        "hold",
        "archived",
        "done",
        "wrong_repository",
        "wrong_base",
        "wrong_digest",
        "wrong_tree",
        "source_only_review",
        "no_distinct_policy",
        "no_pr",
        "wrong_pr",
        "modern_manifest",
        "conflicting_binding",
        "missing_source",
        "wrong_producer",
        "new_claim",
        "later_link_verdict",
        "modern_fenced_verdict",
        "dangling_manifest_root",
    ],
)
def test_incomplete_or_conflicting_legacy_source_refuses(binding, change):
    home, core, source, events, receipt, bundle = binding
    if change == "receipt_changed":
        receipt.write_text("changed")
    elif change == "bundle_changed":
        bundle.write_text("changed")
    elif change == "missing_receipt":
        receipt.unlink()
    elif change == "redirected_receipt":
        other = receipt.with_name("other.md")
        receipt.rename(other)
        receipt.symlink_to(other)
    elif change == "superseded_head":
        events.append(
            {**events[-1], "candidate_commit": "d" * 40, "ts": "2026-09-24T00:00:00+00:00"}
        )
    elif change == "failed_outcome":
        events.append({**events[-1], "verdict": "FAIL", "ts": "2026-09-24T00:00:00+00:00"})
    elif change == "wrong_writer":
        events[-1]["writer"] = "someone-else"
    elif change == "active_owner":
        source.owner = "jarvis"
    elif change == "conflict":
        source.meta["claim_conflicts"] = ["claim"]
    elif change == "hold":
        source.labels.append("hold")
    elif change == "archived":
        source.archived = True
    elif change == "done":
        source.status.value = "done"
    elif change == "wrong_repository":
        source.meta["repository"] = "https://other.example/repo.git"
    elif change == "wrong_base":
        source.meta["base_revision"] = "f" * 40
    elif change == "wrong_digest":
        core["meta"]["candidate_evidence_sha256"] = "0" * 64
    elif change == "wrong_tree":
        core["meta"]["candidate_tree"] = "d" * 40
    elif change == "source_only_review":
        core["labels"].append("source-only")
    elif change == "no_distinct_policy":
        core["labels"].remove("review-distinct-agent")
    elif change == "no_pr":
        core["links"].pop("pr")
    elif change == "wrong_pr":
        core["links"]["pr"] += "0"
    elif change == "modern_manifest":
        manifest = home / "evidence/work/1234abcd/source-bundles" / ("a" * 40 + ".json")
        manifest.parent.mkdir()
        manifest.write_text("{}")
    elif change == "conflicting_binding":
        core["links"]["producer_identity"] = "other"
    elif change == "missing_source":
        core["meta"]["link_source_card"] = "deadbeef"
    elif change == "new_claim":
        events.append(dict(action="claim", ts="2026-09-24T00:00:00+00:00"))
    elif change == "later_link_verdict":
        events.append(
            dict(action="link", key="verdict", value="FAIL", ts="2026-09-24T00:00:00+00:00")
        )
    elif change == "modern_fenced_verdict":
        events[-1]["expected_claim_revision"] = "modern-claim"
    elif change == "dangling_manifest_root":
        (receipt.parent / "source-bundles").symlink_to(receipt.parent / "absent")
    elif change == "wrong_producer":
        core["meta"]["producer_identity"] = "other"
    assert not legacy.verified_legacy_review_source(home, core, "jarvis")
