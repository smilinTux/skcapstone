"""Audit-engine tests for the Syncthing private-material policy.

Synthetic fixtures only: throwaway config.xml files, throwaway folder
roots, and PGPy-generated synthetic keys. No real Syncthing config, agent
home, or key on this host is ever touched.

The negative controls come first: the audit exists to FAIL on a bad tree,
and a check nobody has seen fail is not known to work.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcapstone.sync_policy import (
    MATERIAL_CLASSES,
    Coverage,
    audit,
    audit_folder,
    discover_folders,
    evaluate,
    load_ruleset,
)
from skcapstone.sync_policy.discovery import DiscoveredFolder, parse_config_folders

pgpy = pytest.importorskip("pgpy", reason="fingerprint fixtures need pgpy")

from pgpy.constants import (  # noqa: E402
    CompressionAlgorithm,
    HashAlgorithm,
    KeyFlags,
    PubKeyAlgorithm,
    SymmetricKeyAlgorithm,
)

#: A folder root .stignore that covers every private-material probe.
COVERING_RULES = "\n".join(line for material in MATERIAL_CLASSES for _, line in material.probes)

CONFIG_TEMPLATE = (
    '<configuration><device id="d1" name="n1"/>'
    '<folder id="{fid}" label="{fid}" path="{path}" type="sendreceive"/>'
    "</configuration>"
)


def _write_config(path: Path, folders: list[tuple[str, str]]) -> Path:
    """Write one synthetic Syncthing config.xml."""
    body = "".join(
        CONFIG_TEMPLATE.format(fid=fid, path=folder_path) for fid, folder_path in folders
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"<configuration>{body}</configuration>", encoding="utf-8")
    return path


def _folder(root: Path, rules: str | None = None) -> Path:
    """Create a folder root, optionally with a covering .stignore."""
    root.mkdir(parents=True, exist_ok=True)
    if rules is not None:
        (root / ".stignore").write_text(rules, encoding="utf-8")
    return root


@pytest.fixture
def synthetic_key(tmp_path) -> str:
    """One synthetic PGP private key, ASCII-armored. Never a real key."""
    key = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 1024)
    uid = pgpy.PGPUID.new("Synthetic Audit", email="audit@example.invalid")
    key.add_uid(
        uid,
        usage={KeyFlags.Sign},
        hashes=[HashAlgorithm.SHA256],
        ciphers=[SymmetricKeyAlgorithm.AES256],
        compression=[CompressionAlgorithm.Uncompressed],
    )
    return str(key)


# ------------------------------------------------------------ discovery ---


def test_discovers_multiple_folders_and_resolves_symlinks(tmp_path) -> None:
    real = tmp_path / "real-root"
    _folder(real, COVERING_RULES)
    link = tmp_path / "link-root"
    link.symlink_to(real)
    config = _write_config(
        tmp_path / "config.xml",
        [("folder-a", str(real)), ("folder-b", str(link))],
    )
    folders, findings = discover_folders(tmp_path, config_path=config)
    assert findings == []
    # Both entries resolve to the same root and dedupe to one folder.
    assert folders == [DiscoveredFolder(folder_id="folder-a", path=real.resolve())]


def test_missing_explicit_config_fails_closed(tmp_path) -> None:
    folders, findings = discover_folders(tmp_path, config_path=tmp_path / "absent.xml")
    assert folders == []
    assert findings[0].severity == "error"
    assert findings[0].category == "config_not_found"


def test_no_config_anywhere_fails_closed(tmp_path) -> None:
    folders, findings = discover_folders(tmp_path)
    assert folders == []
    assert findings[0].category == "config_not_found"
    assert findings[0].severity == "error"


def test_malformed_config_fails_closed(tmp_path) -> None:
    bad = tmp_path / "config.xml"
    bad.write_text("<configuration><folder id='x'", encoding="utf-8")
    folders, findings = discover_folders(tmp_path, config_path=bad)
    assert folders == []
    assert findings[0].severity == "error"
    assert findings[0].category == "config_unreadable"


def test_parse_expands_tilde(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = _write_config(tmp_path / "config.xml", [("f", "~/.skcapstone")])
    folders, _ = parse_config_folders(config)
    assert folders[0].path == (tmp_path / ".skcapstone").resolve()


# --------------------------------------------------------- folder audit ---


def test_covered_private_key_passes(tmp_path) -> None:
    root = _folder(tmp_path / "f", COVERING_RULES)
    (root / "agent.key").write_text("synthetic", encoding="utf-8")
    report, keys = audit_folder(DiscoveredFolder("f", root))
    assert report.severity == "warn"  # present but covered is visible, not fatal
    assert not any(f.severity == "error" for f in report.findings)
    assert keys == [root / "agent.key"]


def test_uncovered_private_key_fails_closed(tmp_path) -> None:
    """THE negative control: a key with missing rules must scream."""
    root = _folder(tmp_path / "f", "*.pem\n")  # partial rules only
    (root / "agent.key").write_text("synthetic", encoding="utf-8")
    report, _ = audit_folder(DiscoveredFolder("f", root))
    assert report.severity == "error"
    uncovered = [f for f in report.findings if f.category == "private_material_uncovered"]
    assert [f.path for f in uncovered] == ["agent.key"]
    assert "*.key" in uncovered[0].remediation


def test_missing_stignore_fails_closed(tmp_path) -> None:
    root = _folder(tmp_path / "f")
    report, _ = audit_folder(DiscoveredFolder("f", root))
    assert report.severity == "error"
    assert any(f.category == "stignore_missing" for f in report.findings)
    # Every probe is uncovered, so remediation covers every material class.
    assert set(report.remediation_lines) == set(COVERING_RULES.splitlines())


def test_empty_folder_without_rules_still_fails_closed(tmp_path) -> None:
    """Could-land probes: nothing present yet, still not safe."""
    root = _folder(tmp_path / "f", "*.key\n*.pem\n**/private.*\n")
    report, _ = audit_folder(DiscoveredFolder("f", root))
    assert report.severity == "error"
    probes = {f.path for f in report.findings if f.category == "private_pattern_uncovered"}
    assert ".gnupg/secring.gpg" in probes
    assert "capauth/security/tokens/probe.token" in probes


def test_ignored_directories_are_pruned_from_the_walk(tmp_path) -> None:
    root = _folder(tmp_path / "f", COVERING_RULES + "\nsessions\n")
    ignored = root / "sessions"
    ignored.mkdir()
    (ignored / "stray.key").write_text("synthetic", encoding="utf-8")
    report, keys = audit_folder(DiscoveredFolder("f", root))
    # The stray key sits under an ignored directory: it cannot sync, so the
    # walk never even reports it.
    assert not any(f.path == "sessions/stray.key" for f in report.findings)
    assert keys == []


def test_absent_folder_root_reports_not_held(tmp_path) -> None:
    report, keys = audit_folder(DiscoveredFolder("f", tmp_path / "absent"))
    assert report.severity == "info"
    assert keys == []


def test_template_ruleset_is_clean_on_a_real_tree(tmp_path) -> None:
    """The bundled template at a folder root covers every probe."""
    template = (
        Path(__import__("skcapstone").__file__).parent / "defaults" / ".stignore"
    ).read_text(encoding="utf-8")
    root = _folder(tmp_path / "f", template)
    identity = root / "agents" / "lumina" / "capauth" / "identity"
    identity.mkdir(parents=True)
    (identity / "private.asc").write_text("synthetic", encoding="utf-8")
    report, keys = audit_folder(DiscoveredFolder("f", root))
    assert not any(f.severity == "error" for f in report.findings)
    assert keys == [identity / "private.asc"]


# --------------------------------------------------- legacy root + audit ---


def _audit_home(tmp_path, rules: str | None = COVERING_RULES) -> tuple[Path, Path]:
    """A home with one configured folder and default agent/legacy homes."""
    home = tmp_path / "home"
    folder = _folder(home / "sync", rules)
    _write_config(
        home / ".config" / "syncthing" / "config.xml", [("skcapstone-sync", str(folder))]
    )
    return home, folder


def test_clean_tree_verdict_ok(tmp_path) -> None:
    home, _ = _audit_home(tmp_path)
    report = audit(home=home)
    assert report.ok
    assert report.severity == "ok"


def test_legacy_capauth_root_warns_outside_sync(tmp_path) -> None:
    home, _ = _audit_home(tmp_path)
    legacy = home / ".capauth" / "identity"
    legacy.mkdir(parents=True)
    (legacy / "private.asc").write_text("synthetic", encoding="utf-8")
    report = audit(home=home)
    legacy_findings = [f for f in report.findings if f.category == "legacy_capauth_root"]
    assert len(legacy_findings) == 1
    assert legacy_findings[0].severity == "warn"
    assert report.ok


def test_legacy_capauth_root_inside_synced_folder_is_error(tmp_path) -> None:
    home, _ = _audit_home(tmp_path)
    legacy = home / "sync" / ".capauth" / "identity"
    legacy.mkdir(parents=True)
    (legacy / "private.asc").write_text("synthetic", encoding="utf-8")
    report = audit(home=home, legacy_home=home / "sync" / ".capauth")
    legacy_findings = [f for f in report.findings if f.category == "legacy_capauth_root"]
    assert legacy_findings[0].severity == "error"
    assert not report.ok


def test_duplicate_fingerprints_are_an_error(tmp_path, synthetic_key) -> None:
    home, folder = _audit_home(tmp_path)
    identity = folder / "agents" / "lumina" / "capauth" / "identity"
    identity.mkdir(parents=True)
    (identity / "private.asc").write_text(synthetic_key, encoding="utf-8")
    agent_identity = home / ".skcapstone" / "agents" / "opus" / "capauth" / "identity"
    agent_identity.mkdir(parents=True)
    (agent_identity / "private.asc").write_text(synthetic_key, encoding="utf-8")
    report = audit(home=home)
    duplicates = [f for f in report.findings if f.category == "duplicate_private_fingerprint"]
    assert len(duplicates) == 1
    assert duplicates[0].severity == "error"
    assert "lumina" in duplicates[0].detail and "opus" in duplicates[0].detail
    assert not report.ok


def test_distinct_keys_are_not_flagged(tmp_path, synthetic_key) -> None:
    home, folder = _audit_home(tmp_path)
    identity = folder / "agents" / "lumina" / "capauth" / "identity"
    identity.mkdir(parents=True)
    (identity / "private.asc").write_text(synthetic_key, encoding="utf-8")
    other = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 1024)
    uid = pgpy.PGPUID.new("Other Synthetic", email="other@example.invalid")
    other.add_uid(
        uid,
        usage={KeyFlags.Sign},
        hashes=[HashAlgorithm.SHA256],
        ciphers=[SymmetricKeyAlgorithm.AES256],
        compression=[CompressionAlgorithm.Uncompressed],
    )
    agent_identity = home / ".skcapstone" / "agents" / "opus" / "capauth" / "identity"
    agent_identity.mkdir(parents=True)
    (agent_identity / "private.asc").write_text(str(other), encoding="utf-8")
    report = audit(home=home)
    assert not any(f.category == "duplicate_private_fingerprint" for f in report.findings)
    assert report.ok


# ------------------------------------------------------------- remediation ---


def test_dry_run_remediation_lines_and_no_writes(tmp_path) -> None:
    home, folder = _audit_home(tmp_path, rules="*.key\n")
    before = {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    report = audit(home=home)
    assert not report.ok
    lines = report.folders[0].remediation_lines
    # The covering rule already present is not re-added; every gap is named.
    assert "*.key" not in lines
    assert "*.pem" in lines
    assert "**/private.*" in lines
    assert "capauth/security/tokens" in lines
    assert report.dry_run
    assert report.applied == ()
    after = {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    assert before == after


def test_apply_unions_rules_and_is_idempotent(tmp_path) -> None:
    home, folder = _audit_home(tmp_path, rules="// hand-tuned\n*.key\n")
    report = audit(home=home, apply=True)
    target = folder / ".stignore"
    assert report.applied == (str(target),)
    text = target.read_text(encoding="utf-8")
    assert "// hand-tuned" in text  # existing content preserved
    assert "*.pem" in text and "**/private.*" in text
    assert (
        (folder / ".stignore.bak-sync-policy")
        .read_text(encoding="utf-8")
        .startswith("// hand-tuned")
    )
    second = audit(home=home, apply=True)
    assert second.applied == ()
    assert second.folders[0].severity != "error"


def test_json_report_shape(tmp_path) -> None:
    home, _ = _audit_home(tmp_path)
    report = audit(home=home)
    payload = json.loads(json.dumps(report.model_dump(mode="json"), default=str))
    assert payload["ok"] is True
    assert payload["severity"] == "ok"
    assert payload["folders"][0]["folder_id"] == "skcapstone-sync"
    assert payload["folders"][0]["severity"] == "ok"


def test_evaluate_exported_for_callers() -> None:
    """The semantics helper is part of the package surface."""
    rules = load_ruleset("*.key\n")
    assert evaluate(rules, "x.key") is Coverage.IGNORED
