"""Dashboard assistant console: ad-hoc reports + cross-ticket instructions.

Phase 5. The operator types natural language ("top 5 incidents", "top 2
most-involved tasks", "add a note to inc-a1f and queue lumina to draft the
fix"). The server gathers a compact live snapshot (board + ITIL analytics),
streams an answer from skgateway, and can take a gated action (note / move /
assign / queue-ai) parsed from an ``ACTION {json}`` line the model emits.

Read/report answers are open on the tailnet; any mutation runs through the same
capability gate + audit trail (actor=assistant:<operator>) as the rest of the
dashboard.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("skcapstone.dashboard.assistant")

# Actions the assistant may request (subset of the card mutation + run tools).
_ACTIONS = {"note", "move", "assign", "queue-ai"}


# ---------------------------------------------------------------------------
# Analytics (canned reports the model narrates)
# ---------------------------------------------------------------------------

def board_summary(home: Path) -> dict:
    from .card import KanbanBoard

    kb = KanbanBoard(home)
    cards = kb.cards()
    from collections import Counter
    by_col = Counter(c.status.value for c in cards)
    by_lane = Counter(c.swimlane for c in cards)
    return {
        "active": len(cards),
        "by_column": dict(by_col),
        "by_lane": dict(by_lane),
        "wip": kb.wip_report(),
    }


def most_involved_tasks(home: Path, n: int = 5) -> list[dict]:
    """Cards ranked by activity (event count) - the busiest work items."""
    from .card_store import CardStore

    store = CardStore(home)
    scored = []
    for card in store.list_cards(include_archived=False):
        events = store._read_events(card.id)
        scored.append((len(events), card))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [{
        "id": c.id, "title": c.title, "kind": c.kind.value, "owner": c.owner,
        "status": c.status.value, "events": n_events,
    } for n_events, c in scored[:n]]


def top_incidents(home: Path, n: int = 5) -> list[dict]:
    from . import dashboard_itil as di

    incs = di.get_incidents(home)["incidents"]
    open_first = [i for i in incs if i["open"]] + [i for i in incs if not i["open"]]
    return open_first[:n]


def build_context(home: Path) -> str:
    """A compact live snapshot for the model to answer from."""
    from . import dashboard_itil as di

    bs = board_summary(home)
    ov = di.get_overview(home)
    parts = [
        "KANBAN: %d active cards; by column %s; by lane %s."
        % (bs["active"], bs["by_column"], bs["by_lane"]),
        "TOP INCIDENTS: " + json.dumps(top_incidents(home, 6)),
        "MOST-INVOLVED TASKS: " + json.dumps(most_involved_tasks(home, 6)),
        "ITIL KPIs: " + json.dumps(ov.get("kpis", {})),
        "SLA BREACH-RISK: " + json.dumps(ov.get("breach_risk", [])[:6]),
        "CAB QUEUE: " + json.dumps(ov.get("cab_queue", [])),
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

_SYSTEM = (
    "You are the SKDashboard assistant for a sovereign multi-agent ops team. "
    "Answer the operator's question using ONLY the LIVE SNAPSHOT provided. Be "
    "concise and concrete; prefer short lists with ids. If the operator asks you "
    "to change something (add a note, move/assign a card, or queue an AI agent to "
    "work a ticket), do NOT describe it, instead end your reply with a single line:\n"
    "ACTION {\"tool\": one of note|move|assign|queue-ai, \"card_id\": <id>, ...args}\n"
    "note args: text. move args: column. assign args: owner. queue-ai args: "
    "instruction, agent (lumina|opus|jarvis), mode (propose|dry-run|execute). "
    "Only emit ACTION when the operator clearly requested a change."
)


def stream_answer(home: Path, prompt: str, actor: str = "operator",
                  capability_ok: bool = False):
    """Generator yielding SSE frames: streamed answer, then an action result.

    Yields already-formatted ``event:/data:`` SSE strings.
    """
    from . import skgateway_client as gw

    context = build_context(home)
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": f"LIVE SNAPSHOT:\n{context}\n\nOPERATOR: {prompt}"},
    ]
    collected = []
    got_any = False
    for tok in gw.chat_stream(messages, max_tokens=1400, temperature=0.3, timeout=90):
        got_any = True
        collected.append(tok)
        yield _sse("token", {"text": tok})
    full = "".join(collected)
    if not got_any:
        yield _sse("token", {"text": "(assistant is unavailable right now)"})
    # Parse a trailing ACTION line, if any.
    action = _parse_action(full)
    if action:
        result = _run_action(home, action, actor, capability_ok)
        yield _sse("action", result)
    yield _sse("done", {})


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _parse_action(text: str) -> dict | None:
    import re

    m = re.search(r"ACTION\s*(\{.*\})", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(1))
    except ValueError:
        return None
    if obj.get("tool") in _ACTIONS and obj.get("card_id"):
        return obj
    return None


def _run_action(home: Path, action: dict, actor: str, capability_ok: bool) -> dict:
    """Execute a gated mutation the assistant requested."""
    tool = action["tool"]
    card_id = action["card_id"]
    who = f"assistant:{actor}"
    if not capability_ok:
        return {"tool": tool, "card_id": card_id, "ok": False,
                "error": "capability required for assistant actions"}
    try:
        if tool == "queue-ai":
            from . import agent_run as ar
            r = ar.request_run(home, card_id, action.get("instruction", ""),
                               agent=action.get("agent", "lumina"),
                               mode=action.get("mode", "propose"), requester=who)
            return {"tool": tool, "card_id": card_id, **r}
        from . import dashboard_kanban as dk
        fields = {k: v for k, v in action.items() if k not in ("tool", "card_id")}
        r = dk.apply_mutation(home, card_id, tool, who, **fields)
        return {"tool": tool, "card_id": card_id, **r}
    except Exception as exc:  # noqa: BLE001
        return {"tool": tool, "card_id": card_id, "ok": False, "error": str(exc)}
