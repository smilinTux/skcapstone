"""Forge-qualified source discovery and exact review head binding."""

import importlib.util
from pathlib import Path

import pytest

from skcapstone.forgejo import SKGIT_REPOSITORY

SCRIPT = Path(__file__).parents[1] / "scripts/fleet/link-lineage.py"
SPEC = importlib.util.spec_from_file_location("link_forge_lineage", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
HEAD = "a" * 40
GITHUB = "smilinTux/skcapstone"


def pr(repository):
    return {"repository": repository, "number": 7, "headRefOid": HEAD, "baseRefOid": "b" * 40}


def cards(repository, source="1234abcd", review="abcd1234"):
    url = (
        f"{repository}/pulls/7"
        if repository == SKGIT_REPOSITORY
        else f"https://github.com/{repository}/pull/7"
    )
    return [
        {"id": source, "title": "Implement PR #7", "links": {"repository": repository}},
        {
            "id": review,
            "title": "Review PR #7",
            "labels": ["review", f"parent-{source}"],
            "status": "done",
            "links": {"pr": url, "head": HEAD, "verdict": "PASS"},
            "meta": {"link_head_revision": HEAD},
        },
    ]


def test_same_number_on_two_forges_has_distinct_lineage(tmp_path):
    result = MODULE.reconcile(
        [pr(GITHUB), pr(SKGIT_REPOSITORY)],
        cards(GITHUB) + cards(SKGIT_REPOSITORY, "5678abcd", "abcd5678"),
        tmp_path,
    )
    github = result["records"][f"{GITHUB}#7"]
    private = result["records"][f"{SKGIT_REPOSITORY}#7"]
    assert github["source_card"] == "1234abcd"
    assert private["source_card"] == "5678abcd"
    assert github["mapping_evidence_sha256"] != private["mapping_evidence_sha256"]


@pytest.mark.parametrize("field", ["commit", "head_commit", "head", "meta"])
def test_existing_review_head_fields_are_supported(field):
    review = cards(SKGIT_REPOSITORY)[1]
    review["links"].pop("head")
    review["meta"] = {}
    if field == "meta":
        review["meta"]["link_head_revision"] = HEAD
    else:
        review["links"][field] = HEAD
    assert MODULE._review_is_bound_to_pr(review, SKGIT_REPOSITORY, 7, HEAD)


@pytest.mark.parametrize(
    "change", ["bare_number", "other_forge", "stale_head", "conflicting_pin", "missing_head"]
)
def test_terminal_review_does_not_infer_binding(change, tmp_path):
    source, review = cards(SKGIT_REPOSITORY)
    if change == "bare_number":
        review["links"]["pr"] = "7"
    elif change == "other_forge":
        review["links"]["pr"] = f"https://github.com/{GITHUB}/pull/7"
    elif change == "stale_head":
        review["links"]["head"] = review["meta"]["link_head_revision"] = "c" * 40
    elif change == "conflicting_pin":
        review["meta"]["link_head_revision"] = "c" * 40
    else:
        review["links"].pop("head")
        review["meta"] = {}
    result = MODULE.reconcile([pr(SKGIT_REPOSITORY)], [source, review], tmp_path)
    assert result["records"] == {}
    assert result["coverage"]["unresolved"] == 1


def test_bare_source_title_cannot_cross_forges(tmp_path):
    source, review = cards(GITHUB)
    source.pop("links")
    result = MODULE.reconcile([pr(GITHUB), pr(SKGIT_REPOSITORY)], [source, review], tmp_path)
    assert result["records"] == {}
    assert all(not item["candidate_source_cards"] for item in result["diagnostics"])


def test_explicit_body_source_reference_is_scoped_to_that_pr(tmp_path):
    source, review = cards(SKGIT_REPOSITORY)
    source.pop("links")
    private = {**pr(SKGIT_REPOSITORY), "body": "Implements card 1234abcd"}
    result = MODULE.reconcile([pr(GITHUB), private], [source, review], tmp_path)
    assert list(result["records"]) == [f"{SKGIT_REPOSITORY}#7"]


def test_body_reference_cannot_override_conflicting_repository(tmp_path):
    source, review = cards(SKGIT_REPOSITORY)
    source["links"]["repository"] = GITHUB
    result = MODULE.reconcile(
        [{**pr(SKGIT_REPOSITORY), "body": "Card 1234abcd"}], [source, review], tmp_path
    )
    assert result["records"] == {}


def test_live_card_git_suffix_and_metadata_scope_are_supported(tmp_path):
    source, review = cards(SKGIT_REPOSITORY)
    source.pop("links")
    source["meta"] = {"repository": SKGIT_REPOSITORY + ".git"}
    review["meta"]["repository"] = SKGIT_REPOSITORY + ".git"
    result = MODULE.reconcile([pr(SKGIT_REPOSITORY)], [source, review], tmp_path)
    assert list(result["records"]) == [f"{SKGIT_REPOSITORY}#7"]


def test_conflicting_review_repository_cannot_bind():
    review = cards(SKGIT_REPOSITORY)[1]
    review["meta"]["repository"] = GITHUB
    assert not MODULE._review_is_bound_to_pr(review, SKGIT_REPOSITORY, 7, HEAD)


def test_explicit_source_pr_overrides_legacy_title_and_body_reference(tmp_path):
    source, review = cards(SKGIT_REPOSITORY)
    source["links"]["pr"] = SKGIT_REPOSITORY + "/pulls/8"
    result = MODULE.reconcile(
        [{**pr(SKGIT_REPOSITORY), "body": "Card 1234abcd"}], [source, review], tmp_path
    )
    assert result["records"] == {}


def test_explicit_source_pr_does_not_require_title_number(tmp_path):
    source, review = cards(SKGIT_REPOSITORY)
    source["title"] = "Implement bounded feature"
    source["links"]["pr"] = SKGIT_REPOSITORY + "/pulls/7"
    result = MODULE.reconcile([pr(SKGIT_REPOSITORY)], [source, review], tmp_path)
    assert list(result["records"]) == [f"{SKGIT_REPOSITORY}#7"]


@pytest.mark.parametrize("field", ["commit", "head", "head_commit", "meta", "conflicting"])
def test_explicit_source_head_conflict_cannot_bind_via_title_or_body(field, tmp_path):
    source, review = cards(SKGIT_REPOSITORY)
    if field in {"meta", "conflicting"}:
        source["meta"] = {"link_head_revision": "c" * 40}
        if field == "conflicting":
            source["links"]["commit"] = HEAD
    else:
        source["links"][field] = "c" * 40
    result = MODULE.reconcile(
        [{**pr(SKGIT_REPOSITORY), "body": "Card 1234abcd"}], [source, review], tmp_path
    )
    assert result["records"] == {}
    assert result["coverage"]["unresolved"] == 1


def test_matching_source_head_is_accepted(tmp_path):
    source, review = cards(SKGIT_REPOSITORY)
    source["links"]["commit"] = HEAD
    source["meta"] = {"link_head_revision": HEAD}
    result = MODULE.reconcile([pr(SKGIT_REPOSITORY)], [source, review], tmp_path)
    assert list(result["records"]) == [f"{SKGIT_REPOSITORY}#7"]


def test_private_exclusion_requires_repository_key(tmp_path):
    # The bare-number legacy map can no longer exclude anything.
    with pytest.raises(ValueError, match="structured"):
        MODULE.reconcile([pr(SKGIT_REPOSITORY)], [], tmp_path, {"7": "legacy exclusion"})
    with pytest.raises(ValueError, match="structured"):
        MODULE.reconcile(
            [pr(SKGIT_REPOSITORY)], [], tmp_path, {f"{SKGIT_REPOSITORY}#7": "legacy reason"}
        )
    exclusion = {
        "schema": "skfleet.link-lineage-exclusion/v1",
        "records": [
            {
                "repository": SKGIT_REPOSITORY,
                "pr": 7,
                "head_sha": HEAD,
                "base_sha": "b" * 40,
                "owner": "link",
                "reason": "explicit exclusion",
                "classification": "unmanaged",
                "source_evidence_sha256": "c" * 64,
                "expires_at": "2099-01-01T00:00:00+00:00",
            }
        ],
    }
    result = MODULE.reconcile([pr(GITHUB), pr(SKGIT_REPOSITORY)], [], tmp_path, exclusion)
    assert result["coverage"]["excluded"] == 1
    assert result["coverage"]["unresolved"] == 1
    excluded = [d for d in result["diagnostics"] if d["classification"] == "excluded"]
    assert [d["repository"] for d in excluded] == [SKGIT_REPOSITORY]


def test_lineage_fetch_uses_same_forge_selector(monkeypatch):
    calls = []

    class Connector:
        def list_open(self, repository):
            calls.append(repository)
            return [{"number": 7, "head": {"sha": HEAD}, "base": {"sha": "b" * 40, "ref": "main"}}]

    monkeypatch.setattr(MODULE, "MultiForgeReadOnlyConnector", Connector)
    rows = MODULE.fetch_prs(SKGIT_REPOSITORY)
    assert calls == [SKGIT_REPOSITORY]
    assert rows[0]["repository"] == SKGIT_REPOSITORY
    assert rows[0]["headRefOid"] == HEAD
