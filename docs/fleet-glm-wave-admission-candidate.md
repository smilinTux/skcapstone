# GLM nine-worker wave admission candidate

Cards: `9ca6efd6`, authority repair `35e539d1`

Status: repaired no-action candidate for distinct review. This document is not authorization to cut over, clear a hold, or launch a worker.

## Evidence reproduced before design

The candidate was designed only after locally reproducing the shared preserved evidence:

| Evidence | Reproduced SHA-256 |
| --- | --- |
| RCA `0b07aeeb`, `GLM-NINE-WORKER-CONCURRENCY-AUDIT-0b07aeeb.md` | `501f64cf72a03e49da6a633144d62b4bbb9f28750a19510422186690d24f3aea` |
| Blocked review `334b8d63`, `BLOCKED.json` | `c31e9bc703e064cf7bd64984711befdff4e961fe34f2ada702068ab348b6deca` |
| Active dispatch hold | `bc5ccfd3fae165bacec626683c485fd2496e63e72032c8795c5f2fff3198dc05` |
| chiap01 `534fffec-20260828T110000Z.log` | `385add1adc8bd5307335621b32a5f514f1df4da76ffaa416280d03faab390ab7` |
| chiap03 `910da7a0-20260828T110155Z.log` | `2c1f8d17ed4bc68a6e53e18eaef6aa15254c76a3c347be70fc6d5cf25401bd25` |
| chiap03 `11fae2b7-20260828T105727Z.log` | `562c37aa86a362908e6e00de0349f6f1f4ebcc14f9551bb70e9180f4849184b9` |
| Shared source manifest | `b9e8e33b7bf9a79e25a7c4f8595e5d1616d50ebbd067084db1bcd20f43e3cabb` |

The RCA establishes that host-local selector decisions cannot enforce a fleet-wide limit and that request `peakActive=10` is not a worker count. The blocked review identifies three missing deliverables repaired here: a selected authority and lock, executable deterministic tests, and exact rollback bytes.

## Candidate protocol

`src/skcapstone/fleet/glm_admission.py` is deliberately not connected to a CLI, timer, selector, launcher, gateway, or provider. It can only publish a reservation ledger.

1. The operating system hostname is normalized to lowercase without a trailing dot and must equal `chiap08`. Callers cannot supply authority identity. Host selectors have no dispatch entry point.
2. Compile-time paths `/var/lib/skcapstone-local/glm-admission/admission.lock` and `/var/lib/skcapstone-local/glm-admission/generation.json` are outside Syncthing state. Callers cannot override either path.
3. The authority directory must be a physical, owner-controlled mode-0700 directory. Lock and ledger opens reject symlinks, non-regular files, extra hard links, foreign ownership, and modes other than 0600. The existing ledger must be present, current, schema-exact, owned by chiap08, and internally valid.
4. A chiap08-local advisory `flock` covers ledger read, evidence validation, monotonicity validation, and replacement. Before atomic replace, the writer proves the ledger inode is the one validated under the lock. Missing, malformed, stale, conflicting, live, replaced, or non-monotonic state denies admission.
5. A complete generation can advance by exactly one. A live generation is never refilled.
6. One wave has exactly nine bindings, exactly three for each of chiap01, chiap02, and chiap03. Card, host-distinct agent, session, claim, and absolute workspace identities must each be nonempty and distinct.
7. All three hosts must be reachable, report zero `glm-auto` sessions, and have fresh timestamps.
8. Two frozen, read-only zai samples must be exactly five seconds apart, fresh, and both report `active=0, queued=0`.
9. The exact hold generation and SHA-256 are checked twice under the lock. An active hold or a changed hold denies admission. Thus the currently active hold causes zero publication and zero dispatch.
10. Publication serializes canonical JSON, writes an owner-only temporary regular file in the ledger directory, fsyncs it, atomically renames it over the verified ledger, and fsyncs the directory.

The module contains no network client, inference request, provider request, subprocess launch, tmux operation, or dispatch callback.

## Deterministic qualification

`tests/test_glm_admission.py` replaces socket construction and connection with immediate failures. Frozen inputs and a fixed UTC clock cover:

- concurrent admission, with exactly one whole wave published;
- stale sessions and stale in-flight queue observations;
- missing, malformed, stale, and non-monotonic ledgers;
- crash before rename, preserving the old generation;
- crash after rename, exposing one complete live generation and denying refill;
- hold change while the lock is held;
- partial host reachability;
- tenth-worker denial and duplicate custody denial;
- physical chiap03 spoof denial and active-hold zero publication;
- impossible caller authority, ledger, and lock overrides;
- symlink and non-regular lock and ledger denial.

## Disabled consumer and launcher candidate

`skfleet-glm-consumer` is now an installed console entry point to
`src/skcapstone/fleet/glm_consumer.py`. It remains disabled when the fixed
owner-only `/var/lib/skcapstone-local/glm-admission/consumer.enabled.json`
file is absent. It accepts no path or authority arguments, requires the
physical hostname to be exactly `chiap08`, and reads the three fixed
`chiap01`/`chiap02`/`chiap03` snapshot files. Snapshot files are read-only,
owner-mode 0600, non-symlink regular files with strict schemas. The consumer
contains no provider or inference client.

The consumer selects only dependency-verdict `PASS`, unclaimed, non-human
cards, exactly three assigned by each snapshot host. Recorded claims are
excluded even when their timestamp is old; stale ownership is never stolen.
It obtains claims through supported `skcapstone coord claim` and releases
through supported `skcapstone coord release-claim`. Derived worker agent,
session, claim, host, card, and worktree bindings are all distinct before the
unchanged `admit_wave` reservation is called.

Launch is delegated only to the fixed local
`/usr/local/libexec/skcapstone-glm-worker-control` contract: stage nine
non-running workers with fixed transcript paths, then `commit-wave` once.
Anything other than all nine expected live session identities triggers
`stop-wave` and releases all supported claims. Stop never asks the controller
to remove transcripts. Any observed 429, or positive queue in both consecutive
samples, stops before claims or dispatch. The hold is read-only and is passed
to reviewed admission; no hold-clear operation exists. Neither the worker
controller nor enablement/state directories are created by this candidate.

Focused zero-provider tests use in-memory claim and launch adapters while
forbidding socket/process access. They cover disabled default, host/path spoof
rejection, cardinality and distribution, duplicate custody, stale claims,
non-PASS and human exclusions, queue/429 stop, partial rollback, transcript
preservation, supported release, active hold, unsafe files, and malformed
snapshots.

## No-action cutover packet

No cutover is requested by this card. A later, separately authorized and reviewed change would have to satisfy every item below before any integration work:

1. Obtain a distinct PASS review of the exact commit and shared evidence hashes.
2. Keep the current hold active. Do not treat candidate review as hold clearance.
3. Install the reviewed bytes only on chiap08 under a separate approved deployment change.
4. Keep chiap01, chiap02, and chiap03 selectors incapable of GLM dispatch.
5. Provision a chiap08-local lock and genesis ledger using reviewed exact bytes and restrictive ownership. Never bootstrap from a missing ledger.
6. Bind read-only host and zai samplers that preserve the timestamp and identity contract. Do not add inference probes.
7. Run the deterministic zero-network suite against installed bytes.
8. Require a separate human-authorized hold generation before an inactive hold can be observed. This packet cannot provide that authorization.
9. Reserve the entire 3-by-3 wave in one generation. If any validation fails, dispatch zero workers.
10. Treat any 429, identity conflict, unreachable host, stale sample, malformed ledger, or process uncertainty as a new fail-closed stop condition.

## Rollback

The exact reverse patch and SHA-256 are published with the card evidence after the candidate commit is created. Applying that reverse patch to the exact candidate commit removes the module, tests, and this document. Since this candidate is not wired into runtime selection, rollback requires no gateway or provider mutation and leaves the active hold unchanged.
