"""Authorization for the privileged "queue AI to work an item" action.

Replaces the shared ``SKAI_QUEUE_TOKEN`` header check (loopback-open when
unset) with a capauth PDP decision, staged behind a migration flag
(``SKAI_AUTHZ``) so the rollout can move token -> pdp -> both without a
flag-day cutover.

Fail-closed by construction: token mode denies when no secret is configured,
pdp mode denies on any PDP error, and both mode requires every enabled check
to pass. Nothing in this module performs network or filesystem I/O at import
time; the real capauth ``decide`` is imported lazily inside the default
``decide_fn`` so tests never need a live PDP or a real ``~/.skcapstone``
registry.
"""

from __future__ import annotations

import hmac
import os
from typing import Any, Callable, Optional

#: Valid values for the ``SKAI_AUTHZ`` migration flag.
_VALID_MODES = ("token", "pdp", "both")

#: Default migration mode when ``SKAI_AUTHZ`` is unset.
_DEFAULT_AUTHZ_MODE = "token"

#: Environment variable naming the shared secret checked in "token" mode.
_TOKEN_ENV_VAR = "SKAI_QUEUE_TOKEN"

#: Environment variable selecting the migration mode ("token" | "pdp" | "both").
_AUTHZ_ENV_VAR = "SKAI_AUTHZ"

DecideFn = Callable[..., Any]


def _authz_mode() -> str:
    """Resolve the active migration mode from the ``SKAI_AUTHZ`` env var.

    Returns:
        str: One of ``"token"``, ``"pdp"``, or ``"both"``. Falls back to
        ``"token"`` (the current, pre-migration behavior) when the env var is
        unset or holds an unrecognized value, so a typo never silently
        widens or narrows access.
    """
    raw = os.environ.get(_AUTHZ_ENV_VAR, _DEFAULT_AUTHZ_MODE).strip().lower()
    if raw not in _VALID_MODES:
        return _DEFAULT_AUTHZ_MODE
    return raw


def capability_for(mode: str) -> str:
    """Map a queue-request ``mode`` to the capauth capability it requires.

    Args:
        mode: The requested run mode (e.g. ``"execute"``, ``"propose"``,
            ``"dry-run"``).

    Returns:
        str: ``"agentrun.execute"`` when ``mode == "execute"``; otherwise
        ``"agentrun.queue"`` for every other mode (propose, dry-run, and any
        future non-executing mode).
    """
    if mode == "execute":
        return "agentrun.execute"
    return "agentrun.queue"


def _serialize_obligations(obligations: Any) -> list[dict]:
    """Normalize a capauth ``Decision.obligations`` list to plain dicts.

    ``capauth.authz.decide`` returns pydantic ``Obligation`` (``kind``,
    ``data``) instances; this store-and-forward path needs plain JSON-able
    dicts (consent events are appended as JSONL). Anything already dict-shaped
    or unrecognized passes through unchanged rather than raising, since a
    serialization hiccup here must never turn an authz decision into an
    exception.
    """
    out: list[dict] = []
    for o in obligations or []:
        if hasattr(o, "model_dump"):
            out.append(o.model_dump())
        elif isinstance(o, dict):
            out.append(o)
        else:
            out.append({"kind": getattr(o, "kind", "unknown"), "data": getattr(o, "data", {})})
    return out


def _default_decide_fn(*, capability: str, resource: str, actor: Optional[str]) -> dict:
    """Call the real capauth PDP and return its allow decision AND obligations.

    Imports :func:`capauth.authz.decide` lazily so importing this module
    never requires capauth's dependency chain (pydantic, the pairing/tokens
    storage layer) to be importable, and so any import failure is caught and
    turned into a fail-closed deny rather than an unhandled exception at
    import time.

    ``capauth.authz.decide(subject, capability, resource=None, context=None,
    *, base_dir=None, rules=None) -> Decision`` (``Decision.allow: bool``,
    ``Decision.obligations: list[Obligation]``) is the real signature;
    ``actor`` here maps to its ``subject`` and our string ``resource`` is
    wrapped in a dict since ``decide`` expects ``Optional[dict]``.

    capauth's PDP emits an AUDIT obligation on every decision (allow or deny)
    that no PEP used to keep (Unified Consent Plane, section 2.1 row 1: "AUDIT
    obligation emitted, persisted by no PEP"). Returning it here instead of
    collapsing to a bare bool is what lets a caller persist a ``consent.granted``
    event carrying the PDP's own audit record rather than reconstructing one.

    Args:
        capability: The capability string to request (see :func:`capability_for`).
        resource: The resource identifier being acted on (e.g. a card id).
        actor: The already-authenticated subject/actor identity, or ``None``.

    Returns:
        dict: ``{"allow": bool, "obligations": list[dict]}``. ``allow`` is
        ``True`` only when capauth's ``Decision.allow`` is ``True``. Any
        import error, call error, or unexpected shape denies with an empty
        obligations list.
    """
    try:
        from capauth.authz import decide
    except Exception:  # noqa: BLE001
        return {"allow": False, "obligations": []}
    try:
        result = decide(actor or "", capability, resource={"id": resource})
    except Exception:  # noqa: BLE001
        return {"allow": False, "obligations": []}
    allow = getattr(result, "allow", None)
    obligations = getattr(result, "obligations", None)
    if allow is None and isinstance(result, dict):
        allow = result.get("allow")
        obligations = result.get("obligations", obligations)
    return {"allow": bool(allow), "obligations": _serialize_obligations(obligations)}


def _check_token(token: Optional[str]) -> tuple[bool, str]:
    """Constant-time shared-secret check against ``SKAI_QUEUE_TOKEN``.

    Fails closed when no secret is configured: an unset/empty
    ``SKAI_QUEUE_TOKEN`` denies rather than opening the gate, unlike the
    loopback-open behavior it replaces.

    Args:
        token: The capability token presented by the caller, or ``None``.

    Returns:
        tuple[bool, str]: ``(ok, reason)``.
    """
    secret = os.environ.get(_TOKEN_ENV_VAR)
    if not secret:
        return False, "token denied: SKAI_QUEUE_TOKEN is not configured"
    if not token:
        return False, "token denied: no capability token presented"
    if hmac.compare_digest(token, secret):
        return True, "token ok"
    return False, "token denied: capability token mismatch"


def _check_pdp(
    *,
    decide_fn: DecideFn,
    capability: str,
    resource: str,
    actor: Optional[str],
) -> tuple[bool, str, list[dict]]:
    """Ask ``decide_fn`` whether ``actor`` may exercise ``capability``.

    Any exception raised by ``decide_fn`` is caught and treated as a deny
    (fail-closed): a PDP that is unreachable, misconfigured, or raises must
    never be interpreted as an allow.

    Args:
        decide_fn: Callable accepting keyword args ``capability``,
            ``resource``, ``actor`` and returning a truthy/dict allow result
            (optionally carrying an ``"obligations"`` list) or a falsy deny
            result.
        capability: The capability string requested.
        resource: The resource identifier being acted on.
        actor: The already-authenticated subject/actor identity, or ``None``.

    Returns:
        tuple[bool, str, list[dict]]: ``(ok, reason, obligations)``.
        ``obligations`` is ``[]`` when ``decide_fn`` returned a bare bool (the
        test-injection shape) rather than the dict shape the real PDP uses.
    """
    try:
        result = decide_fn(capability=capability, resource=resource, actor=actor)
    except Exception as exc:  # noqa: BLE001
        return False, f"pdp denied: decide_fn raised {exc!r}", []
    if isinstance(result, dict):
        allow = result.get("allow")
        obligations = result.get("obligations") or []
    else:
        allow = result
        obligations = []
    if allow:
        return True, "pdp ok", obligations
    reason = "pdp denied: capability not granted"
    if capability == "agentrun.execute":
        reason += " (execute requires a 'verified' enrollment; the PDP enforces this)"
    return False, reason, obligations


def authorize_capability(
    *,
    token: Optional[str],
    resource: str,
    capability: str,
    actor: Optional[str] = None,
    decide_fn: Optional[DecideFn] = None,
) -> dict:
    """Authorize an arbitrary capability via the staged token/pdp/both gate.

    The general primitive behind :func:`authorize_queue`: everything except
    the capability string is shared (the ``SKAI_AUTHZ`` staging, the
    fail-closed token check, the PDP call), so every privileged dashboard
    route - the original queue-AI action (``agentrun.*``) and the change.*
    PEPs (validate/schedule/deploy, design doc
    docs/specs/2026-08-13-change-management-cab-ai-arch.md section 7) - can
    share one authorization path instead of re-deriving it. ``authorize_queue``
    is a thin wrapper that derives its capability from a run ``mode`` via
    :func:`capability_for` and delegates here.

    Args:
        token: The ``X-SK-Capability`` header value presented by the caller,
            or ``None``.
        resource: The resource identifier being acted on (e.g. a card or
            change id).
        capability: The capauth capability string being requested (e.g.
            ``"agentrun.execute"``, ``"change.validate"``).
        actor: The already-authenticated subject/actor identity presented to
            the PDP, or ``None``.
        decide_fn: Injectable PDP caller for tests (or an alternate PDP
            client). Accepts keyword args ``capability``, ``resource``,
            ``actor`` and returns a truthy/dict allow result or a falsy deny
            result. When ``None``, a default wrapping the real
            ``capauth.authz.decide`` is built lazily.

    Returns:
        dict: ``{"ok": bool, "reason": str, "via": str, "obligations": list[dict]}``
        where ``via`` is ``"token"``, ``"pdp"``, or ``"both"`` matching the
        active ``SKAI_AUTHZ`` mode, and ``obligations`` carries the PDP's
        AUDIT obligation (empty in ``"token"`` mode, since no PDP call is
        made there) - see :func:`_default_decide_fn`. A caller that wants to
        persist a ``consent.granted`` event reads this key rather than
        re-deriving an audit record of its own.
    """
    authz_mode = _authz_mode()
    effective_decide_fn = decide_fn if decide_fn is not None else _default_decide_fn

    if authz_mode == "token":
        ok, reason = _check_token(token)
        return {"ok": ok, "reason": reason, "via": "token", "obligations": []}

    if authz_mode == "pdp":
        ok, reason, obligations = _check_pdp(
            decide_fn=effective_decide_fn,
            capability=capability,
            resource=resource,
            actor=actor,
        )
        return {"ok": ok, "reason": reason, "via": "pdp", "obligations": obligations}

    # authz_mode == "both": require token AND pdp.
    token_ok, token_reason = _check_token(token)
    pdp_ok, pdp_reason, obligations = _check_pdp(
        decide_fn=effective_decide_fn,
        capability=capability,
        resource=resource,
        actor=actor,
    )
    ok = token_ok and pdp_ok
    if ok:
        reason = "token ok; pdp ok"
    else:
        parts = []
        if not token_ok:
            parts.append(token_reason)
        if not pdp_ok:
            parts.append(pdp_reason)
        reason = "; ".join(parts)
    return {"ok": ok, "reason": reason, "via": "both", "obligations": obligations}


def authorize_queue(
    *,
    token: Optional[str],
    resource: str,
    mode: str,
    actor: Optional[str] = None,
    decide_fn: Optional[DecideFn] = None,
) -> dict:
    """Authorize the privileged "queue AI to work an item" action.

    Staged behind the ``SKAI_AUTHZ`` migration flag (``"token"`` | ``"pdp"`` |
    ``"both"``, default ``"token"``) so the gate can move from the legacy
    shared-secret header check to a capauth PDP decision without a flag-day
    cutover. Every branch fails closed: an unset secret, a PDP exception, or
    an unrecognized mode all deny rather than allow. A thin wrapper over
    :func:`authorize_capability` that derives the capability from ``mode``.

    Args:
        token: The ``X-SK-Capability`` header value presented by the caller,
            or ``None``.
        resource: The resource identifier being acted on (e.g. a card id).
        mode: The requested run mode (``"execute"``, ``"propose"``,
            ``"dry-run"``, ...); passed through :func:`capability_for` to
            pick the capauth capability, and used to enrich deny reasons.
        actor: The already-authenticated subject/actor identity presented to
            the PDP, or ``None``.
        decide_fn: Injectable PDP caller for tests (or an alternate PDP
            client). Accepts keyword args ``capability``, ``resource``,
            ``actor`` and returns a truthy/dict allow result or a falsy deny
            result. When ``None``, a default wrapping the real
            ``capauth.authz.decide`` is built lazily.

    Returns:
        dict: ``{"ok": bool, "reason": str, "via": str, "obligations": list[dict]}``
        where ``via`` is ``"token"``, ``"pdp"``, or ``"both"`` matching the
        active ``SKAI_AUTHZ`` mode. See :func:`authorize_capability` for what
        ``obligations`` carries.
    """
    return authorize_capability(
        token=token,
        resource=resource,
        capability=capability_for(mode),
        actor=actor,
        decide_fn=decide_fn,
    )


__all__ = [
    "authorize_capability",
    "authorize_queue",
    "capability_for",
]
