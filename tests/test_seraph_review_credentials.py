"""Forgejo 15 split credential contracts through the actual HTTP clients."""

import copy
import hashlib
import io
import json
import os
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest

from skcapstone.forgejo import SKGIT_ORIGIN, SKGIT_REPOSITORY, ForgejoClient, ForgejoError
from skcapstone.seraph_review_contracts import ReviewPublicationError
from skcapstone.seraph_review_credentials import (
    IdentityReadClient,
    ProtectionReadClient,
    attest_credentials,
    read_credentials,
    read_protection_token,
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


def protection_file(tmp_path, token="synthetic-admin"):
    path = tmp_path / "chef-skgit.env"
    path.write_text(
        "# Existing operator credential\n"
        "GITHUB_USER=operator\n"
        f"GITHUB_TOKEN={token}\n"
        f"GH_TOKEN={token}\n"
        "GH_URL=https://skgit.skstack01.douno.it\n",
        encoding="utf-8",
    )
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


def test_protection_credential_reads_one_matching_token_without_shell_evaluation(tmp_path):
    path = protection_file(tmp_path)
    assert read_protection_token(path) == "synthetic-admin"


@pytest.mark.parametrize(
    "change",
    [
        lambda text: text + "GH_TOKEN=second\n",
        lambda text: text.replace("GH_TOKEN=synthetic-admin", "GH_TOKEN=other"),
        lambda text: text.replace("GH_URL=", "UNKNOWN="),
        lambda text: text.replace("GITHUB_USER=operator", "export GITHUB_USER=operator"),
        lambda text: text.replace("GITHUB_TOKEN=synthetic-admin", "GITHUB_TOKEN=$(id)"),
    ],
)
def test_protection_credential_rejects_malformed_or_ambiguous_content(tmp_path, change):
    path = protection_file(tmp_path)
    path.write_text(change(path.read_text(encoding="utf-8")), encoding="utf-8")
    with pytest.raises(ReviewPublicationError, match="credential_file_invalid"):
        read_protection_token(path)


def test_protection_credential_rejects_permissions_and_symlinks(tmp_path):
    path = protection_file(tmp_path)
    os.chmod(path, 0o640)
    with pytest.raises(ReviewPublicationError, match="permissions"):
        read_protection_token(path)
    os.chmod(path, 0o600)
    link = tmp_path / "credential-link"
    link.symlink_to(path)
    with pytest.raises(ReviewPublicationError, match="credential_file_invalid"):
        read_protection_token(link)


def test_writer_policy_403_isolated_from_confined_protection_reader(monkeypatch):
    protection = {
        "rule_name": "main",
        "enable_status_check": True,
        "status_check_contexts": ["SKLegal CI / check"],
        "required_approvals": 1,
        "apply_to_admins": True,
        "block_on_rejected_reviews": True,
        "block_on_outdated_branch": True,
        "dismiss_stale_approvals": True,
        "ignore_stale_approvals": False,
        "enable_push": False,
    }

    class Opener:
        def open(self, request, timeout):
            if request.get_header("Authorization") == "token synthetic-writer":
                raise HTTPError(request.full_url, 403, "forbidden", {}, None)
            assert request.get_header("Authorization") == "token synthetic-admin"
            return io.BytesIO(json.dumps(protection).encode())

    monkeypatch.setattr("skcapstone.forgejo.build_opener", lambda *args: Opener())
    path = "/api/v1/repos/smilinTux/sklegal/branch_protections/main"
    with pytest.raises(ForgejoError, match="forge_http_403"):
        ForgejoClient(SKGIT_ORIGIN, "synthetic-writer").request("GET", path)
    policy = ProtectionReadClient(SKGIT_ORIGIN, "synthetic-admin").read(SKGIT_REPOSITORY, "main")
    assert policy.required_checks == frozenset({"SKLegal CI / check"})


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("POST", "/api/v1/repos/smilinTux/sklegal/branch_protections/main", {}),
        ("GET", "/api/v1/repos/smilinTux/sklegal/branches/main", None),
        ("GET", "/api/v1/repos/smilinTux/sklegal/branch_protections/main?page=1", None),
        ("GET", "/api/v1/repos/smilinTux/sklegal/branch_protections/main", {}),
    ],
)
def test_protection_reader_rejects_every_other_request_before_transport(
    monkeypatch, method, path, payload
):
    monkeypatch.setattr("skcapstone.forgejo.build_opener", lambda *args: None)
    client = ProtectionReadClient(SKGIT_ORIGIN, "synthetic-admin")
    with pytest.raises(ReviewPublicationError, match="protection_request_not_authorized"):
        client.request(method, path, payload)
