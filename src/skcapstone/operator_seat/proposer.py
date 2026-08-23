"""The operator's reasoning: turn a firing brief into proposed fixes.

Report-only friendly: propose() returns candidate fixes; it never applies them.
The brain is the intelligence (no rule table decides the fix), and the model call
is injectable so the loop is testable without a live model. The default caller
routes through skgateway, keeping the quiet path cheap (ornith) and the decision
path on a capable model.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

_PROMPT = (
    "You are the autonomous operator of a fleet. These conditions are FIRING (they "
    "need attention):\n{firing}\n\nThe ONLY actions you may propose are from this "
    "catalog (use the exact action name):\n{actions}\n\nPropose the minimal set of "
    "fixes as a JSON array. Each element is an object with keys: app and condition "
    "(copied exactly from one firing condition), action (from the catalog), object "
    "(the affected object name), change_class (standard, normal, or "
    "major), and rationale (one short sentence, no dashes). Propose nothing you are "
    "not confident about. If no action is warranted, return an empty array []. Reply "
    "with ONLY the JSON array."
)


def _extract_json_array(text: str) -> list:
    """Pull the first JSON array out of a model reply (tolerates fences/prose)."""
    if not text:
        return []
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def default_chat(prompt: str, *, base_url: str, model: str, timeout: float = 120.0) -> str:
    """Call skgateway's OpenAI-compatible chat endpoint and return the content."""
    import urllib.request

    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 4096,
            "temperature": 0,
        }
    ).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer sk-local"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read())
    return payload["choices"][0]["message"].get("content") or ""


def propose(
    brief: dict,
    explain: dict,
    *,
    chat: Callable[[str], str],
) -> list[dict[str, Any]]:
    """Reason over a firing brief and return proposed fixes (never applied here).

    A quiet brief proposes nothing without any model call. For a firing brief the
    injected `chat` callable is asked for proposals; each returned proposal is
    validated against the action catalog and dropped if it names an unknown action.
    """
    if brief.get("quiet"):
        return []
    catalog = {a["name"] for a in explain.get("actions", [])}
    firing_identities = {
        (c.get("app"), c.get("type"), c.get("object")) for c in brief.get("firing", [])
    }
    firing = (
        "\n".join(
            f"- {c.get('app')}: {c.get('object')} {c.get('type')}={c.get('status')}"
            for c in brief.get("firing", [])
        )
        or "- (none)"
    )
    actions = "\n".join(
        f"- {a['name']} (blast_radius={a['blast_radius']}, reversible={a['reversible']})"
        for a in explain.get("actions", [])
    )
    reply = chat(_PROMPT.format(firing=firing, actions=actions))
    out: list[dict] = []
    for item in _extract_json_array(reply):
        identity = (
            (item.get("app"), item.get("condition"), item.get("object"))
            if isinstance(item, dict)
            else None
        )
        if (
            isinstance(item, dict)
            and item.get("action") in catalog
            and identity in firing_identities
        ):
            out.append(
                {
                    "app": item["app"],
                    "condition": item["condition"],
                    "action": item["action"],
                    "object": item.get("object", ""),
                    "change_class": item.get("change_class", "normal"),
                    "rationale": str(item.get("rationale", "")),
                }
            )
    return out
