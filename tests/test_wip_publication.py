"""Draft-only publication preserves the existing mediated forge boundary."""

import io
import json
from dataclasses import replace

import pytest

from skcapstone.forgejo import SKGIT_ORIGIN, ForgejoClient, ForgejoError, ForgejoWipClient
from skcapstone.wip_publication import WipCandidate, observe_ci, publish_wip

ROOT = "/api/v1/repos/smilinTux/sklegal"
CANDIDATE = WipCandidate(
    source_card="1234abcd",
    review_card="abcd1234",
    head="a" * 40,
    base="b" * 40,
    producer_family="deepseek",
    reviewer_family="zai",
    review_evidence_sha256="c" * 64,
    title="Output schema pin validation",
)


class Opener:
    def __init__(self):
        self.calls = []

    def open(self, request, timeout):
        self.calls.append(request)
        return io.BytesIO(b"{}")


def transport(monkeypatch, kind=ForgejoWipClient):
    opener = Opener()
    monkeypatch.setattr("skcapstone.forgejo.build_opener", lambda *args: opener)
    return kind(SKGIT_ORIGIN, "synthetic"), opener


def test_default_client_still_cannot_create_pr(monkeypatch):
    client, opener = transport(monkeypatch, ForgejoClient)
    with pytest.raises(ForgejoError):
        client.request("POST", ROOT + "/pulls", CANDIDATE.payload())
    assert not opener.calls


def test_default_read_scope_unchanged_but_wip_reconciles_closed_prs(monkeypatch):
    client, opener = transport(monkeypatch, ForgejoClient)
    with pytest.raises(ForgejoError):
        client.request("GET", ROOT + "/pulls?state=all")
    assert not opener.calls
    client, opener = transport(monkeypatch)
    client.request("GET", ROOT + "/pulls?state=all&page=1&limit=50")
    assert len(opener.calls) == 1


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", ROOT + "/pulls/1/merge"),
        ("POST", ROOT + "/pulls/1/reviews"),
        ("PATCH", ROOT + "/pulls/1"),
        ("DELETE", ROOT + "/branches/main"),
        ("POST", ROOT + "/pulls?state=open"),
    ],
)
def test_draft_client_cannot_actuate_other_endpoints(monkeypatch, method, path):
    client, opener = transport(monkeypatch)
    with pytest.raises(ForgejoError):
        client.request(method, path, CANDIDATE.payload())
    assert not opener.calls


@pytest.mark.parametrize(
    "change",
    [
        {"title": "Ready"},
        {"title": " WIP: misleading"},
        {"title": "WIP: "},
        {"base": "release"},
        {"head": "main"},
        {"merge": True},
        {"body": ""},
        {"title": "WIP: bad\nnew line"},
    ],
)
def test_draft_payload_is_narrow(monkeypatch, change):
    client, opener = transport(monkeypatch)
    payload = CANDIDATE.payload() | change
    with pytest.raises(ForgejoError):
        client.request("POST", ROOT + "/pulls", payload)
    assert not opener.calls


def test_valid_payload_uses_existing_bounded_transport(monkeypatch):
    client, opener = transport(monkeypatch)
    client.request("POST", ROOT + "/pulls", CANDIDATE.payload())
    assert json.loads(opener.calls[0].data)["title"].startswith("WIP: ")
    assert opener.calls[0].get_header("Authorization") == "token synthetic"


class Forge:
    def __init__(self):
        self.posts = 0
        self.open = []
        self.base, self.head = CANDIDATE.base, CANDIDATE.head
        self.statuses = []
        self.readback_change = {}
        self.lose_response = False

    def pr(self):
        return {
            "number": 42,
            "state": "open",
            "merged": False,
            "title": CANDIDATE.payload()["title"],
            "body": CANDIDATE.payload()["body"],
            "head": {
                "ref": CANDIDATE.branch,
                "sha": self.head,
                "repo": {"full_name": "smilinTux/sklegal"},
            },
            "base": {"ref": "main", "sha": self.base},
        }

    def pages(self, path):
        assert "/statuses" in path or path.endswith("/pulls?state=all")
        return self.statuses if "/statuses" in path else self.open

    def request(self, method, path, payload=None):
        if method == "POST":
            self.posts += 1
            self.open = [self.pr()]
            if self.lose_response:
                raise ForgejoError("forge_transport_unavailable")
            return self.pr()
        if "/branches/" in path:
            return {"commit": {"id": self.base if path.endswith("/main") else self.head}}
        return self.pr() | self.readback_change


def test_twice_is_one_exact_wip_pr():
    forge = Forge()
    assert publish_wip(forge, CANDIDATE)["number"] == 42
    assert publish_wip(forge, CANDIDATE)["number"] == 42
    assert forge.posts == 1


def test_lost_post_response_reconciles_without_duplicate():
    forge = Forge()
    forge.lose_response = True
    with pytest.raises(ForgejoError):
        publish_wip(forge, CANDIDATE)
    assert publish_wip(forge, CANDIDATE)["number"] == 42
    assert forge.posts == 1


@pytest.mark.parametrize("field", ["head", "base"])
def test_branch_drift_prevents_publication(field):
    forge = Forge()
    setattr(forge, field, "d" * 40)
    with pytest.raises(ForgejoError):
        publish_wip(forge, CANDIDATE)
    assert forge.posts == 0


@pytest.mark.parametrize(
    "change",
    [
        {"title": "Ready"},
        {"merged": True},
        {"state": "closed"},
        {"body": "wrong lineage"},
        {"base": {"ref": "main", "sha": "d" * 40}},
        {"head": {"ref": CANDIDATE.branch, "sha": "d" * 40}},
        {"head": {"ref": CANDIDATE.branch, "sha": CANDIDATE.head, "repo": None}},
    ],
)
def test_conflicting_existing_pr_is_never_modified(change):
    forge = Forge()
    forge.open = [forge.pr() | change]
    with pytest.raises(ForgejoError):
        publish_wip(forge, CANDIDATE)
    assert forge.posts == 0


def test_duplicate_prs_are_ambiguous():
    forge = Forge()
    forge.open = [forge.pr(), forge.pr() | {"number": 43}]
    with pytest.raises(ForgejoError):
        publish_wip(forge, CANDIDATE)
    assert forge.posts == 0


def test_live_readback_is_required():
    forge = Forge()
    forge.readback_change = {"title": "Ready"}
    with pytest.raises(ForgejoError):
        publish_wip(forge, CANDIDATE)


@pytest.mark.parametrize(
    "change",
    [
        {"source_card": "../bad"},
        {"review_card": "1234abcd"},
        {"head": "main"},
        {"reviewer_family": "deepseek"},
        {"producer_family": "unknown"},
        {"review_evidence_sha256": "missing"},
        {"title": "title\nnewline"},
    ],
)
def test_candidate_validation(change):
    with pytest.raises(ForgejoError):
        replace(CANDIDATE, **change).payload()


@pytest.mark.parametrize(
    "statuses,state",
    [
        ([], "unknown"),
        ([{"id": 1, "context": "ci", "status": "pending"}], "pending"),
        ([{"id": 1, "context": "ci", "status": "failure"}], "failure"),
        ([{"id": 1, "context": "ci", "status": "success"}], "success"),
        (
            [
                {"id": 2, "context": "ci", "status": "failure"},
                {"id": 1, "context": "ci", "status": "success"},
            ],
            "failure",
        ),
    ],
)
def test_ci_is_exact_head_and_never_invented(statuses, state):
    forge = Forge()
    forge.statuses = statuses
    result = observe_ci(forge, CANDIDATE)
    assert result["head"] == CANDIDATE.head
    assert result["state"] == state
    assert forge.posts == 0


def test_ci_conflicting_duplicate_or_malformed_status_fails_closed():
    forge = Forge()
    for statuses in [
        [
            {"id": 1, "context": "ci", "status": "success"},
            {"id": 1, "context": "ci", "status": "failure"},
        ],
        [{"id": 1, "context": "ci", "status": "unknown"}],
        [{"id": True, "context": "ci", "status": "success"}],
    ]:
        forge.statuses = statuses
        with pytest.raises(ForgejoError):
            observe_ci(forge, CANDIDATE)
