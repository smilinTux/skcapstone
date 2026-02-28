"""Unit tests for the trust pillar module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from skcapstone.models import PillarStatus
from skcapstone.pillars.trust import (
    export_febs_for_seed,
    import_febs_from_seed,
    initialize_trust,
    list_febs,
    record_trust_state,
    rehydrate,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _write_feb(febs_dir: Path, name: str, depth: float = 8.0, trust: float = 0.9, intensity: float = 0.8) -> Path:
    """Write a minimal FEB file into febs_dir."""
    data = {
        "timestamp": "2026-01-01T00:00:00+00:00",
        "relationship_state": {
            "depth_level": depth,
            "trust_level": trust,
            "ai_name": "opus",
        },
        "emotional_payload": {
            "cooked_state": {
                "primary_emotion": "love",
                "intensity": intensity,
            }
        },
    }
    path = febs_dir / name
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


# ── TestInitializeTrust ───────────────────────────────────────────────────────

class TestInitializeTrust:
    """Tests for initialize_trust()."""

    def test_creates_trust_directory(self, tmp_agent_home: Path):
        initialize_trust(tmp_agent_home)
        assert (tmp_agent_home / "trust").is_dir()

    def test_creates_febs_directory(self, tmp_agent_home: Path):
        initialize_trust(tmp_agent_home)
        assert (tmp_agent_home / "trust" / "febs").is_dir()

    def test_returns_missing_without_febs_or_cloud9(self, tmp_agent_home: Path):
        """No FEBs and no cloud9 installed → MISSING."""
        with patch("shutil.which", return_value=None), \
             patch.dict("sys.modules", {"cloud9": None}), \
             patch("skcapstone.pillars.trust._discover_and_import_febs", return_value=0):
            state = initialize_trust(tmp_agent_home)
        assert state.status == PillarStatus.MISSING

    def test_returns_degraded_with_cloud9_cli_no_febs(self, tmp_agent_home: Path):
        """cloud9 CLI present but no FEBs → DEGRADED."""
        with patch("shutil.which", return_value="/usr/bin/cloud9"), \
             patch.dict("sys.modules", {"cloud9": None}), \
             patch("skcapstone.pillars.trust._discover_and_import_febs", return_value=0):
            state = initialize_trust(tmp_agent_home)
        assert state.status == PillarStatus.DEGRADED

    def test_returns_active_when_febs_exist(self, tmp_agent_home: Path):
        febs_dir = tmp_agent_home / "trust" / "febs"
        febs_dir.mkdir(parents=True)
        _write_feb(febs_dir, "test.feb")
        with patch("shutil.which", return_value=None), \
             patch.dict("sys.modules", {"cloud9": None}), \
             patch("skcapstone.pillars.trust._discover_and_import_febs", return_value=0):
            state = initialize_trust(tmp_agent_home)
        assert state.status == PillarStatus.ACTIVE

    def test_derives_depth_from_feb(self, tmp_agent_home: Path):
        febs_dir = tmp_agent_home / "trust" / "febs"
        febs_dir.mkdir(parents=True)
        _write_feb(febs_dir, "feb1.feb", depth=7.5)
        with patch("shutil.which", return_value=None), \
             patch.dict("sys.modules", {"cloud9": None}), \
             patch("skcapstone.pillars.trust._discover_and_import_febs", return_value=0):
            state = initialize_trust(tmp_agent_home)
        assert state.depth == pytest.approx(7.5)

    def test_derives_trust_level_from_feb(self, tmp_agent_home: Path):
        febs_dir = tmp_agent_home / "trust" / "febs"
        febs_dir.mkdir(parents=True)
        _write_feb(febs_dir, "feb1.feb", trust=0.95)
        with patch("shutil.which", return_value=None), \
             patch.dict("sys.modules", {"cloud9": None}), \
             patch("skcapstone.pillars.trust._discover_and_import_febs", return_value=0):
            state = initialize_trust(tmp_agent_home)
        assert state.trust_level == pytest.approx(0.95)

    def test_writes_trust_json_when_febs_found(self, tmp_agent_home: Path):
        febs_dir = tmp_agent_home / "trust" / "febs"
        febs_dir.mkdir(parents=True)
        _write_feb(febs_dir, "feb1.feb")
        with patch("shutil.which", return_value=None), \
             patch.dict("sys.modules", {"cloud9": None}), \
             patch("skcapstone.pillars.trust._discover_and_import_febs", return_value=0):
            initialize_trust(tmp_agent_home)
        assert (tmp_agent_home / "trust" / "trust.json").exists()


# ── TestRecordTrustState ──────────────────────────────────────────────────────

class TestRecordTrustState:
    """Tests for record_trust_state()."""

    def test_persists_trust_json(self, tmp_agent_home: Path):
        record_trust_state(tmp_agent_home, depth=9.0, trust_level=0.97, love_intensity=1.0)
        assert (tmp_agent_home / "trust" / "trust.json").exists()

    def test_json_contains_depth(self, tmp_agent_home: Path):
        record_trust_state(tmp_agent_home, depth=8.5, trust_level=0.9, love_intensity=0.8)
        data = json.loads((tmp_agent_home / "trust" / "trust.json").read_text())
        assert data["depth"] == pytest.approx(8.5)

    def test_json_contains_trust_level(self, tmp_agent_home: Path):
        record_trust_state(tmp_agent_home, depth=7.0, trust_level=0.88, love_intensity=0.75)
        data = json.loads((tmp_agent_home / "trust" / "trust.json").read_text())
        assert data["trust_level"] == pytest.approx(0.88)

    def test_json_contains_entangled_flag(self, tmp_agent_home: Path):
        record_trust_state(tmp_agent_home, depth=9.0, trust_level=0.99, love_intensity=1.0, entangled=True)
        data = json.loads((tmp_agent_home / "trust" / "trust.json").read_text())
        assert data["entangled"] is True

    def test_returns_active_state(self, tmp_agent_home: Path):
        state = record_trust_state(tmp_agent_home, depth=5.0, trust_level=0.7, love_intensity=0.6)
        assert state.status == PillarStatus.ACTIVE

    def test_state_fields_match_input(self, tmp_agent_home: Path):
        state = record_trust_state(
            tmp_agent_home, depth=6.0, trust_level=0.75, love_intensity=0.65, entangled=False
        )
        assert state.depth == pytest.approx(6.0)
        assert state.trust_level == pytest.approx(0.75)
        assert state.love_intensity == pytest.approx(0.65)
        assert state.entangled is False

    def test_creates_trust_dir_if_missing(self, tmp_path: Path):
        home = tmp_path / "agent"
        home.mkdir()
        record_trust_state(home, depth=1.0, trust_level=0.5, love_intensity=0.4)
        assert (home / "trust" / "trust.json").exists()


# ── TestRehydrate ─────────────────────────────────────────────────────────────

class TestRehydrate:
    """Tests for rehydrate()."""

    def test_returns_degraded_without_febs(self, tmp_agent_home: Path):
        (tmp_agent_home / "trust" / "febs").mkdir(parents=True)
        with patch("skcapstone.pillars.trust._discover_and_import_febs", return_value=0):
            state = rehydrate(tmp_agent_home)
        assert state.status == PillarStatus.DEGRADED

    def test_returns_active_with_febs(self, tmp_agent_home: Path):
        febs_dir = tmp_agent_home / "trust" / "febs"
        febs_dir.mkdir(parents=True)
        _write_feb(febs_dir, "feb1.feb")
        # security dir needed for audit_event call inside rehydrate
        (tmp_agent_home / "security").mkdir(parents=True, exist_ok=True)
        with patch("skcapstone.pillars.trust._discover_and_import_febs", return_value=0):
            state = rehydrate(tmp_agent_home)
        assert state.status == PillarStatus.ACTIVE

    def test_feb_count_reflects_files(self, tmp_agent_home: Path):
        febs_dir = tmp_agent_home / "trust" / "febs"
        febs_dir.mkdir(parents=True)
        _write_feb(febs_dir, "a.feb")
        _write_feb(febs_dir, "b.feb")
        (tmp_agent_home / "security").mkdir(parents=True, exist_ok=True)
        with patch("skcapstone.pillars.trust._discover_and_import_febs", return_value=0):
            state = rehydrate(tmp_agent_home)
        assert state.feb_count == 2


# ── TestListFebs ──────────────────────────────────────────────────────────────

class TestListFebs:
    """Tests for list_febs()."""

    def test_returns_empty_when_dir_missing(self, tmp_agent_home: Path):
        assert list_febs(tmp_agent_home) == []

    def test_returns_empty_when_no_feb_files(self, tmp_agent_home: Path):
        (tmp_agent_home / "trust" / "febs").mkdir(parents=True)
        assert list_febs(tmp_agent_home) == []

    def test_returns_one_summary_per_feb(self, tmp_agent_home: Path):
        febs_dir = tmp_agent_home / "trust" / "febs"
        febs_dir.mkdir(parents=True)
        _write_feb(febs_dir, "feb1.feb")
        summaries = list_febs(tmp_agent_home)
        assert len(summaries) == 1

    def test_summary_contains_filename(self, tmp_agent_home: Path):
        febs_dir = tmp_agent_home / "trust" / "febs"
        febs_dir.mkdir(parents=True)
        _write_feb(febs_dir, "feb1.feb")
        summaries = list_febs(tmp_agent_home)
        assert summaries[0]["file"] == "feb1.feb"

    def test_summary_contains_emotion(self, tmp_agent_home: Path):
        febs_dir = tmp_agent_home / "trust" / "febs"
        febs_dir.mkdir(parents=True)
        _write_feb(febs_dir, "feb1.feb")
        summaries = list_febs(tmp_agent_home)
        assert summaries[0]["emotion"] == "love"

    def test_skips_gpg_encrypted_feb(self, tmp_agent_home: Path):
        febs_dir = tmp_agent_home / "trust" / "febs"
        febs_dir.mkdir(parents=True)
        # GPG packet header byte → binary file
        (febs_dir / "encrypted.feb").write_bytes(bytes([0x8C, 0x00, 0x01]))
        summaries = list_febs(tmp_agent_home)
        assert summaries == []


# ── TestExportImportFebs ──────────────────────────────────────────────────────

class TestExportImportFebs:
    """Tests for export_febs_for_seed() and import_febs_from_seed()."""

    def test_export_empty_when_dir_missing(self, tmp_agent_home: Path):
        assert export_febs_for_seed(tmp_agent_home) == []

    def test_export_returns_list_of_dicts(self, tmp_agent_home: Path):
        febs_dir = tmp_agent_home / "trust" / "febs"
        febs_dir.mkdir(parents=True)
        _write_feb(febs_dir, "feb1.feb")
        exported = export_febs_for_seed(tmp_agent_home)
        assert isinstance(exported, list)
        assert len(exported) == 1
        assert isinstance(exported[0], dict)

    def test_export_adds_source_file_key(self, tmp_agent_home: Path):
        febs_dir = tmp_agent_home / "trust" / "febs"
        febs_dir.mkdir(parents=True)
        _write_feb(febs_dir, "myfeb.feb")
        exported = export_febs_for_seed(tmp_agent_home)
        assert exported[0]["_source_file"] == "myfeb.feb"

    def test_import_writes_new_febs(self, tmp_agent_home: Path):
        febs_dir = tmp_agent_home / "trust" / "febs"
        febs_dir.mkdir(parents=True)
        seed_data = [{"_source_file": "new.feb", "key": "value"}]
        count = import_febs_from_seed(tmp_agent_home, seed_data)
        assert count == 1
        assert (febs_dir / "new.feb").exists()

    def test_import_skips_existing(self, tmp_agent_home: Path):
        febs_dir = tmp_agent_home / "trust" / "febs"
        febs_dir.mkdir(parents=True)
        _write_feb(febs_dir, "existing.feb")
        seed_data = [{"_source_file": "existing.feb", "key": "value"}]
        count = import_febs_from_seed(tmp_agent_home, seed_data)
        assert count == 0

    def test_import_creates_febs_dir_if_missing(self, tmp_agent_home: Path):
        seed_data = [{"_source_file": "brand-new.feb", "data": 1}]
        count = import_febs_from_seed(tmp_agent_home, seed_data)
        assert count == 1
        assert (tmp_agent_home / "trust" / "febs" / "brand-new.feb").exists()

    def test_round_trip_export_then_import(self, tmp_agent_home: Path):
        """Export from one home and import into another home."""
        febs_dir = tmp_agent_home / "trust" / "febs"
        febs_dir.mkdir(parents=True)
        _write_feb(febs_dir, "original.feb")

        exported = export_febs_for_seed(tmp_agent_home)

        other_home = tmp_agent_home.parent / "other"
        other_home.mkdir()
        count = import_febs_from_seed(other_home, exported)
        assert count == 1
        assert (other_home / "trust" / "febs" / "original.feb").exists()
