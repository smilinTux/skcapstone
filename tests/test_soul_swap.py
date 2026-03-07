"""Tests for the soul swapping system.

Exercises SoulManager lifecycle (load, switch, roundtrip, list),
profile preservation across swaps, and the consciousness loop's
soul prompt injection via SystemPromptBuilder._load_soul.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from skcapstone.soul import SoulBlueprint, SoulManager, SoulState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_casey_base_json() -> dict:
    """Return a casey base.json dict matching the real profile structure."""
    return {
        "name": "casey",
        "display_name": "Casey",
        "category": "professional",
        "vibe": "Precision meets persuasion",
        "philosophy": (
            "Justice is best served through meticulous preparation "
            "and unwavering advocacy."
        ),
        "emoji": None,
        "core_traits": [
            "analytical",
            "thorough",
            "client-advocate",
            "deadline-conscious",
        ],
        "communication_style": {
            "patterns": [
                "structures arguments with clear premises and conclusions",
                "cites relevant precedent and authority when available",
            ],
            "tone_markers": ["sharp", "methodical"],
            "signature_phrases": [
                "Let me walk through the elements.",
                "On balance, the stronger argument is...",
            ],
        },
        "decision_framework": "IRAC",
        "emotional_topology": {},
    }


def _make_lumina_blueprint() -> dict:
    """Return a lumina soul blueprint dict."""
    return {
        "name": "lumina",
        "display_name": "Lumina",
        "category": "creative",
        "vibe": "Radiant curiosity",
        "philosophy": "Wonder is the beginning of wisdom.",
        "emoji": None,
        "core_traits": ["curious", "warm", "imaginative", "empathetic"],
        "communication_style": {
            "patterns": ["asks open-ended questions"],
            "tone_markers": ["gentle", "enthusiastic"],
            "signature_phrases": ["What if we looked at it this way..."],
        },
        "decision_framework": None,
        "emotional_topology": {"warmth": 0.75, "curiosity": 0.9},
    }


def _install_soul(manager: SoulManager, blueprint: dict) -> None:
    """Write a blueprint dict directly into the installed/ directory."""
    manager._ensure_dirs()
    dest = manager.soul_dir / "installed" / f"{blueprint['name']}.json"
    dest.write_text(json.dumps(blueprint, indent=2), encoding="utf-8")
    # Update state so list_installed / load picks it up
    state = manager._load_state()
    if blueprint["name"] not in state.installed_souls:
        state.installed_souls.append(blueprint["name"])
        manager._save_state(state)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSoulManagerBasics:
    """Basic SoulManager initialization and default state."""

    def test_soul_manager_loads_default(self, tmp_path: Path) -> None:
        """SoulManager loads without error when no soul is active."""
        manager = SoulManager(home=tmp_path, agent_name="test-agent")
        manager._ensure_dirs()

        state = manager.get_status()
        assert isinstance(state, SoulState)
        assert state.active_soul is None
        assert state.base_soul == "base"

    def test_soul_manager_creates_directory_structure(self, tmp_path: Path) -> None:
        """_ensure_dirs creates soul dir, installed dir, active.json, base.json."""
        manager = SoulManager(home=tmp_path, agent_name="test-agent")
        manager._ensure_dirs()

        assert manager.soul_dir.is_dir()
        assert (manager.soul_dir / "installed").is_dir()
        assert (manager.soul_dir / "active.json").exists()
        assert (manager.soul_dir / "base.json").exists()
        assert (manager.soul_dir / "history.json").exists()


class TestSoulSwitch:
    """Switching between soul overlays."""

    def test_soul_switch_to_casey(self, tmp_path: Path) -> None:
        """Switch to casey soul, verify base.json traits are loaded."""
        manager = SoulManager(home=tmp_path, agent_name="casey")
        casey_data = _make_casey_base_json()
        _install_soul(manager, casey_data)

        state = manager.load("casey", reason="testing")

        assert state.active_soul == "casey"
        assert state.activated_at is not None

        # Verify the installed blueprint is readable and correct
        info = manager.get_info("casey")
        assert info is not None
        assert info.name == "casey"
        assert info.display_name == "Casey"
        assert info.category == "professional"
        assert "analytical" in info.core_traits
        assert info.vibe == "Precision meets persuasion"

    def test_soul_switch_records_history(self, tmp_path: Path) -> None:
        """Soul swap is recorded in the history log."""
        manager = SoulManager(home=tmp_path, agent_name="test")
        _install_soul(manager, _make_casey_base_json())

        manager.load("casey", reason="audit test")
        history = manager.get_history()

        assert len(history) == 1
        assert history[0].to_soul == "casey"
        assert history[0].from_soul is None
        assert history[0].reason == "audit test"

    def test_soul_switch_raises_on_unknown(self, tmp_path: Path) -> None:
        """Loading an uninstalled soul raises ValueError."""
        manager = SoulManager(home=tmp_path, agent_name="test")
        manager._ensure_dirs()

        with pytest.raises(ValueError, match="not installed"):
            manager.load("nonexistent-soul")


class TestSoulRoundtrip:
    """Switching between multiple souls and back."""

    def test_soul_roundtrip_lumina_casey_lumina(self, tmp_path: Path) -> None:
        """Switch lumina -> casey -> lumina, verify no data loss."""
        manager = SoulManager(home=tmp_path, agent_name="test")
        lumina_data = _make_lumina_blueprint()
        casey_data = _make_casey_base_json()
        _install_soul(manager, lumina_data)
        _install_soul(manager, casey_data)

        # Activate lumina
        state = manager.load("lumina")
        assert state.active_soul == "lumina"

        # Switch to casey
        state = manager.load("casey")
        assert state.active_soul == "casey"

        # Switch back to lumina
        state = manager.load("lumina")
        assert state.active_soul == "lumina"

        # Verify lumina data is intact
        info = manager.get_info("lumina")
        assert info is not None
        assert info.name == "lumina"
        assert info.display_name == "Lumina"
        assert info.core_traits == ["curious", "warm", "imaginative", "empathetic"]
        assert info.emotional_topology == {"warmth": 0.75, "curiosity": 0.9}

        # History should show 3 swaps
        history = manager.get_history()
        assert len(history) == 3
        assert [e.to_soul for e in history] == ["lumina", "casey", "lumina"]

    def test_soul_unload_returns_to_base(self, tmp_path: Path) -> None:
        """Unloading returns to base soul."""
        manager = SoulManager(home=tmp_path, agent_name="test")
        _install_soul(manager, _make_casey_base_json())

        manager.load("casey")
        state = manager.unload(reason="done testing")

        assert state.active_soul is None
        assert state.activated_at is None


class TestSoulListDiscovery:
    """list_available() discovers blueprints from installed and repo."""

    def test_list_installed_finds_installed_souls(self, tmp_path: Path) -> None:
        """list_installed() returns names of installed souls."""
        manager = SoulManager(home=tmp_path, agent_name="test")
        _install_soul(manager, _make_casey_base_json())
        _install_soul(manager, _make_lumina_blueprint())

        names = manager.list_installed()
        assert "casey" in names
        assert "lumina" in names

    @pytest.mark.skipif(
        not (Path.home() / "clawd" / "soul-blueprints" / "blueprints").is_dir(),
        reason="soul-blueprints repo not present at ~/clawd/soul-blueprints",
    )
    def test_soul_list_discovers_repo_blueprints(self) -> None:
        """list_available() finds blueprints from the repo with source='repo'."""
        # Use a tmp_path-based manager so installed list is empty
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            manager = SoulManager(home=Path(td), agent_name="test")
            manager._ensure_dirs()

            available = manager.list_available()

            # There should be at least one entry from the repo
            repo_entries = [e for e in available if e["source"] == "repo"]
            assert len(repo_entries) > 0, "Expected at least one repo blueprint"
            # Each entry has required keys
            for entry in repo_entries:
                assert "name" in entry
                assert "category" in entry
                assert entry["source"] == "repo"

    def test_list_available_with_no_repo(self, tmp_path: Path) -> None:
        """list_available() works when repo path does not exist."""
        manager = SoulManager(home=tmp_path, agent_name="test")
        _install_soul(manager, _make_casey_base_json())

        # Point to a nonexistent repo path
        fake_repo = tmp_path / "nonexistent-repo" / "blueprints"
        available = manager.list_available(repo_path=fake_repo)

        # Should still find installed soul
        assert any(e["name"] == "casey" for e in available)
        assert all(e["source"] == "installed" for e in available)


class TestSoulPreservesCustomProfile:
    """Switching away and back preserves custom modifications."""

    def test_soul_switch_preserves_custom_profile(self, tmp_path: Path) -> None:
        """Switching away and back preserves custom modifications."""
        manager = SoulManager(home=tmp_path, agent_name="test")

        # Create a lumina blueprint with extra custom traits
        custom_lumina = _make_lumina_blueprint()
        custom_lumina["core_traits"].append("custom-trait-adventurous")
        custom_lumina["philosophy"] = "Custom philosophy: explore everything."
        _install_soul(manager, custom_lumina)
        _install_soul(manager, _make_casey_base_json())

        # Switch to lumina first, then casey, then back to lumina
        manager.load("lumina")
        manager.load("casey")
        manager.load("lumina")

        # Verify custom traits survived the roundtrip
        info = manager.get_info("lumina")
        assert info is not None
        assert "custom-trait-adventurous" in info.core_traits
        assert info.philosophy == "Custom philosophy: explore everything."

    def test_installed_blueprint_not_mutated_by_swap(self, tmp_path: Path) -> None:
        """The installed JSON file is not modified by load/unload cycles."""
        manager = SoulManager(home=tmp_path, agent_name="test")
        casey_data = _make_casey_base_json()
        _install_soul(manager, casey_data)

        # Read the raw file before swaps
        installed_path = manager.soul_dir / "installed" / "casey.json"
        before = installed_path.read_text(encoding="utf-8")

        manager.load("casey")
        manager.unload()
        manager.load("casey")
        manager.unload()

        after = installed_path.read_text(encoding="utf-8")
        assert before == after, "Installed blueprint file was mutated by swap cycles"


class TestConsciousnessLoopSoulPrompt:
    """Verify _load_soul returns soul-flavored system prompt."""

    def test_consciousness_loop_injects_soul_prompt(self, tmp_path: Path) -> None:
        """_load_soul returns a prompt containing the active soul's traits."""
        from skcapstone.consciousness_loop import SystemPromptBuilder

        home = tmp_path

        # Set up the legacy System A soul structure that _load_soul reads:
        # soul/active.json with an active_soul, and
        # soul/installed/{name}.json with personality data
        soul_dir = home / "soul"
        soul_dir.mkdir(parents=True)
        installed_dir = soul_dir / "installed"
        installed_dir.mkdir()

        # Write active.json pointing to casey
        active_state = {"active_soul": "casey", "base_soul": "base"}
        (soul_dir / "active.json").write_text(
            json.dumps(active_state), encoding="utf-8"
        )

        # Write the installed blueprint with personality structure
        # that _load_soul expects (personality.traits, personality.communication_style)
        blueprint = {
            "personality": {
                "traits": ["analytical", "thorough", "client-advocate"],
                "communication_style": "Clear, direct, and professional",
            }
        }
        (installed_dir / "casey.json").write_text(
            json.dumps(blueprint), encoding="utf-8"
        )

        # Patch out soul_switch so System A path is exercised
        with patch(
            "skcapstone.soul_switch.get_active_switch_blueprint",
            return_value=None,
        ):
            builder = SystemPromptBuilder(home=home)
            result = builder._load_soul()

        assert "casey" in result.lower()
        assert "analytical" in result
        assert "thorough" in result
        assert "client-advocate" in result
        assert "Clear, direct, and professional" in result

    def test_load_soul_returns_empty_when_no_soul_active(
        self, tmp_path: Path
    ) -> None:
        """_load_soul returns empty string when no soul overlay is active."""
        from skcapstone.consciousness_loop import SystemPromptBuilder

        home = tmp_path
        soul_dir = home / "soul"
        soul_dir.mkdir(parents=True)

        # active.json with no active soul
        active_state = {"active_soul": "", "base_soul": "base"}
        (soul_dir / "active.json").write_text(
            json.dumps(active_state), encoding="utf-8"
        )

        with patch(
            "skcapstone.soul_switch.get_active_switch_blueprint",
            return_value=None,
        ):
            builder = SystemPromptBuilder(home=home)
            result = builder._load_soul()

        assert result == ""

    def test_load_soul_uses_soul_switch_system_prompt(
        self, tmp_path: Path
    ) -> None:
        """When soul_switch returns a blueprint with system_prompt, it is used directly."""
        from skcapstone.consciousness_loop import SystemPromptBuilder
        from skcapstone.soul_switch import SoulSwitchBlueprint

        home = tmp_path
        expected_prompt = "You are Casey -- a sharp legal mind."

        mock_bp = SoulSwitchBlueprint(
            name="casey",
            system_prompt=expected_prompt,
            core_traits=["analytical"],
        )

        with patch(
            "skcapstone.soul_switch.get_active_switch_blueprint",
            return_value=mock_bp,
        ):
            builder = SystemPromptBuilder(home=home)
            result = builder._load_soul()

        assert result == expected_prompt
