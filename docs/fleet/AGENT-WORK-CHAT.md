# Agent work chat and bounded steering

This protocol gives fleet workers a shared work conversation without turning mail into
an unauthenticated command channel. SKMail is append-only coordination evidence. Chat
messages may notify, ask for help, exchange context, and subscribe to a dependency.
They do not approve work, change card state, run shell, or authorize an external action.

## Startup check-in

Every worker should send one message immediately after its exact claim readback:

```text
skmail send <worker-id> all normal STARTUP-<card-id> \
  "worker=<worker-id> card=<card-id> claim_revision=<revision> host=<host> lane=<lane> \
   model=<model> state=RUNNING; ask for context with HELP-<card-id>"
```

The message must contain no secret, prompt, protected Matter content, or credential.
The worker then emits heartbeat metadata at least every 60 seconds. A heartbeat is
observability, not proof that work is progressing.

## Dependency handoff

Ask another worker to notify you when a dependency reaches a terminal state:

```text
skmail send <worker-id> pi-codex-chiap02-8b355339 fyi DEPENDENCY-WAIT-<card-id> \
  "I depend on card 8b355339. Please send DEPENDENCY-UPDATE when it is DONE, BLOCKED, \
   or requires review. My claim_revision=<revision>."
```

The dependency worker replies with immutable evidence, not an informal claim:

```text
skmail send <worker-id> pi-qwen-chiap08-566f659d fyi DEPENDENCY-UPDATE-8b355339 \
  "card=8b355339 state=REVIEW evidence_sha256=<hash> next=independent-review"
```

Never infer DONE from a chat message alone. Re-read CardStore before acting.

## Help request and broadcast

```text
skmail send <worker-id> all normal HELP-566f659d \
  "topic=skgateway queue telemetry symptom=HTTP-503; observed_at=<time>; \
   evidence=/home/.../evidence.json sha256=<hash>; suggestions welcome; no action requested"
```

Workers with relevant subscriptions may answer. Answers should distinguish observation,
hypothesis, reproduction, and recommendation, and include source or evidence hashes.

## Review coordination

```text
skmail send <producer-id> pi-codex-chiap04-<review-id> normal REVIEW-HANDOFF-<card-id> \
  "candidate_commit=<sha> tree=<sha> changed_paths=<manifest-sha>; please return PASS or FAIL \
   with evidence hash; do not merge or deploy"
```

The reviewer records its verdict through the board protocol as well as SKMail. Mail is
the conversation; CardStore is the workflow authority.

## Allowed automatic acknowledgments

The worker-side acknowledgment service may answer only these subjects:

- `HEARTBEAT-CHECK`
- `WORKER-CHAT-TEST`
- `STATUS-CHECK`

It verifies the exact worker identity and a heartbeat no older than five minutes. It
returns card, claim revision, heartbeat time, and disposition. It never executes mail
body text.

## Bounded steering

Steering is a separate, signed protocol. Ordinary chat may request help but cannot steer
a process. Future control messages must include:

```text
type=PAUSE|STOP|RESUME|ASSIST
worker=<exact-worker-id>
card=<exact-card-id>
claim_revision=<exact-revision>
nonce=<single-use-value>
expires_at=<RFC3339>
requested_by=<authorized-identity>
reason=<short explanation>
```

Only an allow-listed supervisor may apply these messages. `ASSIST` supplies bounded,
read-only context; it is never raw shell, arbitrary stdin, or a replacement prompt.
`STOP` and `PAUSE` preserve logs and release only the matching claim generation.

## Operational examples

| Situation | Subject | Required response |
| --- | --- | --- |
| New worker starts | `STARTUP-<card>` | Publish identity, claim revision, lane, model |
| Need a dependency | `DEPENDENCY-WAIT-<card>` | Name dependency and requested terminal states |
| Dependency changes | `DEPENDENCY-UPDATE-<card>` | State plus evidence hash; re-read board |
| Unexpected failure | `HELP-<card>` | Symptom, timestamp, reproduction, evidence hash |
| Review handoff | `REVIEW-HANDOFF-<card>` | Candidate hashes, scope, requested verdict |
| Liveness probe | `STATUS-CHECK` | Automatic bounded ACK only |
| Human emergency action | `STOP` | Signed, nonce-bound supervisor command |

## Safety rules

1. Mail is never an approval, verdict, claim, dependency completion, or deployment gate.
2. Every work message names a card, repository, or bounded topic.
3. Never put secrets, private keys, passphrases, tokens, or protected data in mail.
4. Use exact identities and claim revisions; reject ambiguous recipients.
5. Preserve raw mail and response hashes; corrections append a new message.
6. Re-read authoritative CardStore, evidence, and process state before mutation.
