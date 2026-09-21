"""Runnable private-review cycle safety tests."""

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from skcapstone.seraph_review_capauth import PUBLISH_METHOD, PUBLISH_PATH, SERAPH_FINGERPRINT
from skcapstone.seraph_review_contracts import ReviewPublicationError
from skcapstone.seraph_review_cycle import read_credential, signed_caller
from tests.test_seraph_review_publisher import SERVICE, evidence


def test_signed_caller_covers_exact_publication_digest(monkeypatch):
    captured = []
    caller = signed_caller(
        evidence(),
        lambda value: captured.append(value) or "signature",
        now=datetime.fromtimestamp(1000, timezone.utc),
        nonce="nonce",
    )
    headers = caller.presentation["headers"]
    assert headers["X-SK-Fingerprint"] == SERAPH_FINGERPRINT
    assert headers["X-SK-Nonce"] == "nonce"
    assert captured[0].startswith(f"{PUBLISH_METHOD}\n{PUBLISH_PATH}\n".encode())
    assert SERVICE == "seraph-review-bot"


def test_credential_file_must_be_owner_only_and_exact(tmp_path):
    path = tmp_path / "credential"
    digest = hashlib.sha256(b"synthetic").hexdigest()
    path.write_text(
        "SKGIT_TOKEN=synthetic\n"
        f"SKGIT_TOKEN_SHA256={digest}\n"
        "SKGIT_TOKEN_ID=17\n"
        "SKGIT_TOKEN_NAME=sklegal-seraph-review-publisher\n"
        "SKGIT_TOKEN_SCOPES=read:organization,read:user,write:repository\n"
        "SKGIT_PROVISIONED_REPOSITORY=smilinTux/sklegal\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    credential = read_credential(path)
    assert credential.token == "synthetic"
    assert credential.token_id == 17
    os.chmod(path, 0o640)
    with pytest.raises(ReviewPublicationError, match="permissions"):
        read_credential(path)


@pytest.mark.parametrize("content", ["TOKEN=synthetic\n", "SKGIT_TOKEN=\n", "SKGIT_TOKEN=a b\n"])
def test_credential_file_rejects_ambiguous_content(tmp_path, content):
    path = tmp_path / "credential"
    path.write_text(content, encoding="utf-8")
    os.chmod(path, 0o600)
    with pytest.raises(ReviewPublicationError, match="invalid"):
        read_credential(path)


def test_service_can_write_runtime_coordination_and_card_receipts():
    root = Path(__file__).resolve().parents[1]
    expected = " ".join(
        (
            "ReadWritePaths=%h/.skcapstone/runtime",
            "%h/.skcapstone/coordination",
            "%h/.skcapstone/cards",
        )
    )
    for relative in (
        "systemd/skfleet-seraph-review-publisher.service",
        "src/skcapstone/data/systemd/skfleet-seraph-review-publisher.service",
    ):
        assert expected in (root / relative).read_text(encoding="utf-8")
