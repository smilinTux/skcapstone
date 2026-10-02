# c1a30169: worker liveness generation fence

Claim: `codex-liveness-generation-c1a30169`,
`f8fd7126df5d4aab95c55d7af61e66cb`.
Base: `eefdf916b1d4c1ba58428e092727b07d86ee2756`.

## Failure and correction

The collector joined old and current beat files to the same reused unit by
card ID alone. A stale classification still retained an active projection,
then the lifecycle publisher wrote an unguarded link. Alternating claims
changed the review card revision during a legitimate guarded handoff.

New launch payloads include the wrapper PID and inherited systemd invocation.
The collector verifies both against retained unit properties and exact wrapper
card, owner and claim arguments. It reads the current claim once, rejects
unbound legacy beats, and does not publish ambiguous or stale projections.

`coord worker-liveness` rechecks the exact worker owner and claim under the
existing CardStore mutation lock, then checks the process generation before
appending the native link with the honest observer identity. Identical state
is a no-op. Native source revisions still include liveness; no evidence or
review gate was weakened. Existing active workers and their claims are untouched.

## Validation

The focused suite covers 136 tests: liveness classification and retirement,
reused units with old/new beats, missing generation fields, PID/invocation and
wrapper argument changes, stale claims, true concurrent CardStore lock
contention, valid active and terminal publication, native CLI attribution and
idempotence, coreless mutation rejection, worker launch and seat boundaries.
It executes the actual generated heartbeat shell without Pi and performs a
real installed guarded native handoff on an isolated board while current and
stale liveness observations run concurrently. No live board mutation occurs.

Exact command and final results are retained in the private candidate packet:

```text
PYTHONPATH=src pytest -q tests/test_worker_liveness.py tests/test_worker_liveness_runtime.py tests/test_liveness_generation.py tests/test_skfleet_worker_cgroup.py tests/test_cardstore_mutation_guards.py tests/fleet/test_seat_boundaries.py
```

Ruff and Black cover new modules and changed liveness/runtime test modules.
The large preexisting rotator and authority CLI are not broadly reformatted.

## Installation and rollback boundary

Root owns independent review and installation. New module files are
`fleet/liveness_publication.py` and `cli/coord_liveness.py`; changed consumer
files are `fleet/worker_liveness.py` and `fleet/worker_liveness_runtime.py`.
The rotator changes one heartbeat payload line. `cli/coord.py` changes only
registration: preserve installed authority guarded extensions when applying
those two lines instead of blindly replacing the installed authority CLI.

Before rollout, qualify actual systemd ExecStart serialization with a
synthetic wrapper unit. Test fixtures exercise realistic retained properties,
real Bash and real native CardStore guards, but are not a live unit trial.
Legacy beats lacking PID/invocation are intentionally ignored until normal
fresh launch. Do not restart active workers just to populate these fields.
Back up exact installed bytes, install dependencies before consumers, and
restore those bytes/remove added modules for rollback. No source, quota,
provider, token, protected data or external-action change is included.
