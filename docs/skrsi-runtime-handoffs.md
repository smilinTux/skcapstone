# SKRSI runtime handoffs

Card `c80e3512` repairs the first-wave handoff gap identified by broad review
`5a71c2e4`. The production entrypoint is `skcapstone skrsi run`. It dispatches
through `SKRSIRuntime`, which consumes `FIRST_WAVE_HANDOFFS` in `HandoffRuntime`.
The component modules remain library primitives. Existing fleet timers are not
reconfigured by this source change. Dashboard HTTP integration is card
`ae365315`; the bounded projection adapter here is available to that owner.

| Boundary | Producer | Consumer | Recovery owner | Runtime method |
| --- | --- | --- | --- | --- |
| CardStore ingestion | CardStore | SKRSI | atlas | collect |
| Fleet ingestion | SKFleet | SKRSI | niobe | collect |
| Mail envelope ingestion | SKMail | SKRSI | mero | collect |
| Target registration | registry | collector | atlas | register |
| Cohort evaluation | collector | evaluator | atlas | evaluate |
| Evaluation handoff | evaluator | controller | atlas | transition |
| Controller projection | controller | dashboard-projection | tank | project |
| Query delivery | dashboard-projection | query-delivery | tank | deliver |
| Canary review request | canary | Link | link | canary_review |
| Canonical review materialization | Link | Seraph | link | review |

Every contract specifies a natural key, queue bound of 16, consumer deadline of
2 seconds, at most two retries, exponential backoff starting at 50 milliseconds,
one recovery owner, and notification-only escalation to Mero. Database lock
waits have a separate finite 100 millisecond bound. The executor also has four
workers and a queue of at most 16 waiting jobs. It never creates a thread per
request. Collector batch size consumes the source contract's queue bound.

The runtime database must be on host-local storage and shared by all workers
for that authority on that host. Do not place SQLite databases in Syncthing.
SQLite uniqueness fences concurrent runtime instances by boundary and natural
key. The fingerprint binds input metadata, authority revision, and contract.
CardStore retains its existing locks and revision checks across card operations;
the runtime database does not claim to replace distributed authority.

## Invocation and machine evidence

An existing claimed software lifecycle card must bind the exact request file
SHA256 as `runtime_input_sha256` and the owning quality evaluator's decision as
`quality_gate=PASS`. The runtime does not generate these qualifications for
itself. Missing evidence fails closed. This is a machine evidence requirement,
not an additional human approval or an instruction to label untested work PASS.

```text
skcapstone skrsi run --home AGENT_ROOT --state-dir HOST_LOCAL_STATE \
  --card CARD_ID --agent CLAIM_OWNER --request QUALIFIED_REQUEST_JSON
```

Requests select one operation: `register`, `collect`, `evaluate`, `transition`,
`project`, `deliver`, `canary-review`, or `review`. `dispatch` in
`cli/skrsi_cmd.py` defines the typed component fields for each operation.
Controller mutation, projection, and review source IDs must equal the claimed
card. A projection also binds `experiment_revision`. Input is limited to 1 MB.
SKLegal cards are excluded. There is no merge, deployment, reroute, or actuation
operation. The CLI rechecks claim ownership, dependency completion, exact input
qualification, and current card generation before work and before success.

Services embedding `SKRSIRuntime` must supply trusted, bounded authority,
authorization, and quality adapters. These callbacks are not request fields.
The authority revision must cover the inputs owned by that service. Expiry,
controller revision, source generation, reviewer independence, and unclaimed
review state have additional component-specific checks, including on replay.

## Replay, timeout, and terminal evidence

The executor stores a canonical terminal receipt and result with SHA256 hashes.
The receipt includes the complete contract, input and authority hash, recovery
owner, timestamp, outcome, and a durable notification envelope. A database
trigger rejects replacement of a terminal receipt. Result and receipt hashes
are checked on read. Errors retain only bounded classifications, not exception
messages or protected payloads. CLI failures emit the notification envelopes
on stderr for the owning orchestrator to deliver to its mailbox. This does not
claim that SKMail has already delivered them.

Only `RetryBeforeEffectError`, an explicit consumer certification that no effect
occurred, permits retry. Each retry rechecks live gates and observes backoff and
the same deadline. An unknown failure or timeout is never retried automatically.
Timeout leaves occupancy reserved until the original worker exits. Late results
cannot replace terminal evidence. A timeout records an unknown attempt count
when the caller cannot observe how far the worker progressed.

A callback already running cannot be killed safely by Python threads. It may
finish a metadata operation after timeout. Its native idempotency and revision
fences remain mandatory, and no late result authorizes downstream work. After a
process crash, unresolved occupancy remains fail-closed until the named recovery
owner reconciles authoritative receipts. Age alone is not proof of abandonment.
This favors no duplicate effect over automatic reuse of an uncertain slot.

Review replay returns a materialization receipt with `launch_authority=false`.
It is never reusable launch permission. Link's real reconciler creates one
canonical unclaimed review card and one recommendation. Existing fleet claim
and launch authorization must run against current authority. Changed reviewer
identity collides with the original runtime key; an already claimed or terminal
review cannot pass replay freshness checks.

## Verification and rollback

The integration test drives every boundary through real registry, collector,
evaluator, controller, projection, and Link APIs and requires terminal receipts
for every contract. Separate tests cover saturation, concurrent replay, restart,
late timeout results, live gate changes, safe retry, immutable evidence, native
Link reconciliation, reviewer collision, and card-authorized CLI dispatch.

No database is installed and no live work is executed by publishing this PR.
Before deployment, rollback is discarding the candidate. After installation,
revert the source commit through the normal lifecycle and preserve the host-local
database and all CardStore evidence. Never delete uncertain work records to
reclaim capacity.
