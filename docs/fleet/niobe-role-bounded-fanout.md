# Niobe role-bounded fan-out

Link and Mero may request lifecycle child work, but they do not claim or launch
it. Every request is an append-only `skfleet.niobe-fanout-request/v1` event on
the target card. Niobe is the sole production actuator and processes a request
through the existing fleet selector, workspace materializer, exact CardStore
claim, and detached worker launcher.

A request is accepted only when all of these statements are true:

1. The requester is resolved from the active Link or Mero systemd unit, matching
   control-plane host, invocation, and cgroup evidence. Caller-supplied seat text
   is never authority.
2. The route is allowed by that seat's `SEAT_CHILD_ROUTES` entry.
3. The target card has the exact `fanout-scope-<route>` label.
4. The source head is a full lowercase Git SHA.
5. The model is `sk-codex-mid`.
6. A board-wide CardStore mutation lock atomically reserves the source head
   before materialization, so concurrent requests on different cards produce
   exactly one winner.

This makes effective authority the intersection of the requesting role and the
card scope. Jarvis has no request path and is not a recurring lifecycle seat.

## Receipts and recovery

Niobe first verifies its active Casey decision, host, systemd invocation, and
service cgroup. It then appends immutable `skfleet.niobe-fanout-receipt/v1`
events for source-head reservation, workspace materialization, exact claim,
launch, launch failure, occupancy, stop, release, reassignment, and terminal
retirement. Runtime receipts include the exact request, claim owner, and claim
revision. Receipt identities are deterministic, so repeating a cycle cannot
create a second logical transition.

Each selector cycle reconciles cards that contain fan-out events against fresh
CardStore claim state and live worker units. A missing process with a current
claim records `stopped`. No current claim records `released`. A changed claim
generation records `reassigned`. A terminal card records `retired`. These are
observations, not permission to release a claim. Existing exact-generation
release and worker shutdown fences remain authoritative.

If a claimed receipt cannot be persisted, Niobe releases only the exact claim
generation and does not launch. If a launch receipt cannot be persisted after
the detached unit exists, Niobe leaves the exact unit and claim intact. The
next reconciliation cycle recovers its occupancy receipt from process and
claim truth. Reconciliation considers only receipts carrying the latest exact
request ID and source head, and claim-bearing recovery remains fenced to its
claim generation. The launcher never adds a broad worker stop surface.

## Producer example

```python
from pathlib import Path

from skcapstone.niobe_fanout import LifecycleFanoutRequest, submit_fanout_request

submit_fanout_request(
    Path.home() / ".skcapstone",
    LifecycleFanoutRequest(
        card_id="deadbeef",
        source_head="0123456789abcdef0123456789abcdef01234567",
        requester="link",
        route="integration",
    ),
)
```

The target card must already carry `fanout-scope-integration`. Submission only
records advice. The next activated Niobe cycle performs all mutation.

## Rollback

Disable the Niobe live timer and restore the previously reviewed package,
dispatcher script, and service bytes. Do not delete request or receipt events.
They remain historical evidence and source-head replay fences.
