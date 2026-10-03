"""Actuation gating for the trustee lifecycle verbs (restart / scale / rotate).

Card e51a3e7e (SKW-AUTONOMY-E4, second half). Direct grep before this module
existed: `mcp_tools/trustee_tools.py`, backed by `trustee_ops.py`, exposed
`trustee_restart`, `trustee_scale`, and `trustee_rotate` to any agent session
holding the MCP surface with no freeze check, no capauth check, and no ITIL
requirement anywhere in either file. The only safeguard was
`TrusteeOps._audit`, which records an action AFTER it happens. An audit trail
is a record, not a gate; `_audit` is untouched by this module and stays
exactly that: a record, not a gate.

This module is the one guard `trustee_ops.py`'s three mutating methods share,
per AUTONOMY_ARCHITECTURE.md section 3.5(d):

    refuse when not actuation-ready or frozen (`fleet.store.check_actuation_gate`,
    landed in PR #198 / card 3925d012 -- imported and called here, never
    re-derived); require a capauth PDP allow when enforcement is reachable,
    failing closed when it is not, following `fleet.operator_http.authorize`'s
    pattern (that module's lines ~181-246) rather than inventing a second one;
    and `trustee_rotate`, being a credential-adjacent operation, additionally
    requires an approved ITIL change id, because rotation is never routine.

Why `authorize()` here is a new function and not a call into
`fleet.operator_http.authorize`: that function hardcodes its own
`_operator_rules()` table (operator.observe / operator.act / operator.estate.read),
and `decide()` fails closed on an unknown capability, so calling it with a
`trustee.*` capability would always deny for the wrong reason. Section 3.5(d)
asks this module to follow operator_http's PATTERN (try the import, fail
closed the moment it cannot be reached, never let an exception escape to the
caller), not to reuse its rule table, which belongs to a different domain
(the operator HTTP plane, not trustee deployments).

capauth enforcement here has no separate on/off toggle the way
`SKOPERATOR_HTTP` gates the operator HTTP surface. There is no equivalent
"is this surface running at all" question for trustee tools -- they are
MCP tools, always reachable the moment the MCP server is up, which is
exactly the ungoverned condition this card exists to close. So "enforcement
is available" here means exactly what `authorize()` below tests directly:
capauth is importable and `decide()` answers without raising. When capauth
is not installed in a given environment, `authorize()` denies with reason
"capauth unavailable: ...", which means every trustee lifecycle verb refuses
by default in that environment until capauth is deployed and the caller
holds a verified, signed grant. That is the correct default: an actuation
surface with no reachable PDP has no way to tell an authorized caller from
an unauthorized one, and the fail-closed answer to "I cannot tell" is deny.

The ITIL "is this change approved" check (`change_is_approved`) is
deliberately the narrowest possible read: fold one change record, compare
its status. `operator_seat/dispatch.py` (card cf12b21d, built alongside this
one) is constructing the general "is this change id approved" verifier the
whole actuation contract will eventually share (fold provenance,
`catalog_generation` re-classification against the live catalog, escalation
on staleness). That module does not exist yet in this branch's history, so
there is nothing here to import or duplicate; this function should be
replaced by the dispatcher's shared verifier the moment it lands.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .fleet import store
from .fleet.paths import FleetPaths

logger = logging.getLogger(__name__)

# ── capauth capabilities (trustee lifecycle) ─────────────────────────────

CAP_RESTART = "trustee.restart"
CAP_SCALE = "trustee.scale"
CAP_ROTATE = "trustee.rotate"

# ── refusal reasons ───────────────────────────────────────────────────────
# `frozen` / `unprovisioned` are `fleet.store`'s own spellings (the coord
# card's acceptance criteria use these exact strings); re-exported here so a
# caller never has to import both modules to compare a reason.
REASON_FROZEN = store.REASON_FROZEN
REASON_UNPROVISIONED = store.REASON_UNPROVISIONED
REASON_CAPABILITY_DENIED = "capability_denied"
REASON_CHANGE_NOT_APPROVED = "change_not_approved"


class ActuationRefusedError(Exception):
    """A trustee lifecycle verb was refused by the gate.

    `reason` is always one of the `REASON_*` constants in this module, so a
    caller (an MCP handler, the CLI, a test) can branch on it without parsing
    `str(exc)`. `detail` (also surfaced via `str(exc)`) carries the
    human-readable why.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail or reason
        super().__init__(self.detail)


@dataclass(frozen=True)
class GuardResult:
    """The verdict of `guard()`: allowed, or refused with a machine-readable why."""

    allowed: bool
    reason: Optional[str] = None
    detail: str = ""


def _trustee_rules() -> dict[str, Any]:
    """Capability rules for the three trustee lifecycle verbs.

    Not in capauth's own `DEFAULT_RULES` table (like `fleet.operator_http`'s
    `_operator_rules`, `decide()` accepts a caller-supplied `rules=` override
    for exactly this case). All three sit at the `VERIFIED` enrollment tier,
    the same floor every other actuation-class capability capauth ships
    already uses (`operator.act`, `skchat.send`, `skgateway.admin`,
    `agentrun.execute`): restart/scale/rotate all have a physical effect on a
    deployed agent, never merely read one.
    """
    from capauth import CapabilityRule
    from capauth.pairing import EnrollmentMode

    description = "Trustee lifecycle actuation on a deployed agent (physical effect)."
    return {
        cap: CapabilityRule(
            capability=cap,
            required_capability=cap,
            minimum_mode=EnrollmentMode.VERIFIED,
            description=description,
        )
        for cap in (CAP_RESTART, CAP_SCALE, CAP_ROTATE)
    }


def authorize(
    subject: str,
    capability: str,
    *,
    decide_fn: Optional[Callable[..., Any]] = None,
    base_dir: Optional[Path] = None,
) -> tuple[bool, str]:
    """Ask capauth's PDP whether `subject` may exercise `capability`.

    Fails closed: an unavailable capauth import denies with a clear reason
    rather than raising past the caller, mirroring
    `fleet.operator_http.authorize` and `fleet.signing`'s lazy-capauth
    posture. Deliberately does NOT wrap the `decide_fn` call itself in a
    try/except (operator_http's `authorize` does not either): a `decide()`
    that raises is a bug in the PDP or its storage, not a normal "PDP
    unreachable" uncertainty, and should surface rather than be silently
    swallowed into a deny that looks identical to every other deny.

    Returns:
        `(allow, reason)`. `reason` is always a human sentence from
        `capauth.authz.decide` (or the import-failure message).
    """
    try:
        from capauth import decide
    except Exception as exc:  # noqa: BLE001 - capauth unreachable denies, never raises
        return False, f"capauth unavailable: {exc}"
    decide_fn = decide_fn or decide
    decision = decide_fn(subject, capability, base_dir=base_dir, rules=_trustee_rules())
    return bool(decision.allow), str(decision.reason)


def resolve_subject() -> Optional[str]:
    """The calling process's capauth fingerprint, or None if unresolvable.

    Mirrors `mcp_tools.itil_tools._resolve_authenticated_subject`'s
    try/except-and-return-None shape (same canonical resolver,
    `capauth.resolve_agent_identity`, same "never raise past the caller"
    contract). Returns the PGP fingerprint rather than the short agent name
    because that is the shape `fleet.operator_http.authorize`'s own
    `fingerprint` parameter already expects, and because `decide()`'s
    device/token lookups are keyed off enrolled-device identity.
    """
    try:
        from capauth import resolve_agent_identity

        ident = resolve_agent_identity()
        return ident.fingerprint or None
    except Exception:  # noqa: BLE001 - identity resolution failure must never crash a call
        return None


def change_is_approved(change_id: str, *, shared_root: Path) -> bool:
    """Narrow check: does `change_id` currently fold to APPROVED status?

    See the module docstring: this is the narrowest possible refusal for
    `trustee_rotate` alone, standing in until `operator_seat/dispatch.py`
    (card cf12b21d) ships the general approved-change verifier every
    actuation surface should eventually share.
    """
    if not change_id:
        return False
    from .itil import Change, ITILManager  # skcapstone.itil is a re-export of skcoord.itil

    mgr = ITILManager(shared_root)
    try:
        rid = mgr._resolve_id(mgr.changes_dir, change_id)
        if mgr._load_core(mgr.changes_dir, rid) is None:
            return False
        change = mgr._fold_record(mgr.changes_dir, rid, Change)
    except Exception:  # noqa: BLE001 - an unreadable change record is not an approved one
        logger.warning("change_is_approved: could not fold change %r", change_id, exc_info=True)
        return False
    return change.status.value == "approved"


def guard(
    capability: str,
    *,
    paths: FleetPaths,
    subject: Optional[str],
    shared_root: Path,
    require_approved_change: bool = False,
    change_id: Optional[str] = None,
    decide_fn: Optional[Callable[..., Any]] = None,
    base_dir: Optional[Path] = None,
) -> GuardResult:
    """The one guard `trustee_ops.py`'s restart/scale/rotate all call.

    Order matches AUTONOMY_ARCHITECTURE.md section 3.6 ("freeze wins,
    always, and first"): actuation-readiness/freeze, then the capauth PDP
    allow, then -- only when `require_approved_change` is True, i.e. only
    for `trustee_rotate` -- the approved-change check. `require_approved_change`
    is a separate flag from `change_id` (rather than "a change id was
    supplied" implying the check) so a caller that supplies no change id at
    all is refused with `change_not_approved`, exactly like one that
    supplies an unapproved one, instead of silently skipping the check.
    """
    gate = store.check_actuation_gate(paths)
    if not gate.allowed:
        return GuardResult(False, gate.reason, gate.reason or "")

    if subject is None:
        return GuardResult(
            False, REASON_CAPABILITY_DENIED, "no authenticated subject could be resolved"
        )

    allow, reason = authorize(subject, capability, decide_fn=decide_fn, base_dir=base_dir)
    if not allow:
        return GuardResult(False, REASON_CAPABILITY_DENIED, reason)

    if require_approved_change:
        if not change_id or not change_is_approved(change_id, shared_root=shared_root):
            return GuardResult(
                False,
                REASON_CHANGE_NOT_APPROVED,
                f"change {change_id!r} is not approved",
            )

    return GuardResult(True)
