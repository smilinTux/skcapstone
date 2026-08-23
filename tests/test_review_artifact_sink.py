"""Durable content-addressed review artifact sink tests."""

from __future__ import annotations

import hashlib
import json
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

from skcapstone.qualification.artifacts import ArtifactError, ingest_artifact_bundle


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bundle(tmp_path, *, sealed=True, disposition="accepted"):
    source = tmp_path / "scan"
    source.mkdir()
    report = source / "report.md"
    report.write_text("# Security review\n\nNo findings.\n", encoding="utf-8")
    manifest = {
        "schema": "skcapstone-review-artifact/v1",
        "sealed": sealed,
        "disposition": disposition,
        "producer": {"name": "codex-security", "version": "0.1.21"},
        "accepted_source_sha256": "a" * 64,
        "retention_policy": "release",
        "files": [{"path": "report.md", "sha256": _sha(report), "kind": "report"}],
    }
    (source / "scan-manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return source


def test_accepted_bundle_is_content_addressed_verified_and_read_only(tmp_path) -> None:
    source = _bundle(tmp_path)

    first = ingest_artifact_bundle(source, tmp_path / "durable")
    second = ingest_artifact_bundle(source, tmp_path / "durable")

    assert first.acceptance_state == "accepted"
    assert first.bundle_sha256 == second.bundle_sha256
    assert first.directory == second.directory
    assert first.receipt_path.exists()
    assert not (first.directory / "files" / "report.md").stat().st_mode & stat.S_IWUSR
    assert str(first.directory).startswith(str(tmp_path / "durable" / "sha256"))


def test_hash_mismatch_and_missing_file_fail_closed(tmp_path) -> None:
    source = _bundle(tmp_path)
    manifest_path = source / "scan-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][0]["sha256"] = "b" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ArtifactError, match="digest mismatch"):
        ingest_artifact_bundle(source, tmp_path / "durable")

    manifest["files"][0]["path"] = "missing.md"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ArtifactError, match="missing"):
        ingest_artifact_bundle(source, tmp_path / "durable")


@pytest.mark.parametrize(
    "value",
    [
        "-----BEGIN PGP PRIVATE KEY BLOCK-----",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "Authorization: Bearer synthetic-token",
        '{"authorization":"Bearer synthetic-json-bearer"}',
        "password = 'synthetic-secret'",
        "API_KEY=synthetic-secret-value",
    ],
)
def test_secret_and_raw_credential_material_is_rejected(tmp_path, value) -> None:
    source = _bundle(tmp_path)
    report = source / "report.md"
    report.write_text(value, encoding="utf-8")
    manifest = json.loads((source / "scan-manifest.json").read_text())
    manifest["files"][0]["sha256"] = _sha(report)
    (source / "scan-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ArtifactError, match="sensitive material"):
        ingest_artifact_bundle(source, tmp_path / "durable")


def test_nested_json_credential_material_is_rejected(tmp_path) -> None:
    source = _bundle(tmp_path)
    report = source / "report.json"
    old_report = source / "report.md"
    old_report.unlink()
    report.write_text(
        json.dumps({"result": {"authorization": "Bearer synthetic-nested-token"}}),
        encoding="utf-8",
    )
    manifest = json.loads((source / "scan-manifest.json").read_text())
    manifest["files"][0]["path"] = "report.json"
    manifest["files"][0]["sha256"] = _sha(report)
    (source / "scan-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ArtifactError, match="sensitive material"):
        ingest_artifact_bundle(source, tmp_path / "durable")


@pytest.mark.parametrize("secret_key", ["client_secret", "refresh_token"])
def test_nested_json_oauth_credential_material_is_rejected(tmp_path, secret_key) -> None:
    source = _bundle(tmp_path)
    report = source / "report.json"
    (source / "report.md").unlink()
    report.write_text(
        json.dumps({"result": {secret_key: "synthetic-oauth-secret"}}),
        encoding="utf-8",
    )
    manifest = json.loads((source / "scan-manifest.json").read_text())
    manifest["files"][0]["path"] = "report.json"
    manifest["files"][0]["sha256"] = _sha(report)
    (source / "scan-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ArtifactError, match="sensitive material"):
        ingest_artifact_bundle(source, tmp_path / "durable")


def test_unsealed_or_rejected_bundle_cannot_masquerade_as_acceptance(tmp_path) -> None:
    unsealed = ingest_artifact_bundle(_bundle(tmp_path, sealed=False), tmp_path / "durable")
    assert unsealed.acceptance_state == "unsealed"

    other = tmp_path / "other"
    other.mkdir()
    rejected = ingest_artifact_bundle(_bundle(other, disposition="rejected"), tmp_path / "durable")
    assert rejected.acceptance_state == "rejected"


def test_unsafe_paths_symlinks_and_duplicate_entries_are_rejected(tmp_path) -> None:
    source = _bundle(tmp_path)
    manifest_path = source / "scan-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"].append(dict(manifest["files"][0]))
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ArtifactError, match="duplicate"):
        ingest_artifact_bundle(source, tmp_path / "durable")

    manifest["files"] = [manifest["files"][0]]
    (source / "link.md").symlink_to(source / "report.md")
    manifest["files"][0] = {
        "path": "link.md",
        "sha256": _sha(source / "report.md"),
        "kind": "report",
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ArtifactError, match="symlink"):
        ingest_artifact_bundle(source, tmp_path / "durable")


def test_binary_or_protected_corpus_extensions_are_not_accepted(tmp_path) -> None:
    source = _bundle(tmp_path)
    payload = source / "client-record.pdf"
    payload.write_bytes(b"synthetic protected corpus")
    manifest = json.loads((source / "scan-manifest.json").read_text())
    manifest["files"] = [{"path": payload.name, "sha256": _sha(payload), "kind": "report"}]
    (source / "scan-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ArtifactError, match="artifact extension"):
        ingest_artifact_bundle(source, tmp_path / "durable")


def test_manifest_duplicate_members_and_sensitive_metadata_are_rejected(tmp_path) -> None:
    source = _bundle(tmp_path)
    manifest_path = source / "scan-manifest.json"
    rendered = manifest_path.read_text(encoding="utf-8")
    manifest_path.write_text(
        rendered.replace(
            '"schema": "skcapstone-review-artifact/v1",',
            '"schema": "wrong",\n  "schema": "skcapstone-review-artifact/v1",',
        ),
        encoding="utf-8",
    )
    with pytest.raises(ArtifactError, match="missing or invalid"):
        ingest_artifact_bundle(source, tmp_path / "durable")

    other = tmp_path / "other"
    other.mkdir()
    source = _bundle(other)
    manifest = json.loads((source / "scan-manifest.json").read_text())
    manifest["metadata"] = {"password": "synthetic-secret"}
    (source / "scan-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    with pytest.raises(ArtifactError, match="sensitive material"):
        ingest_artifact_bundle(source, tmp_path / "durable")


def test_nested_symlink_parent_is_rejected(tmp_path) -> None:
    source = _bundle(tmp_path)
    real = source / "real"
    real.mkdir()
    report = real / "nested.md"
    report.write_text("safe text\n", encoding="utf-8")
    (source / "alias").symlink_to(real, target_is_directory=True)
    manifest = json.loads((source / "scan-manifest.json").read_text())
    manifest["files"] = [{"path": "alias/nested.md", "sha256": _sha(report), "kind": "report"}]
    (source / "scan-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ArtifactError, match="symlink"):
        ingest_artifact_bundle(source, tmp_path / "durable")


def test_existing_bundle_receipt_tampering_is_detected(tmp_path) -> None:
    source = _bundle(tmp_path)
    receipt = ingest_artifact_bundle(source, tmp_path / "durable")
    receipt.directory.chmod(0o755)
    receipt.receipt_path.chmod(0o644)
    payload = json.loads(receipt.receipt_path.read_text(encoding="utf-8"))
    payload["acceptance_state"] = "rejected"
    receipt.receipt_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ArtifactError, match="receipt disagrees"):
        ingest_artifact_bundle(source, tmp_path / "durable")


def test_existing_bundle_receipt_projection_tampering_is_detected(tmp_path) -> None:
    source = _bundle(tmp_path)
    receipt = ingest_artifact_bundle(source, tmp_path / "durable")
    receipt.directory.chmod(0o755)
    projection = receipt.directory / "receipt.md"
    projection.chmod(0o644)
    projection.write_text("forged projection\n", encoding="utf-8")

    with pytest.raises(ArtifactError, match="projection disagrees"):
        ingest_artifact_bundle(source, tmp_path / "durable")


def test_sink_symlink_cannot_escape_and_stored_symlinks_are_not_followed(tmp_path) -> None:
    source = _bundle(tmp_path)
    sink = tmp_path / "durable"
    sink.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (sink / "sha256").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactError, match="symlink"):
        ingest_artifact_bundle(source, sink)
    assert not any(outside.iterdir())

    (sink / "sha256").unlink()
    receipt = ingest_artifact_bundle(source, sink)
    receipt.directory.chmod(0o755)
    external_receipt = tmp_path / "external-receipt.json"
    external_receipt.write_text(receipt.receipt_path.read_text(encoding="utf-8"), encoding="utf-8")
    receipt.receipt_path.unlink()
    receipt.receipt_path.symlink_to(external_receipt)

    with pytest.raises(ArtifactError, match="symlink"):
        ingest_artifact_bundle(source, sink)
    assert external_receipt.stat().st_mode & stat.S_IWUSR


def test_existing_bundle_rejects_unmanifested_extra_file(tmp_path) -> None:
    source = _bundle(tmp_path)
    receipt = ingest_artifact_bundle(source, tmp_path / "durable")
    receipt.directory.chmod(0o755)
    extra = receipt.directory / "unexpected-secret.txt"
    extra.write_text("not in the manifest\n", encoding="utf-8")

    with pytest.raises(ArtifactError, match="inventory disagrees"):
        ingest_artifact_bundle(source, tmp_path / "durable")


def test_existing_bundle_rejects_hardlinked_receipt(tmp_path) -> None:
    source = _bundle(tmp_path)
    receipt = ingest_artifact_bundle(source, tmp_path / "durable")
    receipt.directory.chmod(0o755)
    receipt.receipt_path.chmod(0o644)
    external = tmp_path / "external-receipt.json"
    receipt.receipt_path.rename(external)
    receipt.receipt_path.hardlink_to(external)

    with pytest.raises(ArtifactError, match="linked file"):
        ingest_artifact_bundle(source, tmp_path / "durable")
    assert external.stat().st_mode & stat.S_IWUSR


def test_concurrent_identical_ingest_is_idempotent(tmp_path) -> None:
    source = _bundle(tmp_path)
    sink = tmp_path / "durable"

    with ThreadPoolExecutor(max_workers=16) as pool:
        receipts = list(pool.map(lambda _index: ingest_artifact_bundle(source, sink), range(64)))

    assert len({receipt.bundle_sha256 for receipt in receipts}) == 1
    assert len({receipt.directory for receipt in receipts}) == 1


def test_noncanonical_manifest_paths_are_rejected(tmp_path) -> None:
    source = _bundle(tmp_path)
    manifest_path = source / "scan-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "nested/../report.md"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ArtifactError, match="relative"):
        ingest_artifact_bundle(source, tmp_path / "durable")
