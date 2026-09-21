"""Bounded private-forge transport and exact-head observation regressions."""

import io
import json
from urllib.error import HTTPError, URLError

import pytest

from skcapstone.forgejo import (
    SKGIT_ORIGIN,
    SKGIT_REPOSITORY,
    ForgejoClient,
    ForgejoError,
    ForgejoReadOnlyConnector,
    MultiForgeReadOnlyConnector,
    _NoRedirect,
)


class Opener:
    """Capture requests without touching the network."""

    def __init__(self, response=b"{}", error=None):
        self.response, self.error, self.calls = response, error, []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        return io.BytesIO(self.response)


def client(monkeypatch, opener):
    """Inject one transport into the real client."""
    monkeypatch.setattr("skcapstone.forgejo.build_opener", lambda *args: opener)
    return ForgejoClient(SKGIT_ORIGIN, "synthetic-private-token")


def test_credentials_only_in_header_and_timeout_is_bounded(monkeypatch):
    opener = Opener(b'{"login":"seraph"}')
    assert client(monkeypatch, opener).request("GET", "/api/v1/user") == {"login": "seraph"}
    req, timeout = opener.calls[0]
    assert req.full_url == SKGIT_ORIGIN + "/api/v1/user"
    assert req.get_header("Authorization") == "token synthetic-private-token"
    assert 0 < timeout <= 30


@pytest.mark.parametrize(
    "origin",
    [
        "http://skgit.skstack01.douno.it",
        SKGIT_ORIGIN + ".attacker.example",
        SKGIT_ORIGIN + "/api",
        "https://user:password@skgit.skstack01.douno.it",
    ],
)
def test_wrong_origin_is_rejected_before_network(origin):
    with pytest.raises(ForgejoError):
        ForgejoClient(origin, "synthetic")


@pytest.mark.parametrize(
    "path",
    [
        "https://elsewhere/api/v1/user",
        "//elsewhere/api/v1/user",
        "/api/v1/repos/other/repo/pulls",
        "/api/v1/users/another-user/tokens",
        "/api/v1/user?token=synthetic",
        "/api/v1/../admin/users",
        "/api/v1/repos/smilinTux/sklegal/pulls#fragment",
    ],
)
def test_unapproved_path_is_rejected_before_network(monkeypatch, path):
    opener = Opener()
    with pytest.raises(ForgejoError):
        client(monkeypatch, opener).request("GET", path)
    assert not opener.calls


def test_redirect_is_never_followed():
    assert _NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere") is None


@pytest.mark.parametrize(
    "error",
    [
        HTTPError(
            "https://example?token=synthetic-private-token",
            403,
            "synthetic-private-token",
            {},
            None,
        ),
        URLError("synthetic-private-token"),
        TimeoutError("synthetic-private-token"),
    ],
)
def test_transport_failures_do_not_expose_credentials(monkeypatch, error):
    with pytest.raises(ForgejoError) as caught:
        client(monkeypatch, Opener(error=error)).request("GET", "/api/v1/user")
    assert "synthetic-private-token" not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("body", [b"not JSON synthetic-private-token", b"[]" * 10000000])
def test_malformed_or_oversized_body_fails_closed(monkeypatch, body):
    with pytest.raises(ForgejoError):
        client(monkeypatch, Opener(body)).request("GET", "/api/v1/user")


def test_write_is_only_exact_review_endpoint(monkeypatch):
    opener = Opener()
    c = client(monkeypatch, opener)
    with pytest.raises(ForgejoError):
        c.request("DELETE", "/api/v1/repos/smilinTux/sklegal/branch_protections/main")
    with pytest.raises(ForgejoError):
        c.request("POST", "/api/v1/admin/users", {})
    assert not opener.calls
    c.request(
        "POST",
        "/api/v1/repos/smilinTux/sklegal/pulls/2/reviews",
        {"commit_id": "a" * 40, "event": "APPROVED", "body": "Synthetic"},
    )
    assert len(opener.calls) == 1
    assert json.loads(opener.calls[0][0].data)["commit_id"] == "a" * 40


@pytest.mark.parametrize(
    "change", [{"event": "REQUEST_CHANGES"}, {"commit_id": "main"}, {"body": ""}, {"comments": []}]
)
def test_write_requires_exact_approval_payload(monkeypatch, change):
    opener = Opener()
    payload = {"commit_id": "a" * 40, "event": "APPROVED", "body": "Synthetic"}
    payload.update(change)
    with pytest.raises(ForgejoError, match="review_request_invalid"):
        client(monkeypatch, opener).request(
            "POST", "/api/v1/repos/smilinTux/sklegal/pulls/2/reviews", payload
        )
    assert not opener.calls


class Pages:
    """Serve Forgejo pages, including server page-size clamping."""

    def __init__(self, *, changed=False, missing_checks=False):
        self.calls = []
        self.changed, self.missing_checks = changed, missing_checks

    def request(self, method, path, payload=None):
        self.calls.append(path)
        if "/pulls?" in path:
            if "page=1&" in path:
                return [self.pr(2)]
            if "page=2&" in path:
                return [self.pr(3)]
            return []
        if "/statuses?" in path:
            if self.missing_checks or "page=2&" in path:
                return []
            return [
                dict(id=2, context="required", status="failure"),
                dict(id=1, context="required", status="success"),
            ]
        number = int(path.rsplit("/", 1)[-1])
        value = self.pr(number)
        if self.changed:
            value["head"]["sha"] = "b" * 40
        return value

    @staticmethod
    def pr(number):
        """Return a fixture with real Forgejo field names."""
        return dict(
            number=number,
            title="Synthetic",
            body="",
            user={"login": "jarvis"},
            head={"sha": "a" * 40, "ref": "candidate"},
            base={"sha": "c" * 40, "ref": "main"},
            mergeable=True,
        )


def test_all_pages_read_and_latest_status_wins():
    transport = Pages()
    rows = ForgejoReadOnlyConnector(transport).list_open(SKGIT_REPOSITORY)
    assert [r["number"] for r in rows] == [2, 3]
    assert all(r["checks"] == [{"context": "required", "status": "failure"}] for r in rows)
    assert all(r["snapshot_at"] for r in rows)


def test_missing_checks_remain_unknown():
    rows = ForgejoReadOnlyConnector(Pages(missing_checks=True)).list_open(SKGIT_REPOSITORY)
    assert all(r["checks"] == [] for r in rows)


def test_changed_head_during_observation_fails_closed():
    with pytest.raises(ForgejoError, match="head_changed"):
        ForgejoReadOnlyConnector(Pages(changed=True)).list_open(SKGIT_REPOSITORY)


def test_boolean_pr_identity_during_recheck_fails_closed():
    class BooleanIdentity(Pages):
        """Return a malformed detail ID that Python equates with PR one."""

        def request(self, method, path, payload=None):
            if "/pulls?" in path:
                return [self.pr(1)] if "page=1&" in path else []
            if path.endswith("/pulls/1"):
                return self.pr(True)
            return super().request(method, path, payload)

    with pytest.raises(ForgejoError, match="identity_changed"):
        ForgejoReadOnlyConnector(BooleanIdentity()).list_open(SKGIT_REPOSITORY)


def test_repeated_pagination_fails_instead_of_hanging(monkeypatch):
    c = client(monkeypatch, Opener(b'[{"id":1}]'))
    with pytest.raises(ForgejoError, match="pagination"):
        c.pages("/api/v1/repos/smilinTux/sklegal/pulls")


def test_private_token_is_not_required_for_existing_github(monkeypatch):
    monkeypatch.delenv("SKFLEET_SKGIT_READ_TOKEN", raising=False)
    monkeypatch.setattr(
        "skcapstone.link_observation_producer.GhReadOnlyConnector.list_open",
        lambda self, repo: [{"repository": repo}],
    )
    assert MultiForgeReadOnlyConnector().list_open("smilinTux/skcapstone") == [
        {"repository": "smilinTux/skcapstone"}
    ]


def test_missing_private_token_returns_safe_producer_failure(monkeypatch):
    from skcapstone.link_observation_producer import ProducerError

    monkeypatch.delenv("SKFLEET_SKGIT_READ_TOKEN", raising=False)
    with pytest.raises(ProducerError, match="credential_unavailable"):
        MultiForgeReadOnlyConnector().list_open(SKGIT_REPOSITORY)
