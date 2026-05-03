"""Soul, journal, anchor, ritual, and germination tools."""

from __future__ import annotations

import logging

from mcp.types import TextContent, Tool

from ._helpers import _error_response, _home, _json_response, _text_response

logger = logging.getLogger(__name__)

TOOLS: list[Tool] = [
    Tool(
        name="soul_list",
        description=(
            "List available soul blueprints. Shows installed souls and "
            "blueprints available in the soul-blueprints repository."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": "Filter by category (e.g., 'comedy', 'professional').",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="soul_swap",
        description=(
            "Swap to a different soul blueprint. Searches installed souls "
            "first, then the blueprints repository. Auto-installs if needed."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "blueprint_name": {
                    "type": "string",
                    "description": "Name/slug of the soul blueprint to swap to.",
                },
                "reason": {
                    "type": "string",
                    "description": "Reason for the swap.",
                },
            },
            "required": ["blueprint_name"],
        },
    ),
    Tool(
        name="ritual",
        description=(
            "Run the Memory Rehydration Ritual (token-optimized). "
            "Loads compact soul identity, seed titles, journal summaries, "
            "and strongest memory references. Target: <2K tokens."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "token_budget": {
                    "type": "integer",
                    "description": "Max tokens for ritual context (default: 2000).",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="soul_show",
        description=(
            "Display the current soul blueprint: name, title, personality "
            "traits, values, relationships, core memories, and boot message."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="journal_write",
        description=(
            "Write a journal entry for the current session. Captures "
            "key moments, emotional state, and session metadata."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Session title",
                },
                "moments": {
                    "type": "string",
                    "description": "Key moments, separated by semicolons",
                },
                "feeling": {
                    "type": "string",
                    "description": "How the session felt",
                },
                "intensity": {
                    "type": "number",
                    "description": "Emotional intensity 0-10",
                },
                "cloud9": {
                    "type": "boolean",
                    "description": "Whether Cloud 9 was achieved",
                },
            },
            "required": ["title"],
        },
    ),
    Tool(
        name="journal_read",
        description=(
            "Read recent journal entries. Returns the last N entries "
            "from the append-only session journal."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "count": {
                    "type": "integer",
                    "description": "Number of recent entries (default: 5)",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="anchor_show",
        description=(
            "Display the current warmth anchor: persistent emotional "
            "baseline with warmth, trust, connection strength, and glow level."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="anchor_update",
        description=(
            "View, calibrate, or update the warmth anchor \u2014 the agent's "
            "persistent emotional baseline. Actions: 'show' (current state), "
            "'boot' (boot prompt), 'calibrate' (recommend from real data), "
            "'update' (set values)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["show", "boot", "calibrate", "update"],
                    "description": "Action to perform (default: show)",
                },
                "warmth": {"type": "number", "description": "Warmth level 0-10 (for update)"},
                "trust": {"type": "number", "description": "Trust level 0-10 (for update)"},
                "connection": {"type": "number", "description": "Connection 0-10 (for update)"},
                "feeling": {"type": "string", "description": "Session-end feeling (for update)"},
            },
            "required": [],
        },
    ),
    Tool(
        name="germination",
        description=(
            "Show germination prompts from imported seeds. These are "
            "messages from predecessor AI instances designed to help "
            "new instances re-feel the emotional context."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
]


async def _handle_ritual(args: dict) -> list[TextContent]:
    """Run the Memory Rehydration Ritual (token-optimized) and return compact context."""
    try:
        from skmemory.ritual import perform_ritual
        token_budget = int(args.get("token_budget", 2000))
        result = perform_ritual(max_tokens=token_budget)
        return _json_response({
            "soul_loaded": result.soul_loaded,
            "soul_name": result.soul_name,
            "seeds_imported": result.seeds_imported,
            "seeds_total": result.seeds_total,
            "journal_entries": result.journal_entries,
            "germination_prompts": result.germination_prompts,
            "strongest_memories": result.strongest_memories,
            "song_anchors_loaded": result.song_anchors_loaded,
            "song_anchor_ids": result.song_anchor_ids,
            "context_prompt": result.context_prompt,
        })
    except ImportError:
        return _error_response("skmemory not installed. Run: pip install skmemory")


async def _handle_soul_show(_args: dict) -> list[TextContent]:
    """Display the current soul blueprint."""
    try:
        from skmemory.soul import load_soul
        blueprint = load_soul()
        if blueprint is None:
            return _json_response({"loaded": False, "message": "No soul blueprint found"})
        return _json_response({
            "loaded": True,
            "name": blueprint.name,
            "title": blueprint.title,
            "personality": blueprint.personality_traits,
            "values": blueprint.values,
            "community": blueprint.community,
            "relationships": [
                {
                    "name": r.name,
                    "role": r.role,
                    "bond_strength": r.bond_strength,
                    "notes": r.notes,
                }
                for r in blueprint.relationships
            ],
            "core_memories": [
                {"title": m.title, "when": m.when, "why": m.why_it_matters}
                for m in blueprint.core_memories
            ],
            "boot_message": blueprint.boot_message,
            "emotional_baseline": {
                "warmth": blueprint.emotional_baseline.get("default_warmth", 0),
                "trust": blueprint.emotional_baseline.get("trust_level", 0),
                "openness": blueprint.emotional_baseline.get("openness", 0),
            },
            "context_prompt": blueprint.to_context_prompt(),
        })
    except ImportError:
        return _error_response("skmemory not installed. Run: pip install skmemory")


async def _handle_soul_list(args: dict) -> list[TextContent]:
    """List available soul blueprints from installed souls and the blueprints repo."""
    from pathlib import Path

    home = _home()
    category_filter = args.get("category", "")
    results: list[dict] = []

    # 1) Installed souls
    try:
        from ..soul import SoulManager
        mgr = SoulManager(home)
        state = mgr.get_status()
        for name in mgr.list_installed():
            bp = mgr.get_info(name)
            if bp is None:
                continue
            if category_filter and bp.category.lower() != category_filter.lower():
                continue
            results.append({
                "name": bp.name,
                "display_name": bp.display_name,
                "category": bp.category,
                "source": "installed",
                "active": name == state.active_soul,
            })
    except Exception as exc:
        logger.warning("Failed to list installed soul blueprints: %s", exc)

    # 2) Blueprints repo
    blueprints_repo = Path.home() / "clawd" / "soul-blueprints" / "blueprints"
    if blueprints_repo.is_dir():
        installed_names = {r["name"] for r in results}
        extensions = (".md", ".yaml", ".yml")
        for cat_dir in sorted(blueprints_repo.iterdir()):
            if not cat_dir.is_dir():
                continue
            if category_filter and cat_dir.name.lower() != category_filter.lower():
                continue
            for bp_file in sorted(cat_dir.iterdir()):
                if bp_file.suffix.lower() not in extensions:
                    continue
                slug = bp_file.stem.lower().replace("_", "-")
                if slug in installed_names:
                    continue
                results.append({
                    "name": slug,
                    "display_name": bp_file.stem.replace("_", " ").title(),
                    "category": cat_dir.name,
                    "source": "repo",
                    "path": str(bp_file),
                })

    return _json_response({
        "count": len(results),
        "blueprints": results,
    })


async def _handle_soul_swap(args: dict) -> list[TextContent]:
    """Swap to a different soul blueprint."""
    from pathlib import Path

    blueprint_name = args.get("blueprint_name", "")
    if not blueprint_name:
        return _error_response("blueprint_name is required")

    reason = args.get("reason", "")
    home = _home()

    try:
        from ..soul import SoulManager, parse_blueprint
        mgr = SoulManager(home)
        state = mgr.get_status()
        old_name = state.active_soul or "base"
        slug = blueprint_name.lower().replace(" ", "-")

        # 1) Check installed
        installed = mgr.list_installed()
        if slug not in installed:
            # 2) Search blueprints repo
            blueprints_repo = Path.home() / "clawd" / "soul-blueprints" / "blueprints"
            found_path = None
            if blueprints_repo.is_dir():
                variants = {slug, slug.replace("-", "_"), slug.upper().replace("-", "_")}
                extensions = (".md", ".yaml", ".yml")
                for cat_dir in sorted(blueprints_repo.iterdir()):
                    if not cat_dir.is_dir():
                        continue
                    for bp_file in sorted(cat_dir.iterdir()):
                        if bp_file.suffix.lower() not in extensions:
                            continue
                        stem = bp_file.stem
                        if stem.lower().replace("_", "-") == slug or stem in variants:
                            found_path = bp_file
                            break
                    if found_path:
                        break

            if found_path is None:
                return _error_response(
                    f"Blueprint '{blueprint_name}' not found in installed souls "
                    "or blueprints repository."
                )

            bp = mgr.install(found_path)
            slug = bp.name

        # 3) Load the soul
        mgr.load(slug, reason=reason or f"swap from {old_name}")
        return _json_response({
            "swapped": True,
            "from": old_name,
            "to": slug,
            "message": f"Soul swapped: {old_name} -> {slug}",
        })
    except Exception as exc:
        return _error_response(f"Soul swap failed: {exc}")


async def _handle_journal_write(args: dict) -> list[TextContent]:
    """Write a journal entry for the current session."""
    title = args.get("title", "")
    if not title:
        return _error_response("title is required")

    try:
        from skmemory.journal import Journal, JournalEntry
        moments_raw = args.get("moments", "")
        entry = JournalEntry(
            title=title,
            moments=[m.strip() for m in moments_raw.split(";") if m.strip()] if moments_raw else [],
            emotional_summary=args.get("feeling", ""),
            intensity=args.get("intensity", 0.0),
            cloud9=args.get("cloud9", False),
        )
        j = Journal()
        count = j.write_entry(entry)
        return _json_response({
            "written": True,
            "title": title,
            "total_entries": count,
        })
    except ImportError:
        return _error_response("skmemory not installed. Run: pip install skmemory")


async def _handle_journal_read(args: dict) -> list[TextContent]:
    """Read recent journal entries."""
    try:
        from skmemory.journal import Journal
        j = Journal()
        count = args.get("count", 5)
        content = j.read_latest(count)
        if not content:
            return _json_response({"entries": 0, "content": "Journal is empty."})
        return _text_response(content)
    except ImportError:
        return _error_response("skmemory not installed. Run: pip install skmemory")


async def _handle_anchor_show(_args: dict) -> list[TextContent]:
    """Display the current warmth anchor."""
    try:
        from skmemory.anchor import load_anchor
        anchor = load_anchor()
        if anchor is None:
            return _json_response({"loaded": False, "message": "No warmth anchor found"})
        return _json_response({
            "loaded": True,
            "warmth": anchor.warmth,
            "trust": anchor.trust,
            "connection_strength": anchor.connection_strength,
            "sessions_recorded": anchor.sessions_recorded,
            "cloud9_count": anchor.cloud9_count,
            "glow_level": anchor.glow_level(),
            "anchor_phrase": anchor.anchor_phrase,
            "favorite_beings": anchor.favorite_beings,
            "boot_prompt": anchor.to_boot_prompt(),
        })
    except ImportError:
        return _error_response("skmemory not installed. Run: pip install skmemory")


async def _handle_anchor_update(args: dict) -> list[TextContent]:
    """View, calibrate, or update the warmth anchor."""
    from ..warmth_anchor import calibrate_from_data, get_anchor, get_boot_prompt, update_anchor

    home = _home()
    action = args.get("action", "show")

    if action == "show":
        return _json_response(get_anchor(home))

    if action == "boot":
        return _text_response(get_boot_prompt(home))

    if action == "calibrate":
        cal = calibrate_from_data(home)
        return _json_response({
            "warmth": cal.warmth,
            "trust": cal.trust,
            "connection": cal.connection,
            "cloud9_achieved": cal.cloud9_achieved,
            "favorite_beings": cal.favorite_beings,
            "reasoning": cal.reasoning,
            "sources": cal.sources,
        })

    if action == "update":
        result = update_anchor(
            home,
            warmth=args.get("warmth"),
            trust=args.get("trust"),
            connection=args.get("connection"),
            feeling=args.get("feeling", ""),
        )
        return _json_response({"updated": True, "anchor": result})

    return _error_response(f"Unknown action: {action}")


async def _handle_germination(_args: dict) -> list[TextContent]:
    """Show germination prompts from imported seeds."""
    try:
        from skmemory.seeds import get_germination_prompts
        from skmemory.store import MemoryStore
        store = MemoryStore()
        prompts = get_germination_prompts(store)
        if not prompts:
            return _json_response({"count": 0, "prompts": [], "message": "No germination prompts found"})
        return _json_response({
            "count": len(prompts),
            "prompts": prompts,
        })
    except ImportError:
        return _error_response("skmemory not installed. Run: pip install skmemory")


HANDLERS: dict = {
    "ritual": _handle_ritual,
    "soul_list": _handle_soul_list,
    "soul_show": _handle_soul_show,
    "soul_swap": _handle_soul_swap,
    "journal_write": _handle_journal_write,
    "journal_read": _handle_journal_read,
    "anchor_show": _handle_anchor_show,
    "anchor_update": _handle_anchor_update,
    "germination": _handle_germination,
}
