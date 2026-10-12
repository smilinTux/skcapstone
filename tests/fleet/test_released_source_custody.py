"""Released modern producer claims retain proof, never invented ownership."""

import json

import pytest

from skcapstone.fleet import production_acceptance as acceptance, review_dispatch
from skcapstone.fleet import production_review_custody as custody
from tests.fleet import remote_review_fixtures, test_remote_review_policy, test_source_bundle

execution = remote_review_fixtures.execution
remote_policy = test_remote_review_policy.remote_policy
source = test_source_bundle.source


@pytest.fixture
def released(execution, monkeypatch):
    e = execution
    s = e["source"]
    e["bound_before"] = review_dispatch._source(e["home"], s["store"].fold(e["card"]))
    s["store"].append_event(
        s["card"],
        "release_claim",
        "operator",
        released_owner=s["owner"],
        expected_claim_revision=s["claim"],
        abandon_reason="error",
    )
    path = e["paths"].root / "status/node-chiap02/dispatch" / (s["card"] + ".json")
    status = json.loads(path.read_text())
    status.update(
        state="blocked", claim_released=True, error="claim released by authority before evidence"
    )
    status.pop("source_artifact")
    path.write_text(json.dumps(status))
    e["producer_status"] = status
    e["producer_path"] = path
    e["queries"] = []

    def terminal(unit, invocation, *, host):
        e["queries"].append((unit, invocation, host))
        return dict(LoadState="not-found", ActiveState="inactive", MainPID="0", ControlPID="0")

    monkeypatch.setattr(custody, "unit_terminal", terminal)
    return e


def test_exact_released_modern_source_is_reviewable_without_reclaim(released):
    e = released
    s = e["source"]
    result = review_dispatch._source(e["home"], s["store"].fold(e["card"]))
    assert result == e["bound_before"]
    assert s["store"].fold(s["card"]).owner is None
    assert e["queries"] == [(e["producer_status"]["unit"], "b" * 32, "chiap02")]


def test_acceptance_reuses_same_released_terminal_proof(released):
    e = released
    s = e["source"]
    manifest = json.loads(
        (
            e["home"] / "evidence/work" / s["card"] / "source-bundles" / (s["head"] + ".json")
        ).read_text()
    )
    terminal = acceptance._producer_terminal(e["home"], s["store"].fold(s["card"]), manifest)
    assert terminal["family"] == "glm"
    assert terminal["invocation"] == "b" * 32
    assert e["queries"]


@pytest.mark.parametrize(
    "change",
    [
        "new-claim",
        "new-outcome",
        "criteria",
        "no-release",
        "manifest",
        "bundle",
        "evidence",
        "failed-exit",
        "status-owner",
        "request",
        "unit",
        "active-host",
    ],
)
def test_released_source_failures_never_become_legacy_or_current_custody(
    released, monkeypatch, change
):
    e = released
    s = e["source"]
    st = s["store"]
    parent = s["card"]
    if change == "new-claim":
        st.append_event(parent, "claim", "new-builder", owner="new-builder")
        row = st.fold(parent)
        st.append_event(
            parent,
            "release_claim",
            "operator",
            released_owner=row.owner,
            expected_claim_revision=row.meta["_claim_revision"],
            abandon_reason="error",
        )
    elif change == "new-outcome":
        st.append_event(parent, "link", "operator", link_key="verdict", link_value="BLOCKED")
    elif change == "criteria":
        st.append_event(parent, "describe", "operator", description="changed product contract")
    elif change == "no-release":
        original = type(st)._read_events
        monkeypatch.setattr(
            type(st),
            "_read_events",
            lambda self, cid: [
                v for v in original(self, cid) if cid != parent or v["action"] != "release_claim"
            ],
        )
    elif change in ("manifest", "bundle", "evidence"):
        root = e["home"] / "evidence/work" / parent / "source-bundles"
        manifest = json.loads((root / (s["head"] + ".json")).read_text())
        path = (
            root / (s["head"] + ".json")
            if change == "manifest"
            else (
                root / (manifest["bundle_sha256"] + ".bundle")
                if change == "bundle"
                else root / (manifest["evidence_sha256"] + ".md")
            )
        )
        path.write_text("{}")
    elif change == "failed-exit":
        e["producer_status"]["exit_code"] = 1
    elif change == "status-owner":
        e["producer_status"]["owner"] = "another-owner"
    elif change == "request":
        e["producer_status"]["request_id"] = "0" * 64
    elif change == "unit":
        e["producer_status"]["unit"] = "skfleet-builder-wrong.service"
    else:

        def refuse(*a, **kw):
            raise ValueError("exact managed worker death unproven")

        monkeypatch.setattr(custody, "unit_terminal", refuse)
    e["producer_path"].write_text(json.dumps(e["producer_status"]))
    with pytest.raises((ValueError, AttributeError)):
        review_dispatch._source(e["home"], st.fold(e["card"]))
