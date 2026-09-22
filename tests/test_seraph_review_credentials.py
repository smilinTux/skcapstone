"""Forgejo 15 split credential contracts through the actual HTTP clients."""

import copy
import hashlib
import io
import json
import os
from urllib.parse import urlsplit

import pytest

from skcapstone.forgejo import SKGIT_ORIGIN, SKGIT_REPOSITORY, ForgejoClient
from skcapstone.seraph_review_contracts import ReviewPublicationError
from skcapstone.seraph_review_credentials import (
    IdentityReadClient,
    attest_credentials,
    read_credentials,
)


def document():
    """Represent the sealed, synthetic same-account provisioning response."""

    def token(value, identity, name, scopes, repositories):
        return dict(
            token=value,
            token_sha256=hashlib.sha256(value.encode()).hexdigest(),
            token_id=identity,
            token_name=name,
            scopes=scopes,
            repositories=repositories,
            account_id=8,
            account_login="seraph-review-bot",
        )

    return {
        "version": 2,
        "identity": token(
            "synthetic-reader",
            21,
            "sklegal-seraph-identity",
            ["read:organization", "read:user"],
            [],
        ),
        "writer": token(
            "synthetic-writer",
            22,
            "sklegal-seraph-review-publisher",
            ["write:repository"],
            ["smilinTux/sklegal"],
        ),
    }


def stored(tmp_path, data):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)
    return path


@pytest.mark.parametrize("live_id", [8, 9, True])
def test_split_credentials_attest_with_real_clients(tmp_path, monkeypatch, live_id):
    calls = []

    class Opener:
        def open(self, request, timeout):
            path = urlsplit(request.full_url).path
            header = request.get_header("Authorization")
            calls.append((path, header))
            if path == "/api/v1/user":
                assert header == "token synthetic-reader"
                value = {"id": live_id, "login": "seraph-review-bot", "is_admin": False}
            elif path == "/api/v1/user/teams":
                assert header == "token synthetic-reader"
                value = (
                    []
                    if "page=2" in request.full_url
                    else [
                        {
                            "name": "sklegal-seraph-reviewers",
                            "includes_all_repositories": False,
                            "can_create_org_repo": False,
                            "units_map": {
                                "repo.code": "read",
                                "repo.pulls": "write",
                                "repo.wiki": "none",
                            },
                        }
                    ]
                )
            else:
                assert path == "/api/v1/repos/smilinTux/sklegal"
                assert header == "token synthetic-writer"
                value = {"full_name": "smilinTux/sklegal", "private": True}
            return io.BytesIO(json.dumps(value).encode())

    monkeypatch.setattr("skcapstone.forgejo.build_opener", lambda *args: Opener())
    credentials = read_credentials(stored(tmp_path, document()))
    args = (
        IdentityReadClient(SKGIT_ORIGIN, credentials.identity.token),
        ForgejoClient(SKGIT_ORIGIN, credentials.writer.token),
        credentials,
        SKGIT_REPOSITORY,
    )
    if type(live_id) is int and live_id == 8:
        attest_credentials(*args).validate("seraph-review-bot")
    else:
        with pytest.raises(ReviewPublicationError, match="identity"):
            attest_credentials(*args)
    assert len(calls) == 4
    assert "synthetic-reader" not in repr(credentials)
    assert "synthetic-writer" not in repr(credentials)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d["writer"].update(account_id=9),
        lambda d: d["writer"].update(account_login="producer"),
        lambda d: d["writer"].update(scopes=["write:repository", "read:user"]),
        lambda d: d["writer"].update(repositories=[]),
        lambda d: d["writer"].update(repositories=["smilinTux/other"]),
        lambda d: d["identity"].update(scopes=["all"]),
        lambda d: d["identity"].update(token_sha256="0" * 64),
        lambda d: d["identity"].update(token_id=True),
        lambda d: d.update(identity=copy.deepcopy(d["writer"])),
        lambda d: d.update(version=1),
        lambda d: d.update(extra="unknown"),
    ],
)
def test_changed_or_broad_binding_is_denied(tmp_path, mutation):
    data = document()
    mutation(data)
    with pytest.raises(ReviewPublicationError):
        read_credentials(stored(tmp_path, data))


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/v1/repos/smilinTux/sklegal/pulls/2/reviews"),
        ("GET", "/api/v1/repos/smilinTux/sklegal"),
        ("GET", "/api/v1/users/another/tokens"),
    ],
)
def test_identity_credential_never_writes_or_reads_repository(monkeypatch, method, path):
    monkeypatch.setattr("skcapstone.forgejo.build_opener", lambda *args: None)
    with pytest.raises(ReviewPublicationError):
        IdentityReadClient(SKGIT_ORIGIN, "synthetic-reader").request(method, path)


def test_split_file_rejects_permissions_symlinks_and_duplicate_fields(tmp_path):
    path = stored(tmp_path, document())
    os.chmod(path, 0o640)
    with pytest.raises(ReviewPublicationError):
        read_credentials(path)
    os.chmod(path, 0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(ReviewPublicationError):
        read_credentials(link)
    path.write_text('{"version":2,"version":2}')
    with pytest.raises(ReviewPublicationError):
        read_credentials(path)
