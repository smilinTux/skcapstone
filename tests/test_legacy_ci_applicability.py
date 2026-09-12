"""Governed completion receipts for review cards created before CI profiles."""

import copy
import json

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone.ci_applicability import validate_legacy_completion
from skcapstone.review_verdict import validate_review_completion

REPOSITORY = "https://github.com/smilinTux/skgateway.git"
HEAD = "7ece8a0eea9f57d32dd63c44e368efde11c85473"
EVIDENCE = "6865727312e623616f1ce1ca155c30e977d900c7755aad5cf377f53da777d2a9"
CHECKS = {
    "test (22)": {
        "state": "SUCCESS",
        "evidence": "https://github.com/smilinTux/skgateway/actions/runs/34670043279/job/103489568536",
    },
    "docs / docs-check": {
        "state": "SUCCESS",
        "evidence": "https://github.com/smilinTux/skgateway/actions/runs/34670043436/job/103489569181",
    },
    "gitleaks": {
        "state": "SUCCESS",
        "evidence": "https://github.com/smilinTux/skgateway/actions/runs/34670043262/job/103489568546",
    },
}


def legacy_fixture(tmp_path):
    """Create exact immutable metadata and one append-only hosted-check receipt."""
    card_id = "78c57267"
    title = "[SKRSI][S][REVIEW] Independently verify cleanup"
    meta = {
        "repository": REPOSITORY,
        "base_ref": "main",
        "base_revision": "c" * 40,
        "link_source_card": "a68129c9",
        "link_head_revision": HEAD,
        "producer_identity": "codex-a68129c9-r2",
        "candidate_evidence_sha256": EVIDENCE,
    }
    store = CardStore(tmp_path)
    store.create(CardCore(id="a68129c9", title="Synthetic source"))
    store.create(
        CardCore(
            id=card_id,
            title=title,
            meta=meta,
            initial_labels=["parent-a68129c9"],
        )
    )
    receipt = {
        "schema_version": 1,
        "repository": REPOSITORY,
        "candidate_revision": HEAD,
        "candidate_evidence_sha256": EVIDENCE,
        "checks": copy.deepcopy(CHECKS),
    }

    def append(value=receipt, ts="2026-09-12T03:22:30Z"):
        store.append_event(
            card_id,
            "link",
            "seraph",
            link_key="legacy_ci_applicability",
            link_value=json.dumps(value),
            ts=ts,
        )

    return card_id, title, meta, receipt, store, append


def record_verdict(home, card_id):
    """Write the legacy projection consumed by the existing verdict reader."""
    events = home / "coordination" / "card_events"
    events.mkdir(parents=True, exist_ok=True)
    (events / "seraph.jsonl").write_text(
        json.dumps(
            {
                "card_id": card_id,
                "action": "link",
                "link_key": "verdict",
                "link_value": "PASS",
                "ts": "2026-09-12T03:23:00Z",
            }
        )
        + "\n"
    )


def test_exact_skgateway_hosted_checks_apply(tmp_path):
    """The exact PR155 head, evidence, checks, and job URLs satisfy policy."""
    card_id, _, meta, _, _, append = legacy_fixture(tmp_path)
    append()
    assert validate_legacy_completion(card_id, tmp_path, {"meta": meta}) is True


def test_target_review_can_complete_without_fabricated_generic_checks(tmp_path):
    """A governed receipt replaces no absent check with invented SUCCESS."""
    card_id, title, _, _, store, append = legacy_fixture(tmp_path)
    append()
    record_verdict(tmp_path, card_id)
    validate_review_completion(card_id, title, tmp_path)


@pytest.mark.parametrize(
    "case",
    [
        "repository",
        "head",
        "digest",
        "missing_check",
        "extra_check",
        "failure",
        "wrong_repository_url",
        "run_without_job",
        "missing_receipt",
        "malformed",
        "extra_field",
        "bool_version",
        "newer_failure",
    ],
)
def test_legacy_receipt_fails_closed(tmp_path, case):
    """Every identity, result, evidence, and whole-receipt mismatch blocks."""
    card_id, _, meta, receipt, _, append = legacy_fixture(tmp_path)
    if case == "repository":
        receipt["repository"] = "https://github.com/smilinTux/other.git"
    if case == "head":
        receipt["candidate_revision"] = "d" * 40
    if case == "digest":
        receipt["candidate_evidence_sha256"] = "d" * 64
    if case == "missing_check":
        del receipt["checks"]["gitleaks"]
    if case == "extra_check":
        receipt["checks"]["GitGuardian Security Checks"] = copy.deepcopy(
            receipt["checks"]["gitleaks"]
        )
    if case in {"failure", "newer_failure"}:
        receipt["checks"]["test (22)"]["state"] = "FAILURE"
    if case == "wrong_repository_url":
        receipt["checks"]["gitleaks"][
            "evidence"
        ] = "https://github.com/smilinTux/other/actions/runs/1/job/2"
    if case == "run_without_job":
        receipt["checks"]["gitleaks"][
            "evidence"
        ] = "https://github.com/smilinTux/skgateway/actions/runs/34670043262"
    if case == "extra_field":
        receipt["extra"] = True
    if case == "bool_version":
        receipt["schema_version"] = True
    if case == "missing_receipt":
        with pytest.raises(ValueError):
            validate_legacy_completion(card_id, tmp_path, {"meta": meta})
        return
    if case == "malformed":
        append("{")
    elif case == "newer_failure":
        good = copy.deepcopy(receipt)
        good["checks"]["test (22)"]["state"] = "SUCCESS"
        append(good, "2026-09-12T03:22:30Z")
        append(receipt, "2026-09-12T03:23:30Z")
    else:
        append(receipt)
    with pytest.raises(ValueError):
        validate_legacy_completion(card_id, tmp_path, {"meta": meta})


@pytest.mark.parametrize(
    "meta",
    [
        {"repository": REPOSITORY},
        {"repository": REPOSITORY, "link_head_revision": HEAD},
        {
            "repository": REPOSITORY,
            "link_head_revision": "short",
            "candidate_evidence_sha256": EVIDENCE,
        },
        {
            "repository": REPOSITORY,
            "link_head_revision": HEAD,
            "candidate_evidence_sha256": "short",
        },
    ],
)
def test_known_repository_requires_complete_immutable_binding(tmp_path, meta):
    """Known legacy repositories cannot fall back when identity is incomplete."""
    with pytest.raises(ValueError):
        validate_legacy_completion("78c57267", tmp_path, {"meta": meta})


def test_unknown_repository_retains_six_check_fallback(tmp_path):
    """No new mutable receipt changes legacy behavior for other repositories."""
    core = {"meta": {"repository": "https://github.com/example/unknown.git"}}
    assert validate_legacy_completion("bbbbbbbb", tmp_path, core) is False


def test_immutable_profile_path_takes_precedence(tmp_path, monkeypatch):
    """A stored profile never falls through to repository legacy policy."""
    card_id, title, _, _, store, append = legacy_fixture(tmp_path)
    core_path = tmp_path / "cards" / card_id / "core.json"
    core = json.loads(core_path.read_text())
    core["meta"]["ci_profile"] = {"malformed": True}
    core_path.write_text(json.dumps(core))
    append()
    record_verdict(tmp_path, card_id)
    called = False

    def profile(*_args):
        nonlocal called
        called = True
        raise ValueError("profile path")

    monkeypatch.setattr("skcapstone.ci_applicability.validate_profile_completion", profile)
    with pytest.raises(ValueError, match="profile path"):
        validate_review_completion(card_id, title, tmp_path)
    assert called
