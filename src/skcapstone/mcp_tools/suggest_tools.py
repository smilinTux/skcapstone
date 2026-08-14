"""AI next-step suggestion + queuing tools for any fleet surface (coord/gtd/itil).

Card P2.4 of the SKWorld fleet suggestion engine. Lets any agent ask "what
should I do next" on ANY fleet surface (a coord kanban card, a GTD
next-action, or an ITIL incident/problem/change) and queue an instruction on
it, all through one lightweight ItemRef (``surface``, ``id``) instead of
surface-specific tools. All business logic is delegated to
:mod:`skcapstone.agent_run`, which already knows how to lazily materialize
shadow cards (``gtd-<id>``, ``inc-/prb-/chg-<id>``) via ``ensure_card``.
"""

from __future__ import annotations

from mcp.types import TextContent, Tool

from .. import agent_run
from ._helpers import _error_response, _json_response, _shared_root

# Surfaces this resolver understands. "coord" cards use their raw id as the
# card_id; "gtd" next-actions are shadow-materialized under a "gtd-" prefix;
# "itil" records already carry their own inc-/prb-/chg- prefix.
_SURFACES = {"coord", "gtd", "itil"}


def _resolve_card_id(surface: str, item_id: str) -> str | None:
    """Resolve a (surface, id) ItemRef to a CardStore card_id.

    NOTE: DRY against skdashboard.surface_registry.resolve_card_id at
    integration (P2.2); this is a small standalone copy so this module carries
    no hard dependency on skdashboard.

    Args:
        surface: One of "coord", "gtd", "itil".
        item_id: The item's id on that surface.

    Returns:
        The resolved card_id, or ``None`` when the surface is unknown or the
        id is blank.
    """
    surface = (surface or "").strip().lower()
    item_id = (item_id or "").strip()
    if not item_id or surface not in _SURFACES:
        return None
    if surface == "gtd":
        return item_id if item_id.startswith("gtd-") else f"gtd-{item_id}"
    # coord: raw card id, used as-is. itil: already inc-/prb-/chg-<id>.
    return item_id


# ═══════════════════════════════════════════════════════════
# Tool Definitions
# ═══════════════════════════════════════════════════════════

TOOLS: list[Tool] = [
    Tool(
        name="suggest_item",
        description=(
            "Get AI next-step suggestions for any fleet item (coord card, GTD "
            "next-action, or ITIL record). Returns a short list of {text, mode} "
            "instructions an agent could queue next, tailored to the item when an "
            "LLM is available and always falling back to instant heuristics."
        ),
        inputSchema={
            "properties": {
                "surface": {
                    "description": "Fleet surface the item lives on",
                    "enum": ["coord", "gtd", "itil"],
                    "type": "string",
                },
                "id": {"description": "The item's id on that surface", "type": "string"},
                "llm": {
                    "description": "Use the LLM for tailored suggestions (default: true)",
                    "type": "boolean",
                },
            },
            "required": ["surface", "id"],
            "type": "object",
        },
    ),
    Tool(
        name="queue_item",
        description=(
            "Queue an AI next-step instruction on any fleet item (coord card, GTD "
            "next-action, or ITIL record). Attaches an AgentRun to the resolved "
            "card for a runner to pick up under the usual safety gate."
        ),
        inputSchema={
            "properties": {
                "surface": {
                    "description": "Fleet surface the item lives on",
                    "enum": ["coord", "gtd", "itil"],
                    "type": "string",
                },
                "id": {"description": "The item's id on that surface", "type": "string"},
                "instruction": {
                    "description": "The instruction for the agent to carry out",
                    "type": "string",
                },
                "mode": {
                    "description": (
                        "Execution mode (default: propose). Execute-tier is "
                        "deliberately absent: this surface cannot verify the "
                        "agentrun.execute capability, so it is refused at the "
                        "handler too."
                    ),
                    "enum": ["propose", "dry-run"],
                    "type": "string",
                },
                "agent": {
                    "description": "Agent to run the instruction (default: lumina)",
                    "type": "string",
                },
            },
            "required": ["surface", "id", "instruction"],
            "type": "object",
        },
    ),
]


# ═══════════════════════════════════════════════════════════
# Handlers
# ═══════════════════════════════════════════════════════════


async def _handle_suggest_item(args: dict) -> list[TextContent]:
    """Resolve the ItemRef and return AI next-step suggestions for it."""
    card_id = _resolve_card_id(args.get("surface", ""), args.get("id", ""))
    if card_id is None:
        return _error_response(f"unknown surface '{args.get('surface', '')}' or blank id")

    use_llm = args.get("llm", True)
    if not isinstance(use_llm, bool):
        use_llm = True

    result = agent_run.suggest_next_steps(_shared_root(), card_id, use_llm=use_llm)
    return _json_response(result)


async def _handle_queue_item(args: dict) -> list[TextContent]:
    """Resolve the ItemRef and queue an AgentRun instruction on it."""
    card_id = _resolve_card_id(args.get("surface", ""), args.get("id", ""))
    if card_id is None:
        return _error_response(f"unknown surface '{args.get('surface', '')}' or blank id")

    instruction = (args.get("instruction") or "").strip()
    if not instruction:
        return _error_response("instruction is required")

    mode = args.get("mode") or "propose"
    agent = args.get("agent") or "lumina"

    # SECURITY: this MCP tool performs NO per-request capability verification --
    # there is no request context here to carry an X-SK-Capability token, so
    # nothing proves the caller holds "agentrun.execute" (VERIFIED tier). The
    # `mode` argument arrives verbatim from a model tool-call, which is shaped
    # by item text the operator did not author (prompt-injection surface).
    # Same reasoning as the assistant-surface fix in dashboard_assistant.py:
    # never let untrusted text select a higher-privilege capability than was
    # actually verified. Execute-tier runs must use the gated HTTP route
    # (/api/queue/... -> _queue_gate -> queue_authz.authorize_queue).
    if mode == "execute":
        return _error_response(
            "execute-tier queueing is not authorized via the MCP surface; "
            "use the gated queue route"
        )

    # Attribute consent to the real calling agent rather than a blanket
    # "operator". The requester is written into the append-only
    # `agent_run_request` event, so a hardcoded value made every MCP-originated
    # run indistinguishable from a human operator action in the audit trail.
    # Degrades to "unattributed", matching the SPE convention in
    # operator_seat/fleet_adapter.py. Deliberately NOT a synthesized value like
    # "mcp:<agent>": an identity claim capauth could not resolve asserts
    # something it cannot back, which is the same defect as the hardcoded
    # "operator" this replaces. Never raises; attribution is best-effort.
    try:
        from capauth import resolve_agent_identity

        requester = resolve_agent_identity().capauth_uri or "unattributed"
    except Exception:  # noqa: BLE001
        requester = "unattributed"

    result = agent_run.request_run(
        _shared_root(),
        card_id,
        instruction,
        agent=agent,
        mode=mode,
        requester=requester,
    )
    return _json_response(result)


HANDLERS: dict = {
    "suggest_item": _handle_suggest_item,
    "queue_item": _handle_queue_item,
}
