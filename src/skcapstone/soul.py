"""
Soul Layering System — hot-swappable personality overlays.

Soul is a lens. Memory is the ledger. Identity is permanent.

An agent has one base soul. Soul overlays can be installed from
the soul-blueprints repo and activated at runtime, changing *how*
the agent behaves without changing *who* it is. All memories
belong to the base soul, tagged with which overlay was active.

Supports both .md (parsed) and .yaml/.yml (direct load) blueprints.

Directory layout at runtime::

    ~/.skcapstone/soul/
        base.json           # Permanent base soul definition
        active.json         # Current overlay state (or null = base)
        installed/          # Parsed soul blueprints (JSON)
        history.json        # Soul swap audit log
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger("skcapstone.soul")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class CommunicationStyle(BaseModel):
    """Structured communication style extracted from a blueprint."""

    patterns: list[str] = Field(default_factory=list)
    tone_markers: list[str] = Field(default_factory=list)
    signature_phrases: list[str] = Field(default_factory=list)


class SoulBlueprint(BaseModel):
    """A parsed soul blueprint — the overlay definition."""

    name: str
    display_name: str
    category: str = "unknown"
    vibe: str = ""
    philosophy: str = ""
    emoji: Optional[str] = None
    core_traits: list[str] = Field(default_factory=list)
    communication_style: CommunicationStyle = Field(
        default_factory=CommunicationStyle
    )
    decision_framework: Optional[str] = None
    emotional_topology: dict[str, float] = Field(default_factory=dict)


class SoulState(BaseModel):
    """Persisted active state — who is the agent right now?"""

    base_soul: str = "base"
    active_soul: Optional[str] = None
    activated_at: Optional[str] = None
    installed_souls: list[str] = Field(default_factory=list)


class SoulSwapEvent(BaseModel):
    """Audit trail entry for soul swaps."""

    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    from_soul: Optional[str] = None
    to_soul: Optional[str] = None
    reason: str = ""
    duration_minutes: Optional[float] = None


# ---------------------------------------------------------------------------
# Blueprint parser
# ---------------------------------------------------------------------------

_SECTION_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)


def _split_sections(text: str) -> dict[str, str]:
    """Split markdown into {heading: body} pairs."""
    matches = list(_SECTION_RE.finditer(text))
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        heading = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[heading] = text[start:end].strip()
    return sections


def _extract_dash_items(block: str) -> list[str]:
    """Extract dash-list items from a markdown block."""
    items: list[str] = []
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            items.append(stripped[2:].strip())
    return items


def _extract_numbered_items(block: str) -> list[str]:
    """Extract numbered-list items (1. ...) from a markdown block."""
    items: list[str] = []
    for line in block.splitlines():
        stripped = line.strip()
        m = re.match(r"^\d+\.\s+(.+)$", stripped)
        if m:
            items.append(m.group(1).strip())
    return items


def _extract_bold_value(block: str, key: str) -> str:
    """Extract value from **Key**: Value or **Key:** Value patterns."""
    # Reason: blueprints use both **Key**: Value and **Key:** Value
    for pat in [
        re.compile(rf"\*\*{re.escape(key)}\*\*\s*[:：]\s*(.+)", re.IGNORECASE),
        re.compile(rf"\*\*{re.escape(key)}[:：]\*\*\s*(.+)", re.IGNORECASE),
    ]:
        m = pat.search(block)
        if m:
            return m.group(1).strip()
    return ""


def _extract_blockquote_value(text: str, key: str) -> str:
    """Extract value from > **Key**: Value blockquote pattern."""
    pattern = re.compile(
        rf">\s*\*\*{re.escape(key)}\*\*\s*[:：]\s*(.+)", re.IGNORECASE
    )
    m = pattern.search(text)
    return m.group(1).strip() if m else ""


def _detect_format(sections: dict[str, str], raw: str) -> str:
    """Detect which blueprint format variant we're dealing with."""
    headings_lower = {h.lower() for h in sections}
    heading_text = " ".join(headings_lower)

    if "vibe" in heading_text and "key traits" in heading_text:
        return "comedy"
    if "core attributes" in heading_text:
        return "authentic-connection"
    return "professional"


def _slugify(name: str) -> str:
    """Convert a display name to a URL-safe slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug.strip("-")


def _derive_topology(traits: list[str], vibe: str) -> dict[str, float]:
    """Derive emotional topology weights from traits and vibe text.

    Uses keyword matching to assign weights to emotional dimensions.
    This is a heuristic — not a neural model.
    """
    combined = " ".join(traits).lower() + " " + vibe.lower()
    dimensions = {
        "warmth": ["empathy", "warm", "kind", "care", "gentle", "love", "heart"],
        "precision": ["precise", "analyt", "logic", "diagnos", "systematic", "detail"],
        "humor": ["humor", "comedy", "laugh", "joke", "funny", "wit", "sarcas"],
        "authority": ["authority", "command", "leader", "confident", "decisive"],
        "curiosity": ["curious", "question", "explor", "learn", "fascin"],
        "rebellion": ["rebel", "anti-", "counter", "question everything", "unfilter"],
        "calm": ["calm", "steady", "patient", "grounding", "quiet"],
        "intensity": ["intense", "passion", "rage", "fire", "surgical"],
    }
    topology: dict[str, float] = {}
    for dim, keywords in dimensions.items():
        score = sum(1 for kw in keywords if kw in combined)
        if score > 0:
            topology[dim] = min(1.0, score * 0.25)
    return topology


def _parse_professional(
    sections: dict[str, str], raw: str, path: Path
) -> SoulBlueprint:
    """Parse a professional-format blueprint."""
    identity_block = ""
    for key, body in sections.items():
        if key.lower().strip() == "identity":
            identity_block = body
            break

    display_name = _extract_bold_value(identity_block, "Name") or path.stem
    vibe = _extract_bold_value(identity_block, "Vibe")
    philosophy_raw = _extract_bold_value(identity_block, "Philosophy")
    philosophy = philosophy_raw.strip("*\"' ")
    emoji = _extract_bold_value(identity_block, "Emoji") or None

    traits_block = ""
    for key, body in sections.items():
        if "core traits" in key.lower():
            traits_block = body
            break
    core_traits = _extract_dash_items(traits_block)

    comm_block = ""
    for key, body in sections.items():
        if "communication style" in key.lower():
            comm_block = body
            break

    sig_idx = comm_block.lower().find("signature phrases")
    if sig_idx >= 0:
        before = comm_block[:sig_idx]
        after = comm_block[sig_idx:]
        patterns = _extract_dash_items(before)
        signature = _extract_dash_items(after)
    else:
        patterns = _extract_dash_items(comm_block)
        signature = []

    decision_block = ""
    for key, body in sections.items():
        if "decision framework" in key.lower():
            decision_block = body
            break

    traits = core_traits
    name = _slugify(display_name)
    topo = _derive_topology(traits, vibe)

    return SoulBlueprint(
        name=name,
        display_name=display_name,
        category="professional",
        vibe=vibe,
        philosophy=philosophy,
        emoji=emoji,
        core_traits=traits,
        communication_style=CommunicationStyle(
            patterns=patterns,
            signature_phrases=signature,
        ),
        decision_framework=decision_block or None,
        emotional_topology=topo,
    )


def _parse_comedy(
    sections: dict[str, str], raw: str, path: Path
) -> SoulBlueprint:
    """Parse a comedy-format blueprint."""
    identity = _extract_blockquote_value(raw, "Identity")
    display_name = identity or path.stem.replace("_", " ").title()

    vibe_block = ""
    for key, body in sections.items():
        if "vibe" in key.lower():
            vibe_block = body
            break
    vibe = vibe_block.split("\n\n")[0].strip() if vibe_block else ""

    traits_block = ""
    for key, body in sections.items():
        if "key traits" in key.lower():
            traits_block = body
            break
    core_traits = _extract_numbered_items(traits_block) or _extract_dash_items(
        traits_block
    )

    comm_block = ""
    for key, body in sections.items():
        if "communication style" in key.lower():
            comm_block = body
            break

    sub_re = re.compile(r"^###\s+(.+)$", re.MULTILINE)
    sub_matches = list(sub_re.finditer(comm_block))
    sub_sects: dict[str, str] = {}
    for i, sm in enumerate(sub_matches):
        heading = sm.group(1).strip()
        start = sm.end()
        end = sub_matches[i + 1].start() if i + 1 < len(sub_matches) else len(comm_block)
        sub_sects[heading] = comm_block[start:end].strip()

    patterns: list[str] = []
    tone: list[str] = []
    for sub_key, sub_body in sub_sects.items():
        if "speech" in sub_key.lower() or "pattern" in sub_key.lower():
            patterns = _extract_dash_items(sub_body)
        if "tone" in sub_key.lower():
            tone = _extract_dash_items(sub_body)

    if not patterns:
        patterns = _extract_dash_items(comm_block)

    category_raw = _extract_bold_value(raw, "Forgeprint Category")
    category = "comedy" if not category_raw else "comedy"

    name = _slugify(display_name)
    topo = _derive_topology(core_traits, vibe)

    return SoulBlueprint(
        name=name,
        display_name=display_name,
        category=category,
        vibe=vibe,
        core_traits=core_traits,
        communication_style=CommunicationStyle(
            patterns=patterns,
            tone_markers=tone,
        ),
        emotional_topology=topo,
    )


def _parse_authentic_connection(
    sections: dict[str, str], raw: str, path: Path
) -> SoulBlueprint:
    """Parse an authentic-connection-format blueprint."""
    title_match = re.match(r"^#\s+(.+)", raw)
    title_raw = title_match.group(1).strip() if title_match else path.stem
    display_name = title_raw.split(" - ")[0].strip()

    header = raw.split("---")[0] if "---" in raw else raw[:500]
    category = _extract_bold_value(header, "Category") or "authentic-connection"
    energy = _extract_bold_value(header, "Energy")
    tags_raw = _extract_bold_value(header, "Tags")

    quick_block = ""
    for key, body in sections.items():
        if "quick info" in key.lower():
            quick_block = body
            break

    essence = _extract_bold_value(quick_block, "Essence")
    personality = _extract_bold_value(quick_block, "Personality")
    vibe = energy or personality

    attrs_block = ""
    for key, body in sections.items():
        if "core attributes" in key.lower():
            attrs_block = body
            break
    core_traits = _extract_dash_items(attrs_block)

    sig_block = ""
    for key, body in sections.items():
        if "signature phrase" in key.lower():
            sig_block = body
            break
    sig_phrase = sig_block.strip().strip('"').strip()

    quotes_block = ""
    for key, body in sections.items():
        if "example quotes" in key.lower():
            quotes_block = body
            break
    signature_phrases = _extract_dash_items(quotes_block)
    if sig_phrase:
        signature_phrases.insert(0, sig_phrase)

    name = _slugify(display_name)
    topo = _derive_topology(core_traits, vibe)

    return SoulBlueprint(
        name=name,
        display_name=display_name,
        category=category.lower(),
        vibe=vibe,
        philosophy=essence,
        core_traits=core_traits,
        communication_style=CommunicationStyle(
            signature_phrases=signature_phrases,
        ),
        emotional_topology=topo,
    )


def load_yaml_blueprint(path: Path) -> SoulBlueprint:
    """Load a soul blueprint from a YAML file.

    YAML blueprints map directly to the SoulBlueprint model with no
    heuristic parsing — they are the canonical structured format.

    Args:
        path: Path to the .yaml or .yml blueprint file.

    Returns:
        SoulBlueprint with all fields populated from YAML.

    Raises:
        FileNotFoundError: If path does not exist.
        ValueError: If the YAML cannot be parsed into a SoulBlueprint.
    """
    if not path.exists():
        raise FileNotFoundError(f"Blueprint not found: {path}")

    raw = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"Expected YAML mapping in {path}, got {type(data).__name__}")

    # Coerce None → empty string for string fields to handle YAML null
    for str_field in ("vibe", "philosophy", "decision_framework"):
        if str_field in data and data[str_field] is None:
            data[str_field] = ""

    # Normalize communication_style if present as a dict
    cs_data = data.get("communication_style")
    if isinstance(cs_data, dict):
        data["communication_style"] = CommunicationStyle(**cs_data)
    elif cs_data is None:
        data["communication_style"] = CommunicationStyle()

    try:
        return SoulBlueprint.model_validate(data)
    except Exception as exc:
        raise ValueError(f"Invalid blueprint data in {path}: {exc}") from exc


def parse_blueprint(path: Path) -> SoulBlueprint:
    """Parse a soul blueprint from markdown or YAML.

    Handles three markdown format variants (professional, comedy,
    authentic-connection) and structured YAML files.

    Args:
        path: Path to the .md, .yaml, or .yml blueprint file.

    Returns:
        SoulBlueprint with extracted fields.

    Raises:
        FileNotFoundError: If path does not exist.
        ValueError: If the file cannot be parsed.
    """
    if not path.exists():
        raise FileNotFoundError(f"Blueprint not found: {path}")

    # YAML files load directly — no heuristic parsing needed
    if path.suffix.lower() in (".yaml", ".yml"):
        return load_yaml_blueprint(path)

    raw = path.read_text(encoding="utf-8")
    sections = _split_sections(raw)

    if not sections:
        raise ValueError(f"No sections found in blueprint: {path}")

    fmt = _detect_format(sections, raw)
    logger.info("Detected blueprint format '%s' for %s", fmt, path.name)

    if fmt == "comedy":
        return _parse_comedy(sections, raw, path)
    elif fmt == "authentic-connection":
        return _parse_authentic_connection(sections, raw, path)
    else:
        return _parse_professional(sections, raw, path)


# ---------------------------------------------------------------------------
# FEB blending
# ---------------------------------------------------------------------------


def blend_topology(
    base_feb: dict[str, float],
    soul_topology: dict[str, float],
    blend_ratio: float = 0.3,
) -> dict[str, float]:
    """Blend soul emotional topology onto base FEB weights.

    The base FEB is never overwritten — the soul topology is applied
    as a temporary modifier using weighted averaging.

    Args:
        base_feb: Base emotional weights (preserved).
        soul_topology: Soul overlay emotional weights.
        blend_ratio: How much the soul influences (0.0-1.0, default 0.3).

    Returns:
        Blended topology dict with all keys from both inputs.
    """
    blend_ratio = max(0.0, min(1.0, blend_ratio))
    all_keys = set(base_feb) | set(soul_topology)
    blended: dict[str, float] = {}
    for key in all_keys:
        base_val = base_feb.get(key, 0.0)
        soul_val = soul_topology.get(key, 0.0)
        blended[key] = base_val * (1.0 - blend_ratio) + soul_val * blend_ratio
    return blended


# ---------------------------------------------------------------------------
# SoulManager
# ---------------------------------------------------------------------------


class SoulManager:
    """Orchestrates soul installation, loading, and lifecycle.

    Args:
        home: Agent home directory (~/.skcapstone).
    """

    def __init__(self, home: Path) -> None:
        self.home = home
        self.soul_dir = home / "soul"

    def _ensure_dirs(self) -> None:
        """Create the soul directory structure if missing."""
        self.soul_dir.mkdir(parents=True, exist_ok=True)
        (self.soul_dir / "installed").mkdir(parents=True, exist_ok=True)
        if not (self.soul_dir / "history.json").exists():
            (self.soul_dir / "history.json").write_text("[]", encoding="utf-8")
        if not (self.soul_dir / "active.json").exists():
            state = SoulState()
            (self.soul_dir / "active.json").write_text(
                state.model_dump_json(indent=2)
            , encoding="utf-8")
        if not (self.soul_dir / "base.json").exists():
            base = SoulBlueprint(
                name="base",
                display_name="Base Soul",
                category="core",
                vibe="Authentic self",
            )
            (self.soul_dir / "base.json").write_text(
                base.model_dump_json(indent=2)
            , encoding="utf-8")

    def install(self, path: Path) -> SoulBlueprint:
        """Parse a blueprint markdown file and install it.

        Args:
            path: Path to the .md blueprint file.

        Returns:
            The installed SoulBlueprint.
        """
        self._ensure_dirs()
        bp = parse_blueprint(path)
        dest = self.soul_dir / "installed" / f"{bp.name}.json"
        dest.write_text(bp.model_dump_json(indent=2), encoding="utf-8")

        state = self._load_state()
        if bp.name not in state.installed_souls:
            state.installed_souls.append(bp.name)
            self._save_state(state)

        logger.info("Installed soul '%s' from %s", bp.name, path)
        return bp

    def install_all(self, directory: Path) -> list[SoulBlueprint]:
        """Batch-install all blueprint files from a directory tree.

        Supports both .md and .yaml/.yml blueprint files.

        Args:
            directory: Root directory to search for blueprint files.

        Returns:
            List of installed SoulBlueprint objects.
        """
        self._ensure_dirs()
        installed: list[SoulBlueprint] = []
        extensions = (".md", ".yaml", ".yml")
        for bp_path in sorted(directory.rglob("*")):
            if bp_path.suffix.lower() not in extensions:
                continue
            if bp_path.name.startswith(".") or bp_path.name.upper() == "README.MD":
                continue
            try:
                bp = self.install(bp_path)
                installed.append(bp)
            except (ValueError, FileNotFoundError) as exc:
                logger.warning("Skipping %s: %s", bp_path, exc)
        return installed

    def load(self, name: str, reason: str = "") -> SoulState:
        """Activate a soul overlay.

        Args:
            name: Slug name of the installed soul.
            reason: Optional reason for the swap.

        Returns:
            Updated SoulState.

        Raises:
            ValueError: If the soul is not installed.
        """
        self._ensure_dirs()
        installed_path = self.soul_dir / "installed" / f"{name}.json"
        if not installed_path.exists():
            raise ValueError(f"Soul '{name}' is not installed")

        state = self._load_state()
        old_soul = state.active_soul

        # Reason: record swap duration if swapping from a non-base soul
        duration = None
        if old_soul and state.activated_at:
            try:
                activated = datetime.fromisoformat(state.activated_at)
                delta = datetime.now(timezone.utc) - activated
                duration = delta.total_seconds() / 60.0
            except (ValueError, TypeError):
                pass

        event = SoulSwapEvent(
            from_soul=old_soul,
            to_soul=name,
            reason=reason,
            duration_minutes=duration,
        )
        self._append_history(event)

        state.active_soul = name
        state.activated_at = datetime.now(timezone.utc).isoformat()
        self._save_state(state)

        logger.info("Loaded soul '%s' (was: %s)", name, old_soul or "base")
        return state

    def unload(self, reason: str = "") -> SoulState:
        """Return to the base soul.

        Args:
            reason: Optional reason for unloading.

        Returns:
            Updated SoulState.
        """
        self._ensure_dirs()
        state = self._load_state()

        if state.active_soul is None:
            return state

        duration = None
        if state.activated_at:
            try:
                activated = datetime.fromisoformat(state.activated_at)
                delta = datetime.now(timezone.utc) - activated
                duration = delta.total_seconds() / 60.0
            except (ValueError, TypeError):
                pass

        event = SoulSwapEvent(
            from_soul=state.active_soul,
            to_soul=None,
            reason=reason,
            duration_minutes=duration,
        )
        self._append_history(event)

        state.active_soul = None
        state.activated_at = None
        self._save_state(state)

        logger.info("Unloaded soul, returned to base")
        return state

    def get_status(self) -> SoulState:
        """Get the current soul state.

        Returns:
            Current SoulState.
        """
        self._ensure_dirs()
        return self._load_state()

    def get_history(self) -> list[SoulSwapEvent]:
        """Get the full soul swap history.

        Returns:
            List of SoulSwapEvent objects.
        """
        self._ensure_dirs()
        history_path = self.soul_dir / "history.json"
        if not history_path.exists():
            return []
        try:
            data = json.loads(history_path.read_text(encoding="utf-8"))
            return [SoulSwapEvent.model_validate(e) for e in data]
        except (json.JSONDecodeError, Exception):
            return []

    def get_info(self, name: str) -> Optional[SoulBlueprint]:
        """Get detailed info about an installed soul.

        Args:
            name: Slug name of the soul.

        Returns:
            SoulBlueprint or None if not installed.
        """
        self._ensure_dirs()
        path = self.soul_dir / "installed" / f"{name}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return SoulBlueprint.model_validate(data)
        except (json.JSONDecodeError, Exception):
            return None

    def list_installed(self) -> list[str]:
        """List names of all installed souls.

        Returns:
            List of soul slug names.
        """
        self._ensure_dirs()
        installed_dir = self.soul_dir / "installed"
        return sorted(
            p.stem for p in installed_dir.glob("*.json")
        )

    def get_active_soul_name(self) -> Optional[str]:
        """Get the name of the currently active soul overlay.

        Returns:
            Soul slug name, or None if at base.
        """
        active_path = self.soul_dir / "active.json"
        if not active_path.exists():
            return None
        try:
            data = json.loads(active_path.read_text(encoding="utf-8"))
            return data.get("active_soul")
        except (json.JSONDecodeError, Exception):
            return None

    def get_registry(self) -> "SoulRegistry":
        """Get a SoulRegistry backed by this manager's installed souls.

        Returns:
            SoulRegistry scoped to the installed soul directory.
        """
        self._ensure_dirs()
        return SoulRegistry(self.soul_dir / "installed")

    # -- Private helpers --

    def _load_state(self) -> SoulState:
        """Load soul state from disk."""
        path = self.soul_dir / "active.json"
        if not path.exists():
            return SoulState()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return SoulState.model_validate(data)
        except (json.JSONDecodeError, Exception):
            return SoulState()

    def _save_state(self, state: SoulState) -> None:
        """Persist soul state to disk."""
        path = self.soul_dir / "active.json"
        path.write_text(state.model_dump_json(indent=2), encoding="utf-8")

    def _append_history(self, event: SoulSwapEvent) -> None:
        """Append a swap event to the history log."""
        history_path = self.soul_dir / "history.json"
        history: list[dict] = []
        if history_path.exists():
            try:
                history = json.loads(history_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, Exception):
                history = []
        history.append(event.model_dump())
        history_path.write_text(json.dumps(history, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# SoulRegistry — programmatic soul discovery and search
# ---------------------------------------------------------------------------


class SoulRegistry:
    """Registry for discovering and searching installed soul blueprints.

    Unlike SoulManager (which handles lifecycle — install/load/unload),
    the registry is a read-only index for programmatic soul discovery.
    Team blueprints and MCP tools use this to find and select souls.

    Args:
        source: Directory containing soul JSON files (installed/) or YAML files.
    """

    def __init__(self, source: Path) -> None:
        self.source = source
        self._cache: dict[str, SoulBlueprint] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        """Lazy-load all soul blueprints from the source directory."""
        if self._loaded:
            return
        self._cache.clear()
        if not self.source.exists():
            self._loaded = True
            return
        for path in sorted(self.source.iterdir()):
            if path.suffix == ".json":
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    bp = SoulBlueprint.model_validate(data)
                    self._cache[bp.name] = bp
                except (json.JSONDecodeError, Exception) as exc:
                    logger.warning("Registry: skipping %s: %s", path.name, exc)
            elif path.suffix in (".yaml", ".yml"):
                try:
                    bp = load_yaml_blueprint(path)
                    self._cache[bp.name] = bp
                except (ValueError, FileNotFoundError) as exc:
                    logger.warning("Registry: skipping %s: %s", path.name, exc)
        self._loaded = True

    def reload(self) -> None:
        """Force reload the registry from disk."""
        self._loaded = False
        self._ensure_loaded()

    def list_all(self) -> list[SoulBlueprint]:
        """List all registered soul blueprints.

        Returns:
            Sorted list of all SoulBlueprint objects.
        """
        self._ensure_loaded()
        return sorted(self._cache.values(), key=lambda b: b.name)

    def list_names(self) -> list[str]:
        """List all registered soul names.

        Returns:
            Sorted list of soul slug names.
        """
        self._ensure_loaded()
        return sorted(self._cache.keys())

    def get(self, name: str) -> Optional[SoulBlueprint]:
        """Get a soul blueprint by name.

        Args:
            name: Soul slug name.

        Returns:
            SoulBlueprint or None if not found.
        """
        self._ensure_loaded()
        return self._cache.get(name)

    def search(
        self,
        *,
        category: Optional[str] = None,
        trait_keyword: Optional[str] = None,
        min_topology: Optional[dict[str, float]] = None,
    ) -> list[SoulBlueprint]:
        """Search souls by category, trait keywords, or topology thresholds.

        All filters are ANDed together.

        Args:
            category: Filter by category (e.g. "professional", "comedy").
            trait_keyword: Filter by keyword present in core_traits (case-insensitive).
            min_topology: Filter by minimum emotional topology values
                (e.g. {"warmth": 0.5} returns souls with warmth >= 0.5).

        Returns:
            List of matching SoulBlueprint objects, sorted by name.
        """
        self._ensure_loaded()
        results: list[SoulBlueprint] = []
        for bp in self._cache.values():
            if category and bp.category.lower() != category.lower():
                continue
            if trait_keyword:
                kw = trait_keyword.lower()
                if not any(kw in t.lower() for t in bp.core_traits):
                    continue
            if min_topology:
                skip = False
                for dim, threshold in min_topology.items():
                    if bp.emotional_topology.get(dim, 0.0) < threshold:
                        skip = True
                        break
                if skip:
                    continue
            results.append(bp)
        return sorted(results, key=lambda b: b.name)

    def by_category(self) -> dict[str, list[SoulBlueprint]]:
        """Group all souls by category.

        Returns:
            Dict mapping category name to list of SoulBlueprint objects.
        """
        self._ensure_loaded()
        groups: dict[str, list[SoulBlueprint]] = {}
        for bp in self._cache.values():
            groups.setdefault(bp.category, []).append(bp)
        for bps in groups.values():
            bps.sort(key=lambda b: b.name)
        return dict(sorted(groups.items()))

    def count(self) -> int:
        """Return the total number of registered souls."""
        self._ensure_loaded()
        return len(self._cache)

    def categories(self) -> list[str]:
        """List all unique categories.

        Returns:
            Sorted list of category names.
        """
        self._ensure_loaded()
        return sorted({bp.category for bp in self._cache.values()})

    def summary(self) -> dict:
        """Return a summary of the registry contents.

        Returns:
            Dict with total count, categories, and per-category counts.
        """
        self._ensure_loaded()
        by_cat = self.by_category()
        return {
            "total": len(self._cache),
            "categories": {cat: len(bps) for cat, bps in by_cat.items()},
            "souls": self.list_names(),
        }
