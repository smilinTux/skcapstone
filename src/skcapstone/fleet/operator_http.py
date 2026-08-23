"""sknoded's operator-plane HTTP surface (``skoperator.remote/v1``, P1).

Implements the transport half of ``docs/OPERATOR_PLANE_REMOTE_STANDARD.md``
(ratified, PR #179) per the Phase 1 slice of ``docs/OPERATOR_PLANE_MIGRATION.md``:
a ``/operator/v1/...`` HTTP listener on ``sknoded``, tailnet-bind only, gated
OFF by default, serving the exact ``skoperator.observation/v1`` envelopes the
CLI-exec (``skcapstone atlas eyes``) path already produces. This module adds
NO new observation semantics: it is a wire transport in front of the same
freeze-independent, read-only lanes ``operator_seat.eyes`` already exercises.

Design decisions this module makes within the standard's stated flexibility
(not deviations -- the standard says "whichever local means the app provides"):

* ``GET /apps/{app}/observe`` prefers the CLI lane (``<spec.cli> observe``,
  exec'd node-local -- the standard's own rationale for why this surface
  exists: PATH is finally evaluated on the right node) and falls back to the
  in-process seat adapter (``operator_seat.loop.ADAPTERS``) only when the CLI
  lane cannot answer. When BOTH lanes answer but disagree on a condition,
  that condition renders ``Unknown`` with reason ``LaneConflict`` rather than
  picking a winner (standard section 8: two readings never averaged, never
  silently preferred).
* ``GET /apps/{app}/act`` and ``POST`` writes are OUT OF SCOPE for P1 (the
  card explicitly forbids P2/P3 work here). The path exists, is gated on
  ``operator.act``, checks freeze server-side on every call, and always
  answers "reserved, not implemented" -- it can never actuate anything.
* ``GET /operator/v1/estate`` aggregates only THIS node's apps. Cross-node
  fan-out is Phase 4/5 territory (it needs node-to-node registration this
  phase does not add) and is explicitly out of scope here.

Gate: ``SKOPERATOR_HTTP`` (env), default OFF -- unset, a node behaves
byte-identically to before this module existed; ``sknoded.main_loop`` does not
even import this module's heavy dependencies until the gate is checked true.

Bind: ``SKOPERATOR_HTTP_BIND`` must name a concrete, non-wildcard address (the
node's Tailscale IP). Blank, unset, ``0.0.0.0``, or ``::`` all REFUSE to bind
(mirrors ``skharness.serve.resolve_bind``, the shipped precedent for
``skcode-hostd.service``). This surface never listens on a public interface,
gate on or off.

Auth: capauth end to end, per standard section 6.

* Identity: every request carries ``X-SK-Fingerprint`` / ``X-SK-Timestamp`` /
  ``X-SK-Nonce`` / ``X-SK-Signature``, a detached PGP signature over
  ``method\\npath\\nsha256(body)\\ntimestamp\\nnonce`` made by the fingerprint's
  OWN key (never "any trusted key", which would let key A's signature stand
  in for key B's claimed identity). Skew > 120s or a replayed nonce refuses.
* Capability: the authenticated fingerprint is then checked against
  ``capauth.authz.decide()`` -- the shipped PDP, not a new auth scheme -- for
  ``operator.observe`` / ``operator.act`` / ``operator.estate.read``
  (``OPERATOR_RULES`` below, passed as this call's ``rules=`` override since
  these three capabilities are not in capauth's own ``DEFAULT_RULES`` table;
  ``decide()`` is written exactly to take a caller-supplied rule table).
  ``operator.observe`` NEVER implies ``operator.act``: they are different
  capability strings, checked independently, gated to different minimum
  enrollment modes (observe: tofu: the skchat.inbox / skchat.status tier;
  act: verified: the skchat.send / skgateway.admin tier).

Failure taxonomy (standard section 7): every auth/routing failure this module
manufactures maps to exactly one of three families, and the mapping is a pure
function (``REASON_FAMILY``) so a test can assert no reason silently drifts
into "healthy": ``Unreachable`` (no endpoint, agent not ready, stale watch),
``Unknown`` (probe failed/timed out/unparseable, lane conflict), or
``Unauthorized`` (missing/invalid signature, missing capability). An app's own
condition health is NEVER inferred from any of these; it comes only from its
observation envelope.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlsplit

from . import signing, store
from .paths import FleetPaths

logger = logging.getLogger(__name__)

# ── gate + bind ───────────────────────────────────────────────────────────

#: Master gate. Default OFF (unset): a node behaves byte-identically to
#: before this surface existed. Same truthy convention as
#: ``operator_seat.discovery.DISCOVERY_ENV``.
GATE_ENV = "SKOPERATOR_HTTP"

#: The Tailscale IP to bind. No default: an unset/blank/wildcard value REFUSES
#: to bind rather than falling back to a public or loopback address (matches
#: ``skharness.serve.resolve_bind`` / ``skcode-hostd.service``).
BIND_ENV = "SKOPERATOR_HTTP_BIND"

#: Registered in docs/PORTS.md and FLEET_RESERVED_PORTS (src/skcapstone/__init__.py).
PORT = 9392

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_WILDCARD = frozenset({"0.0.0.0", "::"})


def http_enabled() -> bool:
    """Whether ``SKOPERATOR_HTTP`` is set to a truthy value (default OFF)."""
    import os

    return os.environ.get(GATE_ENV, "").strip().lower() in _TRUTHY


def resolve_bind_host(explicit: str | None = None) -> str | None:
    """The concrete bind address, or None to refuse starting the surface.

    Args:
        explicit: Override for tests; falls back to ``SKOPERATOR_HTTP_BIND``.

    Returns:
        The trimmed host string, or None when it is unset, blank, or a
        wildcard/public address (``0.0.0.0`` / ``::``).
    """
    import os

    host = explicit if explicit is not None else os.environ.get(BIND_ENV, "")
    host = (host or "").strip()
    if not host or host in _WILDCARD:
        return None
    return host


# ── failure taxonomy (standard section 7) ───────────────────────────────────

FAMILY_UNREACHABLE = "Unreachable"
FAMILY_UNKNOWN = "Unknown"
FAMILY_UNAUTHORIZED = "Unauthorized"

REASON_NO_ENDPOINT = "NoEndpoint"
REASON_AGENT_NOT_READY = "AgentNotReady"
REASON_WATCH_STALE = "WatchStale"
REASON_PROBE_FAILED = "ProbeFailed"
REASON_PROBE_TIMEOUT = "ProbeTimeout"
REASON_UNPARSEABLE = "Unparseable"
REASON_LANE_CONFLICT = "LaneConflict"
REASON_UNAUTHORIZED = "Unauthorized"
REASON_SIGNATURE_INVALID = "SignatureInvalid"
REASON_CAPABILITY_MISSING = "CapabilityMissing"

#: Pure mapping, asserted exhaustive by tests: no reason code may be absent
#: (and therefore silently ambiguous) and none may map anywhere but one of
#: the three named families -- never "healthy".
REASON_FAMILY: dict[str, str] = {
    REASON_NO_ENDPOINT: FAMILY_UNREACHABLE,
    REASON_AGENT_NOT_READY: FAMILY_UNREACHABLE,
    REASON_WATCH_STALE: FAMILY_UNREACHABLE,
    REASON_PROBE_FAILED: FAMILY_UNKNOWN,
    REASON_PROBE_TIMEOUT: FAMILY_UNKNOWN,
    REASON_UNPARSEABLE: FAMILY_UNKNOWN,
    REASON_LANE_CONFLICT: FAMILY_UNKNOWN,
    REASON_UNAUTHORIZED: FAMILY_UNAUTHORIZED,
    REASON_SIGNATURE_INVALID: FAMILY_UNAUTHORIZED,
    REASON_CAPABILITY_MISSING: FAMILY_UNAUTHORIZED,
}

#: Timeouts (standard section 7, "one place"). Request: matches
#: ``operator_seat.discovery.SUBPROCESS_TIMEOUT``. Watch heartbeat/dead: the
#: SSE cadence below.
REQUEST_TIMEOUT_S = 10.0
WATCH_HEARTBEAT_S = 30.0
WATCH_POLL_S = 5.0

# ── capauth scopes (standard section 6) ─────────────────────────────────────

SCOPE_OBSERVE = "operator.observe"
SCOPE_ACT = "operator.act"
SCOPE_ESTATE_READ = "operator.estate.read"

#: Skew tolerance for X-SK-Timestamp and the replay window for X-SK-Nonce.
AUTH_SKEW_S = 120.0


def _operator_rules() -> dict[str, Any]:
    """The three operator capability rules, built lazily (capauth is a soft
    import everywhere else in ``fleet``, so this module stays importable
    without capauth installed; only calling ``authorize()`` needs it).

    Not in capauth's own ``DEFAULT_RULES``: ``decide()`` is written to accept
    a caller-supplied ``rules=`` table for exactly this case (its docstring:
    "skcode.dispatch and other surfaces slot in later by adding rows here").
    Minimum modes follow the same read/write/act sensitivity gradient
    capauth's own seeded rules already use (skchat.inbox / skgateway.infer /
    skchat.send): observe and the estate rollup are TOFU (least sensitive,
    matching skchat.status's "read operational metadata" tier); act is
    VERIFIED (matches skchat.send / skgateway.admin / agentrun.execute --
    every actuation-class capability capauth ships is VERIFIED).
    """
    from capauth import CapabilityRule
    from capauth.pairing import EnrollmentMode

    return {
        SCOPE_OBSERVE: CapabilityRule(
            capability=SCOPE_OBSERVE,
            required_capability=SCOPE_OBSERVE,
            minimum_mode=EnrollmentMode.TOFU,
            description="Read one app's observation envelope or explain contract.",
        ),
        SCOPE_ESTATE_READ: CapabilityRule(
            capability=SCOPE_ESTATE_READ,
            required_capability=SCOPE_ESTATE_READ,
            minimum_mode=EnrollmentMode.TOFU,
            description="Read this node's aggregate estate rollup.",
        ),
        SCOPE_ACT: CapabilityRule(
            capability=SCOPE_ACT,
            required_capability=SCOPE_ACT,
            minimum_mode=EnrollmentMode.VERIFIED,
            description=(
                "Reserved for future actuation; unimplemented in P1. observe " "NEVER implies act."
            ),
        ),
    }


def authorize(
    fingerprint: str,
    capability: str,
    *,
    decide_fn: Callable[..., Any] | None = None,
    base_dir=None,
) -> tuple[bool, str]:
    """Ask capauth's PDP whether ``fingerprint`` holds ``capability``.

    Fails closed: an unavailable capauth import denies with a clear reason
    rather than raising past the caller (mirrors ``fleet.signing``'s
    lazy-capauth posture).

    Returns:
        ``(allow, reason)``. ``reason`` is always a human sentence from
        ``capauth.authz.decide`` (or the import-failure message); callers map
        a False ``allow`` to :data:`REASON_CAPABILITY_MISSING` at the HTTP
        boundary, keeping the wire-facing reason code stable even though the
        underlying capauth sentence may change wording.
    """
    try:
        from capauth import decide
    except Exception as exc:  # noqa: BLE001
        return False, f"capauth unavailable: {exc}"
    decide_fn = decide_fn or decide
    decision = decide_fn(fingerprint, capability, base_dir=base_dir, rules=_operator_rules())
    return bool(decision.allow), str(decision.reason)


# ── request signing (standard section 6) ────────────────────────────────────


def canonical_request_bytes(
    method: str, path: str, body: bytes, timestamp: str, nonce: str
) -> bytes:
    """The exact bytes a client signs: ``method\\npath\\nsha256(body)\\nts\\nnonce``."""
    digest = hashlib.sha256(body or b"").hexdigest()
    return f"{method}\n{path}\n{digest}\n{timestamp}\n{nonce}".encode("utf-8")


class NonceCache:
    """Per-node in-memory replay guard, bounded by the auth skew window.

    In-process is a deliberate, sufficient choice: sknoded is one long-lived
    process per node (never multi-worker), so a restart clearing history is
    the same "relist" story the watch cursor already tells, not a new gap.
    """

    def __init__(self, window_s: float = AUTH_SKEW_S) -> None:
        self._window = window_s
        self._seen: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def check_and_record(self, fingerprint: str, nonce: str, now: float) -> bool:
        """Return True (and record) when ``(fingerprint, nonce)`` is fresh."""
        with self._lock:
            cutoff = now - self._window
            for key in [k for k, seen_at in self._seen.items() if seen_at < cutoff]:
                del self._seen[key]
            key = (fingerprint, nonce)
            if key in self._seen:
                return False
            self._seen[key] = now
            return True


def _default_verify_one(data: bytes, signature: str, armored_key: str) -> bool:
    from capauth.crypto import get_backend

    return get_backend().verify(data, signature, armored_key)


@dataclass
class AuthResult:
    fingerprint: str | None
    reason: str | None  # one of the REASON_* constants, or None on success
    detail: str


def verify_signed_request(
    method: str,
    path: str,
    body: bytes,
    headers: Mapping[str, str],
    *,
    roster_by_fpr: Callable[[], dict[str, str]] | None = None,
    verify_one: Callable[[bytes, str, str], bool] | None = None,
    nonce_cache: NonceCache | None = None,
    now: float | None = None,
) -> AuthResult:
    """Verify the four ``X-SK-*`` headers per standard section 6.

    Pinned verification: the claimed fingerprint's OWN key must verify the
    signature (via ``roster_by_fpr()[fingerprint]``), never "any roster key".
    A fingerprint absent from the roster is :data:`REASON_UNAUTHORIZED`
    (unknown identity); a present fingerprint whose signature does not verify,
    a stale timestamp, or a replayed nonce are all :data:`REASON_SIGNATURE_INVALID`
    (a known identity that failed cryptographic proof).
    """
    roster_by_fpr = roster_by_fpr or signing.roster_by_fingerprint
    verify_one = verify_one or _default_verify_one
    now = time.time() if now is None else now

    fpr = (headers.get("X-SK-Fingerprint") or "").strip()
    ts = (headers.get("X-SK-Timestamp") or "").strip()
    nonce = (headers.get("X-SK-Nonce") or "").strip()
    sig = (headers.get("X-SK-Signature") or "").strip()
    if not (fpr and ts and nonce and sig):
        return AuthResult(None, REASON_UNAUTHORIZED, "missing one or more X-SK-* auth headers")

    try:
        ts_val = float(ts)
    except ValueError:
        return AuthResult(None, REASON_SIGNATURE_INVALID, "X-SK-Timestamp is not numeric")
    if abs(now - ts_val) > AUTH_SKEW_S:
        return AuthResult(
            None, REASON_SIGNATURE_INVALID, f"timestamp skew exceeds {AUTH_SKEW_S:.0f}s"
        )

    try:
        roster = roster_by_fpr()
    except Exception as exc:  # noqa: BLE001
        return AuthResult(None, REASON_UNAUTHORIZED, f"roster unavailable: {exc}")
    key = roster.get(fpr)
    if key is None:
        return AuthResult(None, REASON_UNAUTHORIZED, f"fingerprint {fpr!r} is not trusted")

    data = canonical_request_bytes(method, path, body, ts, nonce)
    try:
        ok = verify_one(data, sig, key)
    except Exception as exc:  # noqa: BLE001
        return AuthResult(None, REASON_SIGNATURE_INVALID, f"verifier error: {exc}")
    if not ok:
        return AuthResult(
            None, REASON_SIGNATURE_INVALID, "signature does not verify against that fingerprint"
        )

    cache = nonce_cache if nonce_cache is not None else _DEFAULT_NONCE_CACHE
    if not cache.check_and_record(fpr, nonce, now):
        return AuthResult(None, REASON_SIGNATURE_INVALID, "nonce already used (replay)")

    return AuthResult(fpr, None, "ok")


_DEFAULT_NONCE_CACHE = NonceCache()


# ── envelope signing (standard section 6) ───────────────────────────────────


def _envelope_canonical_bytes(envelope: dict) -> bytes:
    body = {k: v for k, v in envelope.items() if k not in ("signature", "signer_fpr")}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_envelope(
    envelope: dict,
    *,
    signer: Callable[[bytes], str] | None = None,
    signer_fpr: str | None = None,
) -> dict:
    """Attach ``signature`` + ``signer_fpr`` to an envelope, or leave both None.

    Reuses ``fleet.signing``'s existing, already-gated (``SKFLEET_SIGNING``,
    default off) capauth signer rather than a new signing path: no signer
    configured (the common case today) yields an explicitly-present,
    explicitly-``None`` pair, never a missing key that could be mistaken for
    "not yet checked".
    """
    resolved_signer = signer if signer is not None else signing.default_signer()
    out = dict(envelope)
    if resolved_signer is None:
        out.setdefault("signature", None)
        out.setdefault("signer_fpr", None)
        return out
    try:
        out["signature"] = resolved_signer(_envelope_canonical_bytes(envelope))
        out["signer_fpr"] = signer_fpr or signing.own_fingerprint()
    except Exception:  # noqa: BLE001 -- signing is best-effort, never fatal to a GET
        out["signature"] = None
        out["signer_fpr"] = None
    return out


# ── local observation (reuses operator_seat.eyes' freeze-independent lanes) ─


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect_app_lanes(
    paths: FleetPaths,
    *,
    now_iso: str | None = None,
    cli_timeout: float = REQUEST_TIMEOUT_S,
    seat_timeout: float = REQUEST_TIMEOUT_S,
    run: Callable[[list[str], float], tuple[int, str, str]] | None = None,
    adapters: dict[str, Callable[..., dict]] | None = None,
    problem_when_true: frozenset | None = None,
) -> dict[str, dict]:
    """The per-app cli/seat lane readings this node can locally produce.

    Deliberately mirrors ``operator_seat.eyes.assess()``'s app-collection
    loop (registered Operatorapp specs, plus seat-only built-ins) MINUS its
    ITIL correlation and unregistered-module scan: those read outside the
    fleet tree (the coordination/itil and shell/modules.json siblings)
    and are eyes-report concerns, not part of one app's observation envelope.
    Freeze-independent BY CONSTRUCTION: unlike ``operator_seat.loop._run_once``
    (which stands down entirely when frozen), this never reads
    ``store.is_frozen`` and never could -- observe has no freeze branch to
    forget to bypass.

    Returns:
        ``{app_name: {"cli", "declared_conditions", "cli_lane", "seat_lane",
        "conflicts"}}``.
    """
    from ..operator_seat import eyes, loop

    now = now_iso or _now_iso()
    if adapters is None:
        adapters = loop.ADAPTERS
    if problem_when_true is None:
        problem_when_true = loop.PROBLEM_WHEN_TRUE

    out: dict[str, dict] = {}
    for spec_obj in store.list_specs(paths, "operatorapp"):
        spec = spec_obj.get("spec", {}) or {}
        name = spec_obj.get("name") or spec.get("name")
        if not name or spec.get("deleted"):
            continue
        declared = [str(c) for c in spec.get("conditions", []) or []]
        cli = str(spec.get("cli", "") or "")
        cli_lane = (
            eyes.observe_via_cli(cli, declared, problem_when_true, timeout=cli_timeout, run=run)
            if cli
            else {"state": "no-cli", "conditions": [], "detail": "spec declares no cli"}
        )
        seat_lane = eyes.observe_via_seat(
            name, adapters, declared, problem_when_true, paths, now, timeout=seat_timeout
        )
        out[name] = {
            "cli": cli,
            "declared_conditions": declared,
            "cli_lane": cli_lane,
            "seat_lane": seat_lane,
            "conflicts": eyes.lane_conflicts(cli_lane, seat_lane),
        }

    for name in sorted(adapters):
        if name in out:
            continue
        seat_lane = eyes.observe_via_seat(
            name, adapters, [], problem_when_true, paths, now, timeout=seat_timeout
        )
        out[name] = {
            "cli": "",
            "declared_conditions": [],
            "cli_lane": {
                "state": "unregistered",
                "conditions": [],
                "detail": "no Operatorapp registration; seat-only builtin",
            },
            "seat_lane": seat_lane,
            "conflicts": [],
        }
    return out


_UNREACHABLE_LANE_STATES = frozenset(
    {"no-cli", "cli-error", "timeout", "unparseable", "no-adapter", "error", "unregistered"}
)


def _lane_failure_reason(cli_lane: dict, seat_lane: dict) -> str:
    """Map two failed lanes to one standard reason code (section 7)."""
    states = {cli_lane.get("state"), seat_lane.get("state")}
    absent = {"no-cli", "unregistered", "no-adapter"}
    if states <= absent:
        return REASON_NO_ENDPOINT
    if "timeout" in states:
        return REASON_PROBE_TIMEOUT
    if "unparseable" in states:
        return REASON_UNPARSEABLE
    return REASON_PROBE_FAILED


def build_observation_envelope(app: str, entry: dict, *, observed_at: str | None = None) -> dict:
    """One ``skoperator.observation/v1`` envelope for ``app``, unsigned.

    Prefers the cli lane (see module docstring); falls back to the seat lane;
    surfaces per-condition ``LaneConflict`` when both lanes answer but
    disagree; never fabricates health when both lanes fail.
    """
    from ..operator_seat import adapter, loop

    cli_lane = entry["cli_lane"]
    seat_lane = entry["seat_lane"]
    schema = loop.CONDITION_SCHEMAS.get(app) or adapter.condition_schema(
        entry["declared_conditions"]
    )

    conflicting = {c["type"] for c in entry["conflicts"]}
    if cli_lane.get("state") == "ok":
        conditions = list(cli_lane["conditions"])
        provenance = f"cli-local:{app}"
    elif seat_lane.get("state") == "ok":
        conditions = list(seat_lane["conditions"])
        provenance = f"builtin:{app}"
    else:
        conditions = []
        provenance = f"unreachable:{app}"

    raw: list[dict] = []
    reason = _lane_failure_reason(cli_lane, seat_lane) if not conditions else None
    for cond in conditions:
        ctype = cond["type"]
        if ctype in conflicting:
            raw.append(
                {
                    "type": ctype,
                    "status": "Unknown",
                    "reason": REASON_LANE_CONFLICT,
                    "message": f"cli and seat lanes disagree on {ctype}",
                }
            )
        elif cond.get("absent"):
            raw.append(
                {
                    "type": ctype,
                    "status": "Unknown",
                    "reason": REASON_PROBE_FAILED,
                    "message": "declared condition absent from the lane's payload",
                }
            )
        else:
            raw.append({"type": ctype, "status": cond["status"]})

    payload = {"conditions": raw} if raw else None
    envelope = adapter.normalize_observe(
        app, payload, schema, observed_at=observed_at or _now_iso(), provenance=provenance
    )
    if reason is not None:
        for cond in envelope["conditions"]:
            cond.setdefault("reason", reason)
            cond.setdefault(
                "message", f"cli_lane={cli_lane.get('state')} seat_lane={seat_lane.get('state')}"
            )
    return envelope


# ── the pure router (testable without a socket) ─────────────────────────────


@dataclass
class HttpResponse:
    status: int
    body: dict
    headers: dict = field(default_factory=dict)


def _error(status: int, reason: str, message: str) -> HttpResponse:
    family = REASON_FAMILY.get(reason, FAMILY_UNKNOWN)
    body = {"status": "Unknown", "family": family, "reason": reason, "message": message}
    return HttpResponse(status, body)


@dataclass
class RouterDeps:
    """Injectable seams so ``route()`` runs in tests with zero real crypto/IO."""

    paths: FleetPaths
    node: str
    verify_request: Callable[..., AuthResult] = verify_signed_request
    authorize_fn: Callable[..., tuple[bool, str]] = authorize
    signer: Callable[[bytes], str] | None = None
    signer_fpr: str | None = None
    now_iso: Callable[[], str] = _now_iso
    readiness_checks: Callable[[], list[str]] | None = None


def _check_readiness(deps: RouterDeps) -> list[str]:
    """Failing dependency names, per standard section 4. Empty means ready."""
    if deps.readiness_checks is not None:
        return deps.readiness_checks()
    failing = []
    if not deps.paths.root.exists():
        failing.append("registry unreadable: fleet root does not exist")
    try:
        import capauth  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        failing.append(f"capauth registry unavailable: {exc}")
    return failing


def _require_auth(
    deps: RouterDeps, method: str, path: str, body: bytes, headers: Mapping[str, str], scope: str
) -> tuple[str, None] | tuple[None, HttpResponse]:
    """Authenticate + authorize one request for ``scope``. Returns (fingerprint,
    None) on success or (None, error_response) on failure."""
    result = deps.verify_request(method, path, body, headers)
    if result.reason is not None:
        status = 401
        return None, _error(status, result.reason, result.detail)
    allow, reason = deps.authorize_fn(result.fingerprint, scope)
    if not allow:
        return None, _error(403, REASON_CAPABILITY_MISSING, reason)
    return result.fingerprint, None


def route(
    method: str,
    raw_path: str,
    headers: Mapping[str, str],
    body: bytes,
    deps: RouterDeps,
) -> HttpResponse:
    """Pure request router: no socket, no threading. One dispatch per call."""
    split = urlsplit(raw_path)
    path = split.path
    parts = [p for p in path.split("/") if p]

    if parts[:2] != ["operator", "v1"]:
        return HttpResponse(404, {"error": "not found"})
    tail = parts[2:]

    if tail == ["healthz"]:
        return HttpResponse(200, {"status": "ok", "node": deps.node})

    if tail == ["readyz"]:
        failing = _check_readiness(deps)
        if failing:
            return HttpResponse(503, {"ready": False, "failing": failing})
        return HttpResponse(200, {"ready": True, "failing": []})

    if tail == ["apps"] and method == "GET":
        fpr, err = _require_auth(deps, method, path, body, headers, SCOPE_OBSERVE)
        if err is not None:
            return err
        lanes = collect_app_lanes(deps.paths)
        apps = []
        for name, entry in sorted(lanes.items()):
            if entry["cli_lane"]["state"] == "ok":
                shape = "exec"
            elif entry["seat_lane"]["state"] == "ok":
                shape = "in-proc"
            else:
                shape = "unknown"
            apps.append(
                {
                    "name": name,
                    "contractVersion": 1,
                    "facet": shape,
                    "cli": entry["cli"],
                    "endpoint": None,
                }
            )
        return HttpResponse(200, {"node": deps.node, "apps": apps})

    if len(tail) == 3 and tail[0] == "apps" and tail[2] == "explain" and method == "GET":
        fpr, err = _require_auth(deps, method, path, body, headers, SCOPE_OBSERVE)
        if err is not None:
            return err
        app = tail[1]
        from ..operator_seat import registration

        meta = registration.APP_REGISTRY.get(app)
        if meta is None:
            return _error(404, REASON_NO_ENDPOINT, f"{app!r} is not a known adapter on this node")
        return HttpResponse(200, meta["explain"]())

    if len(tail) == 3 and tail[0] == "apps" and tail[2] == "observe" and method == "GET":
        fpr, err = _require_auth(deps, method, path, body, headers, SCOPE_OBSERVE)
        if err is not None:
            return err
        app = tail[1]
        lanes = collect_app_lanes(deps.paths)
        entry = lanes.get(app)
        if entry is None:
            return _error(404, REASON_NO_ENDPOINT, f"{app!r} is not observable on this node")
        envelope = build_observation_envelope(app, entry, observed_at=deps.now_iso())
        envelope = sign_envelope(envelope, signer=deps.signer, signer_fpr=deps.signer_fpr)
        return HttpResponse(200, envelope)

    if tail == ["estate"] and method == "GET":
        fpr, err = _require_auth(deps, method, path, body, headers, SCOPE_ESTATE_READ)
        if err is not None:
            return err
        lanes = collect_app_lanes(deps.paths)
        envelopes = [
            sign_envelope(
                build_observation_envelope(name, entry, observed_at=deps.now_iso()),
                signer=deps.signer,
                signer_fpr=deps.signer_fpr,
            )
            for name, entry in sorted(lanes.items())
        ]
        return HttpResponse(
            200,
            {
                "schema": "skoperator.estate/v1",
                "node": deps.node,
                "scope": "single-node",
                "note": (
                    "P1: this node's own apps only. Cross-node fan-out is a "
                    "later phase (needs node-to-node registration this "
                    "surface does not add yet)."
                ),
                "observed_at": deps.now_iso(),
                "apps": envelopes,
            },
        )

    if len(tail) == 3 and tail[0] == "apps" and tail[2] == "act" and method == "POST":
        fpr, err = _require_auth(deps, method, path, body, headers, SCOPE_ACT)
        if err is not None:
            return err
        frozen = store.is_frozen(deps.paths)
        return HttpResponse(
            501,
            {
                "error": "act is reserved, not implemented in P1",
                "frozen": frozen,
                "note": (
                    "the act migration (standard section on non-goals) is out "
                    "of scope for this surface; this path exists only so a "
                    "caller can tell 'refused' apart from 'does not exist'"
                ),
            },
        )

    return HttpResponse(404, {"error": "not found"})


# ── watch (SSE): cursor + heartbeat + 410-relist (standard section 5) ──────


class CursorState:
    """A per-node monotonic cursor over the composed local observation set.

    Bumped only when the digest of "every local app's envelope" actually
    changes, matching "bumped on every NEW observation" (section 5), not on
    every poll tick. A restart resets to empty history: a client holding a
    cursor from before the restart gets 410 Gone and relists, exactly the
    k8s resourceVersion-too-old dance the standard borrows.
    """

    def __init__(self, ring_size: int = 200) -> None:
        self._lock = threading.Lock()
        self._cursor = 0
        self._digest: str | None = None
        self._seen: set[int] = {0}
        self._ring_size = ring_size

    def bump_if_changed(self, digest: str) -> int:
        with self._lock:
            if digest != self._digest:
                self._cursor += 1
                self._digest = digest
                self._seen.add(self._cursor)
                if len(self._seen) > self._ring_size:
                    self._seen.discard(min(self._seen - {0}))
            return self._cursor

    def has_cursor(self, cursor: int) -> bool:
        with self._lock:
            return cursor in self._seen


def _estate_digest(deps: RouterDeps) -> tuple[str, list[dict]]:
    lanes = collect_app_lanes(deps.paths)
    envelopes = [
        build_observation_envelope(name, entry, observed_at=deps.now_iso())
        for name, entry in sorted(lanes.items())
    ]
    canonical = json.dumps(envelopes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest(), envelopes


def watch_stream(
    deps: RouterDeps,
    cursor_state: CursorState,
    client_cursor: int,
    *,
    write: Callable[[bytes], None],
    stop_event: threading.Event,
    poll_s: float = WATCH_POLL_S,
    heartbeat_s: float = WATCH_HEARTBEAT_S,
) -> None:
    """Drive one SSE connection until ``stop_event`` fires or the write fails.

    Caller has already written the ``200``/``text/event-stream`` header and
    already handled the 410-Gone-on-stale-cursor case before calling this.
    """
    last_sent = time.monotonic()
    while not stop_event.is_set():
        digest, envelopes = _estate_digest(deps)
        cursor = cursor_state.bump_if_changed(digest)
        if cursor != client_cursor:
            client_cursor = cursor
            frame = (
                f"id: {cursor}\ndata: "
                + json.dumps({"node": deps.node, "apps": envelopes}, sort_keys=True)
                + "\n\n"
            )
            try:
                write(frame.encode("utf-8"))
            except OSError:
                return
            last_sent = time.monotonic()
        elif time.monotonic() - last_sent >= heartbeat_s:
            try:
                write(b": heartbeat\n\n")
            except OSError:
                return
            last_sent = time.monotonic()
        stop_event.wait(timeout=poll_s)


# ── socket server ────────────────────────────────────────────────────────


def _make_handler(deps: RouterDeps, cursor_state: CursorState, stop_event: threading.Event):
    class Handler(BaseHTTPRequestHandler):
        server_version = "sknoded-operator-http/1"

        def log_message(self, fmt, *args):  # noqa: N802 - stdlib signature
            logger.debug("operator_http: %s - %s", self.address_string(), fmt % args)

        def _body(self) -> bytes:
            length = int(self.headers.get("Content-Length", 0) or 0)
            return self.rfile.read(length) if length > 0 else b""

        def _send_json(self, resp: HttpResponse) -> None:
            payload = json.dumps(resp.body, sort_keys=True).encode("utf-8")
            self.send_response(resp.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            for key, value in resp.headers.items():
                self.send_header(key, value)
            self.end_headers()
            try:
                self.wfile.write(payload)
            except OSError:
                pass

        def _dispatch(self, method: str) -> None:
            split = urlsplit(self.path)
            parts = [p for p in split.path.split("/") if p]
            body = self._body()

            if parts == ["operator", "v1", "observe"] and method == "GET":
                query = parse_qs(split.query)
                fpr, err = _require_auth(
                    deps, method, split.path, body, self.headers, SCOPE_OBSERVE
                )
                if err is not None:
                    self._send_json(err)
                    return
                if query.get("watch", ["0"])[0] not in ("1", "true"):
                    digest, envelopes = _estate_digest(deps)
                    cursor = cursor_state.bump_if_changed(digest)
                    self._send_json(
                        HttpResponse(200, {"node": deps.node, "cursor": cursor, "apps": envelopes})
                    )
                    return
                try:
                    requested = int(query.get("cursor", ["0"])[0])
                except ValueError:
                    requested = 0
                if requested and not cursor_state.has_cursor(requested):
                    self._send_json(
                        _error(410, REASON_WATCH_STALE, "cursor no longer held; relist")
                    )
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                try:
                    self.wfile.flush()
                except OSError:
                    return
                watch_stream(
                    deps,
                    cursor_state,
                    requested,
                    write=lambda chunk: (self.wfile.write(chunk), self.wfile.flush()),
                    stop_event=stop_event,
                )
                return

            resp = route(method, self.path, self.headers, body, deps)
            self._send_json(resp)

        def do_GET(self) -> None:  # noqa: N802 - stdlib signature
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802 - stdlib signature
            self._dispatch("POST")

    return Handler


@dataclass
class ServerHandle:
    server: ThreadingHTTPServer
    thread: threading.Thread
    stop_event: threading.Event
    host: str
    port: int

    def stop(self, timeout: float = 5.0) -> None:
        self.stop_event.set()
        self.server.shutdown()
        self.thread.join(timeout=timeout)
        self.server.server_close()


def start_background(
    paths: FleetPaths,
    node: str,
    *,
    host: str | None = None,
    port: int | None = None,
    signer: Callable[[bytes], str] | None = None,
    signer_fpr: str | None = None,
) -> ServerHandle | None:
    """Start the operator-plane HTTP surface in a background thread.

    Returns None (and logs, never raises) when no valid bind host resolves:
    a misconfigured/absent Tailscale IP disables the SURFACE, not the node
    agent's core self-report loop. Callers passing an explicit ``host``
    bypass env resolution entirely (tests use this to bind ``127.0.0.1``
    without touching ``SKOPERATOR_HTTP_BIND``); production callers leave it
    None so the wildcard-refusing env resolution applies.
    """
    resolved_host = host if host is not None else resolve_bind_host()
    if resolved_host is None:
        logger.error("operator_http: refusing to start -- %s is unset/blank/wildcard", BIND_ENV)
        return None
    resolved_port = PORT if port is None else port

    deps = RouterDeps(paths=paths, node=node, signer=signer, signer_fpr=signer_fpr)
    cursor_state = CursorState()
    stop_event = threading.Event()
    handler = _make_handler(deps, cursor_state, stop_event)

    try:
        server = ThreadingHTTPServer((resolved_host, resolved_port), handler)
    except OSError as exc:
        logger.error("operator_http: bind %s:%s failed: %s", resolved_host, resolved_port, exc)
        return None
    server.daemon_threads = True

    thread = threading.Thread(
        target=server.serve_forever, name="sknoded-operator-http", daemon=True
    )
    thread.start()
    actual_port = server.server_address[1]
    logger.info("operator_http: listening on %s:%s", resolved_host, actual_port)
    return ServerHandle(
        server=server, thread=thread, stop_event=stop_event, host=resolved_host, port=actual_port
    )
