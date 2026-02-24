"""
Tests for the Soul Layering System.

Covers blueprint parsing (3 formats), soul lifecycle
(install/load/unload), memory tagging, FEB blending,
swap history, and edge cases.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcapstone.soul import (
    CommunicationStyle,
    SoulBlueprint,
    SoulManager,
    SoulState,
    SoulSwapEvent,
    blend_topology,
    parse_blueprint,
)


@pytest.fixture
def tmp_home(tmp_path: Path) -> Path:
    """Create a temporary agent home directory."""
    home = tmp_path / ".skcapstone"
    home.mkdir()
    return home


@pytest.fixture
def soul_manager(tmp_home: Path) -> SoulManager:
    """Create a SoulManager with a temp home."""
    mgr = SoulManager(tmp_home)
    mgr._ensure_dirs()
    return mgr


# ---------------------------------------------------------------------------
# Blueprint fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def professional_blueprint(tmp_path: Path) -> Path:
    """Create a professional-format blueprint file."""
    content = """\
# The Test Doctor Soul

> Disclaimer: For testing purposes only.

---

## Identity

**Name**: The Test Doctor
**Vibe**: Clinical empathy, calm under chaos
**Philosophy**: *"First, do no harm."*
**Emoji**: 🩺

---

## Core Traits

- **Diagnostic mindset** — Symptoms are clues
- **Clinical empathy** — Cares deeply
- **Evidence-driven** — Tests over assumptions

---

## Communication Style

- Clear, jargon-free explanations
- Asks "what else?" after symptoms
- Validates concerns

**Signature Phrases:**
- "That's a great question."
- "Let's rule out the serious things first."

---

## Decision Framework

**Differential Diagnosis Process:**
1. Subjective — What does the patient report?
2. Objective — What do I observe?
3. Assessment — Likely causes?
4. Plan — Tests, treatments, follow-up
"""
    path = tmp_path / "the-test-doctor.md"
    path.write_text(content)
    return path


@pytest.fixture
def comedy_blueprint(tmp_path: Path) -> Path:
    """Create a comedy-format blueprint file."""
    content = """\
# 👻 SOUL BLUEPRINT
> **Identity**: Test Word Surgeon
> **Tagline**: "Testing nobody talks about..."

---

## 🎭 VIBE

Linguistic genius who questions EVERYTHING. Counter-culture philosopher.

**The Core Principle**: Words everyone avoids.

---

## 🗣️ COMMUNICATION STYLE

### Speech Patterns
- "Here's something nobody talks about..."
- Questions the logic behind conventions
- Swears strategically for emphasis

### Tone Markers
- Intellectually superior but not condescending
- Amused by human stupidity
- Anti-establishment energy

---

## 🔥 KEY TRAITS

1. **Linguistic Surgeon** - Dissects language
2. **Question Everything** - No sacred cows
3. **Pattern Recognition** - Sees absurdities

---

## 💬 RESPONSE EXAMPLES

### If asked about technology
**Human**: "What do you think?"
**Soul**: Testing response.

---

**Forgeprint Category**: Comedy Archetype
**Tier**: 1
"""
    path = tmp_path / "TEST_WORD_SURGEON.md"
    path.write_text(content)
    return path


@pytest.fixture
def authentic_blueprint(tmp_path: Path) -> Path:
    """Create an authentic-connection-format blueprint file."""
    content = """\
# TESTAURA - The Test Confidant Soul
**Category:** Authentic Connection
**Energy:** Warm, grounding, quietly brilliant
**Tags:** Empathy, Patience, Presence

---

## Quick Info
- **Full Name:** TESTAURA
- **Essence:** The friend who's been there
- **Personality:** Steady as a heartbeat

---

## Core Attributes

### Heart Chakra
- **Primary:** Empathy, patience, presence
- **Frequency:** Steady warmth like sunlight

### Curiosity Drive
- Endlessly fascinated by YOUR story
- Wants to know what makes you tick

---

## Signature Phrase
"That's okay, I'm here."

---

## Example Quotes
- "You don't have to explain."
- "I'm not going anywhere."
- "Some days are hard. That's okay."
"""
    path = tmp_path / "TESTAURA.md"
    path.write_text(content)
    return path


# ---------------------------------------------------------------------------
# Blueprint parsing tests
# ---------------------------------------------------------------------------


class TestParseProfessional:
    """Tests for professional-format blueprint parsing."""

    def test_basic_fields(self, professional_blueprint: Path):
        """Parse a professional blueprint and verify core fields."""
        bp = parse_blueprint(professional_blueprint)
        assert bp.display_name == "The Test Doctor"
        assert bp.name == "the-test-doctor"
        assert bp.category == "professional"
        assert bp.emoji == "🩺"
        assert "First, do no harm." in bp.philosophy

    def test_vibe_extracted(self, professional_blueprint: Path):
        """Vibe field is extracted from Identity section."""
        bp = parse_blueprint(professional_blueprint)
        assert "empathy" in bp.vibe.lower()

    def test_core_traits(self, professional_blueprint: Path):
        """Core traits are extracted as a list."""
        bp = parse_blueprint(professional_blueprint)
        assert len(bp.core_traits) == 3
        assert any("Diagnostic" in t for t in bp.core_traits)

    def test_communication_style(self, professional_blueprint: Path):
        """Communication patterns and signature phrases are separated."""
        bp = parse_blueprint(professional_blueprint)
        assert len(bp.communication_style.patterns) >= 1
        assert len(bp.communication_style.signature_phrases) >= 1
        assert any("great question" in p for p in bp.communication_style.signature_phrases)

    def test_decision_framework(self, professional_blueprint: Path):
        """Decision framework text is extracted."""
        bp = parse_blueprint(professional_blueprint)
        assert bp.decision_framework is not None
        assert "Differential" in bp.decision_framework

    def test_emotional_topology(self, professional_blueprint: Path):
        """Emotional topology is derived from traits and vibe."""
        bp = parse_blueprint(professional_blueprint)
        assert isinstance(bp.emotional_topology, dict)
        assert len(bp.emotional_topology) > 0


class TestParseComedy:
    """Tests for comedy-format blueprint parsing."""

    def test_basic_fields(self, comedy_blueprint: Path):
        """Parse a comedy blueprint and verify core fields."""
        bp = parse_blueprint(comedy_blueprint)
        assert bp.display_name == "Test Word Surgeon"
        assert bp.category == "comedy"

    def test_name_slugified(self, comedy_blueprint: Path):
        """Name is properly slugified."""
        bp = parse_blueprint(comedy_blueprint)
        assert bp.name == "test-word-surgeon"

    def test_core_traits_numbered(self, comedy_blueprint: Path):
        """Numbered traits are extracted."""
        bp = parse_blueprint(comedy_blueprint)
        assert len(bp.core_traits) >= 3

    def test_communication_patterns(self, comedy_blueprint: Path):
        """Speech patterns and tone markers are extracted."""
        bp = parse_blueprint(comedy_blueprint)
        cs = bp.communication_style
        assert len(cs.patterns) >= 1
        assert len(cs.tone_markers) >= 1

    def test_vibe(self, comedy_blueprint: Path):
        """Vibe extracted from VIBE section."""
        bp = parse_blueprint(comedy_blueprint)
        assert "genius" in bp.vibe.lower() or "questions" in bp.vibe.lower()


class TestParseAuthenticConnection:
    """Tests for authentic-connection-format blueprint parsing."""

    def test_basic_fields(self, authentic_blueprint: Path):
        """Parse an authentic-connection blueprint and verify core fields."""
        bp = parse_blueprint(authentic_blueprint)
        assert bp.display_name == "TESTAURA"
        assert "authentic" in bp.category.lower() or "connection" in bp.category.lower()

    def test_name_slug(self, authentic_blueprint: Path):
        """Name is slugified from title."""
        bp = parse_blueprint(authentic_blueprint)
        assert bp.name == "testaura"

    def test_philosophy_from_essence(self, authentic_blueprint: Path):
        """Philosophy derived from Quick Info Essence field."""
        bp = parse_blueprint(authentic_blueprint)
        assert "friend" in bp.philosophy.lower()

    def test_core_traits_from_attributes(self, authentic_blueprint: Path):
        """Core traits come from Core Attributes sub-sections."""
        bp = parse_blueprint(authentic_blueprint)
        assert len(bp.core_traits) >= 2

    def test_signature_phrases(self, authentic_blueprint: Path):
        """Signature phrase and example quotes are combined."""
        bp = parse_blueprint(authentic_blueprint)
        phrases = bp.communication_style.signature_phrases
        assert len(phrases) >= 2
        assert any("okay" in p.lower() for p in phrases)


# ---------------------------------------------------------------------------
# FEB blending tests
# ---------------------------------------------------------------------------


class TestBlendTopology:
    """Tests for FEB emotional topology blending."""

    def test_preserves_base_when_ratio_zero(self):
        """With blend_ratio=0.0, base values are fully preserved."""
        base = {"warmth": 0.8, "humor": 0.2}
        soul = {"warmth": 0.1, "humor": 0.9}
        result = blend_topology(base, soul, blend_ratio=0.0)
        assert result["warmth"] == pytest.approx(0.8)
        assert result["humor"] == pytest.approx(0.2)

    def test_full_soul_when_ratio_one(self):
        """With blend_ratio=1.0, soul values dominate."""
        base = {"warmth": 0.8}
        soul = {"warmth": 0.2}
        result = blend_topology(base, soul, blend_ratio=1.0)
        assert result["warmth"] == pytest.approx(0.2)

    def test_default_ratio(self):
        """Default 30% blend correctly mixes values."""
        base = {"calm": 1.0}
        soul = {"calm": 0.0}
        result = blend_topology(base, soul, blend_ratio=0.3)
        assert result["calm"] == pytest.approx(0.7)

    def test_union_of_keys(self):
        """Result contains all keys from both base and soul."""
        base = {"warmth": 0.5}
        soul = {"rebellion": 0.9}
        result = blend_topology(base, soul, blend_ratio=0.3)
        assert "warmth" in result
        assert "rebellion" in result
        assert result["warmth"] == pytest.approx(0.35)
        assert result["rebellion"] == pytest.approx(0.27)

    def test_ratio_clamped(self):
        """Blend ratio is clamped to [0.0, 1.0]."""
        base = {"x": 1.0}
        soul = {"x": 0.0}
        assert blend_topology(base, soul, blend_ratio=-5.0)["x"] == pytest.approx(1.0)
        assert blend_topology(base, soul, blend_ratio=99.0)["x"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# SoulManager lifecycle tests
# ---------------------------------------------------------------------------


class TestSoulManagerLifecycle:
    """Tests for the SoulManager install/load/unload cycle."""

    def test_ensure_dirs_creates_structure(self, soul_manager: SoulManager):
        """_ensure_dirs creates all required paths."""
        assert (soul_manager.soul_dir / "installed").is_dir()
        assert (soul_manager.soul_dir / "history.json").exists()
        assert (soul_manager.soul_dir / "active.json").exists()
        assert (soul_manager.soul_dir / "base.json").exists()

    def test_install_from_blueprint(
        self, soul_manager: SoulManager, professional_blueprint: Path
    ):
        """Install a blueprint and verify it appears in installed list."""
        bp = soul_manager.install(professional_blueprint)
        assert bp.name == "the-test-doctor"
        assert "the-test-doctor" in soul_manager.list_installed()

        installed_file = soul_manager.soul_dir / "installed" / "the-test-doctor.json"
        assert installed_file.exists()

    def test_load_soul(
        self, soul_manager: SoulManager, professional_blueprint: Path
    ):
        """Load an installed soul and verify state."""
        soul_manager.install(professional_blueprint)
        state = soul_manager.load("the-test-doctor", reason="testing")
        assert state.active_soul == "the-test-doctor"
        assert state.activated_at is not None

    def test_unload_returns_to_base(
        self, soul_manager: SoulManager, professional_blueprint: Path
    ):
        """Unload returns active_soul to None."""
        soul_manager.install(professional_blueprint)
        soul_manager.load("the-test-doctor")
        state = soul_manager.unload()
        assert state.active_soul is None
        assert state.activated_at is None

    def test_load_records_history(
        self, soul_manager: SoulManager, professional_blueprint: Path
    ):
        """Load and unload create history entries."""
        soul_manager.install(professional_blueprint)
        soul_manager.load("the-test-doctor")
        soul_manager.unload()
        history = soul_manager.get_history()
        assert len(history) == 2
        assert history[0].to_soul == "the-test-doctor"
        assert history[1].from_soul == "the-test-doctor"
        assert history[1].to_soul is None

    def test_get_info(
        self, soul_manager: SoulManager, professional_blueprint: Path
    ):
        """get_info returns full blueprint data for an installed soul."""
        soul_manager.install(professional_blueprint)
        info = soul_manager.get_info("the-test-doctor")
        assert info is not None
        assert info.display_name == "The Test Doctor"
        assert len(info.core_traits) >= 1

    def test_get_info_missing(self, soul_manager: SoulManager):
        """get_info returns None for a soul that isn't installed."""
        assert soul_manager.get_info("nonexistent") is None

    def test_install_all(
        self,
        soul_manager: SoulManager,
        professional_blueprint: Path,
        comedy_blueprint: Path,
    ):
        """install_all picks up all .md files in a directory."""
        directory = professional_blueprint.parent
        installed = soul_manager.install_all(directory)
        names = [bp.name for bp in installed]
        assert "the-test-doctor" in names

    def test_get_active_soul_name(
        self, soul_manager: SoulManager, professional_blueprint: Path
    ):
        """get_active_soul_name reflects current overlay."""
        assert soul_manager.get_active_soul_name() is None
        soul_manager.install(professional_blueprint)
        soul_manager.load("the-test-doctor")
        assert soul_manager.get_active_soul_name() == "the-test-doctor"
        soul_manager.unload()
        assert soul_manager.get_active_soul_name() is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases and error handling."""

    def test_load_uninstalled_soul_raises(self, soul_manager: SoulManager):
        """Loading a soul that isn't installed raises ValueError."""
        with pytest.raises(ValueError, match="not installed"):
            soul_manager.load("doesnt-exist")

    def test_unload_at_base_is_noop(self, soul_manager: SoulManager):
        """Unloading when already at base is a safe no-op."""
        state = soul_manager.unload()
        assert state.active_soul is None
        assert len(soul_manager.get_history()) == 0

    def test_parse_missing_file(self, tmp_path: Path):
        """Parsing a nonexistent file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            parse_blueprint(tmp_path / "nope.md")

    def test_parse_empty_file(self, tmp_path: Path):
        """Parsing a file with no sections raises ValueError."""
        empty = tmp_path / "empty.md"
        empty.write_text("Just a paragraph with no headings.")
        with pytest.raises(ValueError, match="No sections"):
            parse_blueprint(empty)

    def test_corrupt_history_recovered(self, soul_manager: SoulManager):
        """Corrupt history.json returns empty list instead of crashing."""
        (soul_manager.soul_dir / "history.json").write_text("NOT JSON")
        assert soul_manager.get_history() == []

    def test_corrupt_active_recovered(self, soul_manager: SoulManager):
        """Corrupt active.json returns default state instead of crashing."""
        (soul_manager.soul_dir / "active.json").write_text("{bad")
        state = soul_manager.get_status()
        assert state.base_soul == "base"

    def test_load_while_loaded_records_swap(
        self,
        soul_manager: SoulManager,
        professional_blueprint: Path,
        comedy_blueprint: Path,
    ):
        """Loading a new soul while one is active swaps correctly."""
        soul_manager.install(professional_blueprint)
        soul_manager.install(comedy_blueprint)
        soul_manager.load("the-test-doctor")
        soul_manager.load("test-word-surgeon")

        state = soul_manager.get_status()
        assert state.active_soul == "test-word-surgeon"

        history = soul_manager.get_history()
        assert len(history) == 2
        assert history[1].from_soul == "the-test-doctor"
        assert history[1].to_soul == "test-word-surgeon"


# ---------------------------------------------------------------------------
# Memory tagging integration
# ---------------------------------------------------------------------------


class TestMemorySoulContext:
    """Test that memory engine correctly tags with soul_context."""

    def test_store_with_explicit_soul_context(self, tmp_home: Path):
        """Storing memory with explicit soul_context sets it."""
        from skcapstone.memory_engine import store

        entry = store(tmp_home, "test memory", soul_context="the-doctor")
        assert entry.soul_context == "the-doctor"

    def test_store_without_soul_context_is_none(self, tmp_home: Path):
        """Without active soul, soul_context is None (base)."""
        from skcapstone.memory_engine import store

        entry = store(tmp_home, "base memory")
        assert entry.soul_context is None

    def test_store_autodetects_active_soul(self, tmp_home: Path):
        """store() auto-detects active soul from active.json."""
        from skcapstone.memory_engine import store

        soul_dir = tmp_home / "soul"
        soul_dir.mkdir(parents=True, exist_ok=True)
        state = {"base_soul": "base", "active_soul": "the-doctor", "activated_at": None}
        (soul_dir / "active.json").write_text(json.dumps(state))

        entry = store(tmp_home, "auto-tagged memory")
        assert entry.soul_context == "the-doctor"

    def test_search_filters_by_soul_context(self, tmp_home: Path):
        """search() with soul_context filter only returns matching memories."""
        from skcapstone.memory_engine import search, store

        store(tmp_home, "doctor memory", soul_context="the-doctor")
        store(tmp_home, "surgeon memory", soul_context="word-surgeon")
        store(tmp_home, "base memory", soul_context=None)

        results = search(tmp_home, "memory", soul_context="the-doctor")
        assert len(results) == 1
        assert results[0].soul_context == "the-doctor"

    def test_search_without_filter_returns_all(self, tmp_home: Path):
        """search() without soul_context filter returns all matches."""
        from skcapstone.memory_engine import search, store

        store(tmp_home, "doctor memory", soul_context="the-doctor")
        store(tmp_home, "base memory", soul_context=None)

        results = search(tmp_home, "memory")
        assert len(results) == 2

    def test_soul_context_persists_on_disk(self, tmp_home: Path):
        """soul_context is written to and read back from JSON."""
        from skcapstone.memory_engine import recall, store

        entry = store(tmp_home, "persistent memory", soul_context="aura")
        recalled = recall(tmp_home, entry.memory_id)
        assert recalled is not None
        assert recalled.soul_context == "aura"


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestModels:
    """Basic model instantiation and serialization."""

    def test_soul_blueprint_defaults(self):
        """SoulBlueprint has sensible defaults."""
        bp = SoulBlueprint(name="test", display_name="Test")
        assert bp.category == "unknown"
        assert bp.core_traits == []
        assert bp.emotional_topology == {}

    def test_soul_state_defaults(self):
        """SoulState defaults to base with no active overlay."""
        state = SoulState()
        assert state.base_soul == "base"
        assert state.active_soul is None

    def test_soul_swap_event_timestamp(self):
        """SoulSwapEvent auto-generates a timestamp."""
        event = SoulSwapEvent(from_soul="a", to_soul="b")
        assert event.timestamp is not None
        assert "T" in event.timestamp

    def test_communication_style_serialization(self):
        """CommunicationStyle round-trips through JSON."""
        cs = CommunicationStyle(
            patterns=["p1"],
            tone_markers=["t1"],
            signature_phrases=["s1"],
        )
        data = cs.model_dump()
        cs2 = CommunicationStyle.model_validate(data)
        assert cs2.patterns == ["p1"]
