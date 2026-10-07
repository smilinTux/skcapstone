# Bounded Pi gateway request recovery

Fleet Pi workers already load `pi-cardstore-guard.mjs`. Its session-start hook
wraps only the actual `skgateway` provider through Pi's public provider registry.
The existing models, authentication, base URL and request body remain unchanged.
The helper ships beside the guard through the normal package script manifest;
no local extension installation or worker restart is part of this change.

Only HTTP 503 with error type `bucket_no_eligible_member`, and HTTP 504, retry.
There are at most four HTTP attempts with 30, 60 and 120 second backoff, inside
a six-minute total request budget. Slow attempts consume that budget; fewer
than four may fit. Cancellation remains cancellation. Every client 4xx,
unrelated or malformed 503, and partial streamed response stays terminal.
There is no alternate provider or privacy-scope fallback.

Each attempt and retry is recorded by Pi's `appendEntry` API as
`skfleet.gateway_transport_retry`: provider/model identity, status, attempt,
elapsed time, delay and bounded error classification. No prompt, headers,
credentials or response content are recorded. The session manager supplies
entry timestamps and session attribution.

Nested provider retries are disabled for these gateway calls. After the audited
transport reaches its terminal decision, the stream retains an error result
with structured `gatewayError` metadata and stable error text. Pi's separate
text-matching outer retry cannot start another four-attempt round. Native
context-413 custody proof accepts this structured metadata alongside legacy
413 text, while preserving its exact hash, invocation, provider/model,
timestamp, claim and byte-limit checks.

This prevents a new worker request from terminating during a short gateway
breaker interval. It does not grant permission to replay an already stopped
generation, reset a consumed continuation, loosen admission, or publish a
worker verdict. Existing failed source custody still needs governed recovery.

Deploy only from reviewed merged main. Rollback is a main revert and ordinary
rollout. No candidate is installed into a production venv during validation.
