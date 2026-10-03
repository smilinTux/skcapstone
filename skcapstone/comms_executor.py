"""Comms executor (P4, card c6a87139): the ops/comms leg of the execute mux.

Structurally draft-only, copying the shape of
`skharness.autocode.direct.DirectExecutor`: that class inherits all of
`EngineeringExecutor`'s plumbing but overrides `_merge` to raise, so a merge
is unreachable BY CONSTRUCTION, not by a runtime check a caller could skip.
`CommsExecutor` does the same thing for the send boundary: it can draft, and
its `send` method exists ONLY to raise. There is no other path to a
transport anywhere in this module - it does not import a mail client, a
skchat/skcomms sender, or any other network-capable object, so even before
`send` is reached there is nothing here able to perform an outbound side
effect.

The one thing that CAN send is `send_authority.SendAuthority`, a wholly
separate class in a separate module this file never imports. That separation
is deliberate and structural: the drafting code path literally cannot reach
the sending code path by construction, matching the Change Management
two-executor split (prepare vs. armed deploy authority) exactly.

Contract: ``fn(context) -> {"summary", "activity", "links"}``, the same
shape `agent_run.claude_dispatcher` and the skharness code bridge use, so
`execute_mux.build_execute_mux` can route to either interchangeably. Never
raises out of `__call__`: a raise moves the card straight to FAILED (noisy,
per the card's own acceptance criteria), so every failure path here is
caught and returned as an ordinary (non-exception) result instead, landing
the card in NEEDS_REVIEW like any other completed draft attempt.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CommsExecutor:
    """Draft-only executor for ops/comms card work.

    Wired unconditionally by ``agent_run._maybe_wire_execute_mux`` (no env
    flag gates it, unlike the code bridge's ``SKAI_EXECUTE_BRIDGE``): unlike
    a real repo mutation, drafting is exactly the safe, desired default
    behavior Chef's "outbound is draft by default" rule calls for. The
    safety boundary this card adds is not an extra flag, it is that this
    class is structurally unable to send (see ``send`` below).
    """

    def __call__(self, context: dict) -> dict:
        try:
            draft = self._build_draft(context)
        except Exception as exc:  # noqa: BLE001 - a raise here must never fail the run (R1)
            return {
                "summary": f"comms draft failed: {exc}",
                "activity": [{"atype": "error", "text": str(exc)}],
                "links": {},
            }
        return {
            "summary": f"drafted (not sent): {draft['subject']}",
            "activity": [
                {
                    "atype": "action",
                    "text": "prepared a draft for review; nothing was sent",
                }
            ],
            "links": {"draft": draft},
        }

    def _build_draft(self, context: dict) -> dict[str, Any]:
        """Build the draft artifact. Raises only on a genuinely malformed
        context (e.g. non-dict); ``__call__`` is the sole boundary that
        turns any such raise into an ordinary result."""
        instruction = str(context.get("instruction") or "").strip()
        title = str(context.get("title") or "").strip()
        prepared_by = str(context.get("agent") or "").strip() or "unattributed"
        return {
            "status": "draft",
            "subject": title or "(untitled)",
            "body": instruction or "(no instruction given)",
            "card_id": context.get("card_id"),
            "prepared_by": prepared_by,
            "prepared_at": _now_iso(),
        }

    def send(self, *_args: Any, **_kwargs: Any) -> None:
        """HARD GUARDRAIL: this object drafts, it never sends. Present (like
        ``DirectExecutor._merge``) so even a caller that mistakenly reaches
        for ``.send()`` on the drafting object gets a loud, structural
        refusal instead of a silent no-op or, worse, an actual send."""
        raise RuntimeError(
            "CommsExecutor must never send: comms work is draft-only by "
            "construction (Chef's standing outbound-is-draft-by-default "
            "rule, P4/c6a87139). Use send_authority.SendAuthority instead, "
            "armed explicitly by an identity that did not prepare this draft."
        )
