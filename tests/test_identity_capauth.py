"""Tests for CapAuth identity integration.

Covers both paths: real CapAuth profile present vs placeholder fallback.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from skcapstone.discovery import (
    _sync_identity_json,
    _try_load_capauth_profile,
    discover_identity,
)
from skcapstone.models import IdentityState, PillarStatus
from skcapstone.pillars.identity import (
    _generate_placeholder_fingerprint,
    _try_init_capauth,
    generate_identity,
)


def _fake_profile(
    fingerprint: str = "A" * 40,
    name: str = "Opus",
    email: str = "opus@capauth.local",
) -> SimpleNamespace:
    """Build a fake SovereignProfile-like object for testing."""
    return SimpleNamespace(
        key_info=SimpleNamespace(
            fingerprint=fingerprint,
            public_key_path="/tmp/public.asc",
            created=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        entity=SimpleNamespace(name=name, email=email),
    )


# ---------------------------------------------------------------------------
# discover_identity: real CapAuth path
# ---------------------------------------------------------------------------


class TestDiscoverWithCapAuth:
    """Tests for discover_identity when a real CapAuth profile exists."""

    def test_uses_capauth_fingerprint(self, tmp_agent_home: Path):
        """discover_identity returns the real CapAuth fingerprint."""
        fake = _fake_profile(fingerprint="B" * 40, name="Jarvis")

        with patch(
            "skcapstone.discovery._try_load_capauth_profile",
            return_value=IdentityState(
                fingerprint=fake.key_info.fingerprint,
                name=fake.entity.name,
                email=fake.entity.email,
                key_path=Path(fake.key_info.public_key_path),
                created_at=fake.key_info.created,
                status=PillarStatus.ACTIVE,
            ),
        ):
            state = discover_identity(tmp_agent_home)

        assert state.fingerprint == "B" * 40
        assert state.name == "Jarvis"
        assert state.status == PillarStatus.ACTIVE

    def test_syncs_identity_json(self, tmp_agent_home: Path):
        """discover_identity writes identity.json with capauth_managed=True."""
        identity_dir = tmp_agent_home / "identity"
        identity_dir.mkdir(parents=True, exist_ok=True)

        fake = _fake_profile(fingerprint="C" * 40)
        with patch(
            "skcapstone.discovery._try_load_capauth_profile",
            return_value=IdentityState(
                fingerprint="C" * 40,
                name="Opus",
                email="opus@capauth.local",
                key_path=Path("/tmp/public.asc"),
                status=PillarStatus.ACTIVE,
            ),
        ):
            discover_identity(tmp_agent_home)

        manifest = json.loads((identity_dir / "identity.json").read_text())
        assert manifest["fingerprint"] == "C" * 40
        assert manifest["capauth_managed"] is True

    def test_replaces_placeholder_fingerprint(self, tmp_agent_home: Path):
        """When upgrading from placeholder to real keys, identity.json updates."""
        identity_dir = tmp_agent_home / "identity"
        identity_dir.mkdir(parents=True, exist_ok=True)

        old_manifest = {
            "name": "test",
            "email": "test@skcapstone.local",
            "fingerprint": _generate_placeholder_fingerprint("test"),
            "capauth_managed": False,
        }
        (identity_dir / "identity.json").write_text(json.dumps(old_manifest))

        with patch(
            "skcapstone.discovery._try_load_capauth_profile",
            return_value=IdentityState(
                fingerprint="D" * 40,
                name="test",
                email="test@capauth.local",
                status=PillarStatus.ACTIVE,
            ),
        ):
            state = discover_identity(tmp_agent_home)

        assert state.fingerprint == "D" * 40
        updated = json.loads((identity_dir / "identity.json").read_text())
        assert updated["fingerprint"] == "D" * 40
        assert updated["capauth_managed"] is True


# ---------------------------------------------------------------------------
# discover_identity: placeholder fallback
# ---------------------------------------------------------------------------


class TestDiscoverWithoutCapAuth:
    """Tests for discover_identity when CapAuth is not available."""

    def test_reads_existing_identity_json(self, tmp_agent_home: Path):
        """Falls back to reading identity.json when no CapAuth profile."""
        identity_dir = tmp_agent_home / "identity"
        identity_dir.mkdir(parents=True, exist_ok=True)

        manifest = {
            "name": "fallback-agent",
            "email": "fallback@skcapstone.local",
            "fingerprint": "F" * 40,
            "capauth_managed": False,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        (identity_dir / "identity.json").write_text(json.dumps(manifest))

        with patch("skcapstone.discovery._try_load_capauth_profile", return_value=None):
            state = discover_identity(tmp_agent_home)

        assert state.fingerprint == "F" * 40
        assert state.name == "fallback-agent"
        assert state.status == PillarStatus.DEGRADED

    def test_no_identity_at_all(self, tmp_agent_home: Path):
        """No identity.json and no CapAuth returns MISSING."""
        with patch("skcapstone.discovery._try_load_capauth_profile", return_value=None):
            state = discover_identity(tmp_agent_home)

        assert state.status == PillarStatus.MISSING

    def test_capauth_managed_true_is_active(self, tmp_agent_home: Path):
        """identity.json with capauth_managed=True is ACTIVE even without profile load."""
        identity_dir = tmp_agent_home / "identity"
        identity_dir.mkdir(parents=True, exist_ok=True)

        manifest = {
            "name": "real-agent",
            "email": "real@capauth.local",
            "fingerprint": "E" * 40,
            "capauth_managed": True,
        }
        (identity_dir / "identity.json").write_text(json.dumps(manifest))

        with patch("skcapstone.discovery._try_load_capauth_profile", return_value=None):
            state = discover_identity(tmp_agent_home)

        assert state.status == PillarStatus.ACTIVE


# ---------------------------------------------------------------------------
# generate_identity: init path
# ---------------------------------------------------------------------------


class TestGenerateIdentity:
    """Tests for generate_identity (the skcapstone init path)."""

    def test_placeholder_when_no_capauth(self, tmp_agent_home: Path):
        """Without CapAuth, generates a placeholder fingerprint."""
        with patch(
            "skcapstone.pillars.identity._try_init_capauth", return_value=None
        ):
            state = generate_identity(tmp_agent_home, "test-agent")

        assert state.status == PillarStatus.DEGRADED
        assert state.fingerprint == _generate_placeholder_fingerprint("test-agent")
        assert len(state.fingerprint) == 40

    def test_active_when_capauth_works(self, tmp_agent_home: Path):
        """With CapAuth, uses real keys and sets ACTIVE."""
        fake_state = IdentityState(
            fingerprint="A" * 40,
            key_path=Path("/tmp/pub.asc"),
            name="test",
            email="test@capauth.local",
            status=PillarStatus.ACTIVE,
        )
        with patch(
            "skcapstone.pillars.identity._try_init_capauth", return_value=fake_state
        ):
            state = generate_identity(tmp_agent_home, "test")

        assert state.fingerprint == "A" * 40
        assert state.status == PillarStatus.ACTIVE

    def test_identity_json_written(self, tmp_agent_home: Path):
        """identity.json is always written, even with placeholders."""
        with patch(
            "skcapstone.pillars.identity._try_init_capauth", return_value=None
        ):
            generate_identity(tmp_agent_home, "writer-test")

        assert (tmp_agent_home / "identity" / "identity.json").exists()
        data = json.loads(
            (tmp_agent_home / "identity" / "identity.json").read_text()
        )
        assert data["name"] == "writer-test"
        assert data["capauth_managed"] is False

    def test_placeholder_deterministic(self):
        """Placeholder fingerprints are deterministic for the same name."""
        fp1 = _generate_placeholder_fingerprint("agent-x")
        fp2 = _generate_placeholder_fingerprint("agent-x")
        fp3 = _generate_placeholder_fingerprint("agent-y")
        assert fp1 == fp2
        assert fp1 != fp3


# ---------------------------------------------------------------------------
# _sync_identity_json
# ---------------------------------------------------------------------------


class TestSyncIdentityJson:
    """Tests for the identity.json sync helper."""

    def test_creates_file_if_missing(self, tmp_path: Path):
        """Creates identity.json from scratch when it doesn't exist."""
        identity_dir = tmp_path / "identity"
        state = IdentityState(
            fingerprint="X" * 40,
            name="new-agent",
            email="new@capauth.local",
            status=PillarStatus.ACTIVE,
        )
        _sync_identity_json(identity_dir, state)

        assert (identity_dir / "identity.json").exists()
        data = json.loads((identity_dir / "identity.json").read_text())
        assert data["fingerprint"] == "X" * 40
        assert data["capauth_managed"] is True

    def test_skips_write_when_unchanged(self, tmp_path: Path):
        """Does not rewrite identity.json when fingerprint matches."""
        identity_dir = tmp_path / "identity"
        identity_dir.mkdir(parents=True)

        existing = {
            "name": "same",
            "email": "same@capauth.local",
            "fingerprint": "Y" * 40,
            "capauth_managed": True,
            "created_at": None,
        }
        path = identity_dir / "identity.json"
        path.write_text(json.dumps(existing))
        mtime_before = path.stat().st_mtime

        state = IdentityState(
            fingerprint="Y" * 40,
            name="same",
            email="same@capauth.local",
            status=PillarStatus.ACTIVE,
        )
        _sync_identity_json(identity_dir, state)

        assert path.stat().st_mtime == mtime_before

    def test_updates_when_fingerprint_changes(self, tmp_path: Path):
        """Rewrites identity.json when the fingerprint is different."""
        identity_dir = tmp_path / "identity"
        identity_dir.mkdir(parents=True)

        old = {"fingerprint": "OLD" + "0" * 37, "capauth_managed": False}
        (identity_dir / "identity.json").write_text(json.dumps(old))

        state = IdentityState(
            fingerprint="NEW" + "1" * 37,
            name="upgraded",
            status=PillarStatus.ACTIVE,
        )
        _sync_identity_json(identity_dir, state)

        data = json.loads((identity_dir / "identity.json").read_text())
        assert data["fingerprint"] == "NEW" + "1" * 37
        assert data["capauth_managed"] is True
