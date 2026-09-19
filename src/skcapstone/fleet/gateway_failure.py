"""One recognition table for gateway failures, shared by launcher and wrapper.

The fleet has two classifiers for the same question, "did this worker fail
before it did any work on the card, because of the gateway rather than the
card". ``skfleet-worker-wrapper.py`` answers it at exit and stamps the answer
into the worker-exit record's ``transport_failure``; ``skfleet-rotate.py``
answers it again from the worker's stdout log. Until 2026-09-18 the two tables
were maintained separately and neither recognised HTTP 503.

Measured that day over the 2,980 worker-exit records on the chi fleet, 1,793
were a 503 the gateway had emitted before any model was reached, and 2,901 of
2,980 records carried ``transport_failure: null``. Seat
``pi-glm-chiap01-0aec5a64`` alone produced 120 of them against bucket
``sk-glm-s`` between 06:17 and 15:22 UTC on 2026-09-09, each hold about 21
seconds with nothing written under it. Scored as ordinary failed work, that
pure infrastructure outage was re-dispatched every five minutes for nine hours
and drove cards into the monotonic claim ceiling.

Both call sites now import their table from here, so a status the gateway
starts emitting cannot be recognised by one and missed by the other.
"""

from __future__ import annotations

import json
import re

#: One HTTP status followed by one JSON object, and nothing else. The whole
#: report must match: arbitrary prose, partial agent output and mixed reports
#: stay substantive, because a gateway error that arrives *after* the agent
#: produced output is not a pre-agent failure.
GATEWAY_ERROR_RE = re.compile(r"^\s*(\d{3}):\s*(\{.*\})\s*$", re.S)

#: Advisory lines the worker CLI prints before it has done anything. They are
#: startup chatter, not agent output, so a gateway error behind one is still a
#: pre-agent failure. Measured 2026-09-18: 86 chi exit records carry one of
#: these ahead of a real status+JSON body and were missed by the anchored match.
_PREAMBLE_RE = re.compile(
    r"^(?:Warning: Model \".*?\" not found for provider \".*?\"\..*|MCP: .*)$"
)

#: 502 bodies whose code names an upstream failure. An unnamed 502 code stays
#: substantive on purpose (see tests/test_skfleet_transport_retry.py).
_UPSTREAM_CODES = frozenset(
    {
        "empty_upstream_response",
        "invalid_upstream_completion",
        "upstream_unreachable",
    }
)

_TIMEOUT_CODES = frozenset(
    {
        "first_token_timeout",
        "gateway_timeout",
        "timeout_before_first_token",
        "upstream_timeout",
    }
)


def strip_preamble(text: str) -> str:
    """Drop leading worker-CLI advisories so a pre-agent body still matches."""
    lines = str(text or "").splitlines()
    index = 0
    while index < len(lines) and (
        not lines[index].strip() or _PREAMBLE_RE.match(lines[index].strip())
    ):
        index += 1
    return "\n".join(lines[index:])


def classify_gateway_failure(text: str) -> str | None:
    """Return a known pre-agent gateway failure kind, or None.

    Fails closed: anything this does not positively recognise is treated as
    the worker's own substantive report and charges the card.
    """
    match = GATEWAY_ERROR_RE.fullmatch(strip_preamble(text))
    if not match:
        return None
    try:
        payload = json.loads(match.group(2))
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("message"), str):
        return None
    status = int(match.group(1))
    code = payload.get("code")
    kind = payload.get("type")
    # 503 is Service Unavailable. Every 503 the gateway emits is raised before
    # a model is reached: no eligible bucket member, a full or timed-out
    # capacity queue, a quarantined model claim, or a declaring backend that is
    # down. None of them is the card failing, so all of them are pre-agent.
    if status == 503:
        if kind in ("bucket_no_eligible_member", "model_owner_backend_down"):
            return kind
        if code in ("capacity_exceeded", "queue_timeout"):
            return "capacity_exhausted"
        if kind == "model_claim_quarantined":
            return "backend_claims_quarantined"
        return "gateway_503"
    if status == 404 and code in (404, "404", "not_found", "route_not_found"):
        return "gateway_404"
    if status == 429 and code in (429, "429", "rate_limit", "cooldown"):
        return "gateway_429"
    if status == 502 and code == "invalid_upstream_tool_calls":
        return "invalid_upstream_tool_calls"
    if status == 502 and code in _UPSTREAM_CODES:
        return "upstream_failure"
    message = payload["message"].casefold()
    if status == 400 and (
        "unable to generate parser" in message or "automatic parser generation failed" in message
    ):
        return "upstream_template_rejection"
    if status in (408, 502, 504) and code in _TIMEOUT_CODES:
        return "first_token_timeout"
    return None


#: The wrapper's substring table, for diagnostics that are not a clean
#: status+JSON body (a bare "Connection refused" from the transport itself).
#: ``backend_claims_quarantined`` carried only the hyphenated spelling until
#: 2026-09-18; the gateway has always emitted the quarantine as
#: ``"type":"model_claim_quarantined"`` (skgateway src/proxy/router.mjs,
#: claimQuarantinedResponse), so the pattern never fired. Both spellings are
#: matched now: the real one, and the legacy one an older gateway may still use.
TRANSPORT_PATTERNS = {
    "rate_limited": re.compile(r"(?:\b429\b|rate.?limit)", re.I),
    "model_owner_backend_down": re.compile(r"model_owner_backend_down", re.I),
    "backend_claims_quarantined": re.compile(
        r"backend[-_ ]claims[-_ ]quarantined|model[-_ ]claims?[-_ ]quarantined", re.I
    ),
    "bucket_no_eligible_member": re.compile(r"bucket_no_eligible_member", re.I),
    "capacity_exhausted": re.compile(
        r"\"code\"\s*:\s*\"(?:capacity_exceeded|queue_timeout)\"", re.I
    ),
    "invalid_upstream_tool_calls": re.compile(r"invalid_upstream_tool_calls", re.I),
    "upstream_failure": re.compile(
        r"\"code\"\s*:\s*\"(?:empty_upstream_response|invalid_upstream_completion"
        r"|upstream_unreachable)\"",
        re.I,
    ),
    "first_token_timeout": re.compile(
        r"\"code\"\s*:\s*\"(?:first_token_timeout|gateway_timeout"
        r"|timeout_before_first_token|upstream_timeout)\"",
        re.I,
    ),
    "connection_failure": re.compile(
        r"connection (?:error|failed|failure|refused|reset|timed? ?out)|"
        r"failed to connect|network is unreachable|temporary failure in name resolution",
        re.I,
    ),
    "upstream_template_rejection": re.compile(
        r"unable to generate parser\b|automatic parser generation failed", re.I
    ),
    "gateway_404": re.compile(r"^\s*404:\s*\{", re.M),
    # Ordered last: a 503 whose body names a more specific cause above is
    # reported as that cause, and this catches the rest.
    "gateway_503": re.compile(r"^\s*503:\s*\{", re.M),
}

#: The exit-record vocabulary. The launcher filters worker-exit records on this
#: set, so it must stay exactly the wrapper's pattern keys.
TRANSPORT_FAILURE_CLASSES = frozenset(TRANSPORT_PATTERNS)

#: Structured kinds that are named differently in the exit-record vocabulary.
_STRUCTURED_ALIASES = {"gateway_429": "rate_limited"}


def classify_transport_diagnostic(text: str) -> str | None:
    """Return the allow-listed transport class for a worker diagnostic.

    A whole-report status+JSON body is authoritative, so launcher and wrapper
    agree on it exactly; the substring table is the fallback for transport
    noise that never reached the gateway. Every value this returns is in
    ``TRANSPORT_FAILURE_CLASSES``, which is what the launcher filters on.
    """
    structured = classify_gateway_failure(text)
    if structured is not None:
        kind = _STRUCTURED_ALIASES.get(structured, structured)
        if kind in TRANSPORT_FAILURE_CLASSES:
            return kind
    for kind, pattern in TRANSPORT_PATTERNS.items():
        if pattern.search(text):
            return kind
    return None
