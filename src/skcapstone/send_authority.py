"""Send authority (P4, card c6a87139): the ONLY code path able to actually
send a comms draft.

Copies the Change Management two-executor split exactly
(docs/specs/2026-08-13-change-management-cab-ai-arch.md;
`skharness.autocode.direct.DirectExecutor` / `EngineeringExecutor`):
preparing (`comms_executor.CommsExecutor`) cannot send, structurally. A
wholly separate, explicitly armed authority is the only thing that can, and
it refuses a self-approved send the same way the ITIL CAB fold excludes a
vote cast by the drafter (`skcoord.itil._fold_change`'s
``v.agent != prepared_by`` guard, at src/skcoord/itil.py around line 1245).

Fail-closed by default, the same seam shape as
``agent_run.set_execute_dispatcher`` and
``change_deploy.set_deploy_dispatcher``: no real transport (skcomms, skchat,
SMTP, whatever) is wired here. Wiring one is deliberately future work; this
card ships the structural boundary a transport will sit behind, so "no
transport wired yet", "not armed", and "self-approval" are three instances
of the SAME refusal (``SendAuthorityError``), not three different code paths
an integrator has to keep straight.

Chef's rule ("outbound is draft by default, never auto-send without a
per-item go") is enforced here as CODE, not convention: there is no default
constructor argument, no env var, and no call sequence that produces a send
without an explicit ``armed=True`` AND an ``armed_by`` identity AND that
identity differing from whoever prepared the draft AND a transport actually
wired. Remove any one of the four and ``send`` raises instead of sending.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger("skcapstone.send_authority")

_send_dispatcher: Optional[Callable[[dict], dict]] = None


def set_send_dispatcher(fn: Optional[Callable[[dict], dict]]) -> None:
    """Wire (or clear, with ``None``) the real transport.

    Default ``None`` keeps every send fail-closed regardless of arm/approval
    state: `SendAuthority.send` checks the arm and no-self-approval
    conditions BEFORE it ever looks at whether a transport is wired, so a
    transport being wired never becomes the thing standing between an
    unarmed or self-approved call and an actual send.
    """
    global _send_dispatcher
    _send_dispatcher = fn


def send_dispatch_available() -> bool:
    """True when a real transport has been wired."""
    return _send_dispatcher is not None


class SendAuthorityError(RuntimeError):
    """A send was refused by ``SendAuthority`` itself, before any transport
    was touched. Distinct from a transport-level failure (a real network
    error raised by the wired dispatcher), which propagates as-is so it is
    never confused with a structural refusal."""


class SendAuthority:
    """The only object permitted to invoke the wired send dispatcher.

    Armed explicitly, per instance, by construction: there is no default
    that produces ``armed=True``, so an authority built without saying so
    out loud can never send (arm check). ``send()`` additionally refuses
    when the armer is the same identity that prepared the draft
    (no-self-approval), mirroring the ITIL CAB `agent != prepared_by` guard.
    """

    def __init__(self, *, armed: bool, armed_by: str) -> None:
        self.armed = bool(armed)
        self.armed_by = (armed_by or "").strip()

    def send(self, draft: dict[str, Any], *, prepared_by: Optional[str] = None) -> dict[str, Any]:
        """Send ``draft`` (typically ``CommsExecutor``'s ``links["draft"]``).

        Args:
            draft: the drafted artifact to send.
            prepared_by: identity that prepared the draft. Defaults to
                ``draft.get("prepared_by")`` (the field
                ``CommsExecutor._build_draft`` stamps) when not given
                explicitly, so a caller need not thread it through by hand.

        Returns:
            Whatever the wired transport returns.

        Raises:
            SendAuthorityError: not armed, no armer identity, the armer
                equals the preparer (no-self-approval), or no transport is
                wired. Checked in that order, so the first violated
                invariant is always the one reported.
        """
        if not self.armed:
            raise SendAuthorityError("send refused: this authority was not armed")
        if not self.armed_by:
            raise SendAuthorityError("send refused: no armer identity given (armed_by required)")
        resolved_preparer = prepared_by if prepared_by is not None else draft.get("prepared_by")
        resolved_preparer = (resolved_preparer or "").strip()
        if resolved_preparer and resolved_preparer == self.armed_by:
            raise SendAuthorityError(
                f"send refused: no-self-approval ({self.armed_by!r} prepared and "
                "armed the same draft)"
            )
        if _send_dispatcher is None:
            raise SendAuthorityError("send refused: no transport wired (fail-closed default)")
        logger.info(
            "send_authority: sending draft %r armed_by=%r prepared_by=%r",
            draft.get("subject"),
            self.armed_by,
            resolved_preparer,
        )
        return _send_dispatcher(dict(draft))
