# GLM nine-worker concurrency audit

Card: `0b07aeeb`

Verdict: `PASS_FOR_REVIEW`

This is a read-only root-cause audit and bounded repair proposal. It does not clear the active GLM hold, dispatch a worker, send inference or canary traffic, or change gateway or provider configuration.

## Preserved evidence

The following hashes were reproduced from preserved bytes. The worker logs are host-local, while the hold is shared.

| Artifact | Host | SHA-256 |
| --- | --- | --- |
| `/home/skuser01/.skcapstone/fleet/logs/534fffec-20260828T110000Z.log` | chiap01 | `385add1adc8bd5307335621b32a5f514f1df4da76ffaa416280d03faab390ab7` |
| `/home/skuser01/.skcapstone/fleet/logs/910da7a0-20260828T110155Z.log` | chiap03 | `2c1f8d17ed4bc68a6e53e18eaef6aa15254c76a3c347be70fc6d5cf25401bd25` |
| `/home/skuser01/.skcapstone/fleet/logs/11fae2b7-20260828T105727Z.log` | chiap03 | `562c37aa86a362908e6e00de0349f6f1f4ebcc14f9551bb70e9180f4849184b9` |
| `/home/skuser01/.skcapstone/evidence/fleet-glm-dispatch-hold.json` | shared, reproduced on chiap01, chiap02, and chiap03 | `bc5ccfd3fae165bacec626683c485fd2496e63e72032c8795c5f2fff3198dc05` |

All three logs contain the same attributable response class, `429` with `rate_limited_all_candidates`, followed by release of the card claim. The hold records `active=true`, reason `zai_429_backend_cooldown`, those three log hashes, and manual clearance only after cooldown and fresh read-only queue samples.

## Root cause

The planned limit was nine workers, expressed as three host-local `glm-auto-` sessions on each of chiap01, chiap02, and chiap03. The selector had only a host-local `rotate.lock`, took one host-local tmux snapshot, and calculated `free = 3 - local_glm_sessions`. It did not acquire a shared GLM lease before claim and launch. The card hash partition prevented duplicate cards, but it did not serialize aggregate GLM capacity.

Preserved action receipts show overlapping refill decisions:

1. `10:57:27Z`, chiap03 saw `glm=0/3` and launched `11fae2b7`.
2. `11:00:00Z`, chiap01 independently saw `glm=0/3` and launched `00fd934c`, `17119fee`, and `534fffec`.
3. `11:01:21Z`, chiap02 independently saw `glm=0/3` and launched `103edd61`.
4. `11:02:12Z`, chiap03 saw only its local `glm=2/3` and launched `597cf198`.

This sequence proves independent host-local admission, not a single fleet-wide nine-slot admission decision. The earlier shared action store also contains Syncthing conflict copies for equal timestamp directories, which confirms concurrent writers and makes an action directory unsuitable as a lock.

The gateway receipt preserved in jarvis skmail at `11:11:57Z` reports z.ai `max=10`, `queued=0`, `peakActive=10`, and `totalProcessed=10328`. Gateway `peakActive` counts concurrent requests, not tmux sessions, and it is a boot-lifetime high-water mark. Therefore it does not prove that ten GLM tmux sessions existed at one instant. It does prove that ten z.ai requests overlapped at least once. A ninth worker ceiling cannot guarantee eight or nine requests because a worker or coordinator may overlap a prior request, retry, or auxiliary request.

### Alternatives distinguished

| Candidate cause | Finding |
| --- | --- |
| Selector race | Proven primary control defect. Admission and locking were host-local, while the safety ceiling was fleet-wide. |
| Stale sessions | Contributory risk, not proven as the direct tenth request. Each host counted only local names from one pre-launch snapshot. Shared liveness was published after launches for reaping, but was not used as an atomic admission ledger. A terminating session or in-flight request can outlive the snapshot boundary. |
| Gateway accounting | Explains the apparent mismatch between a nine-session plan and `peakActive=10`. `peakActive` is concurrent requests and a historical high-water mark, not a worker roster. It remains valid evidence of no spare request headroom at gateway max 10. |
| Backend cooldown | Consequence and independent safety state, not the selector root cause. Three preserved `rate_limited_all_candidates` responses prove provider-side throttling. Queue depth remained zero, so the gateway queue was not saturated. Cooldown must remain authoritative until separately cleared. |

## Bounded append-only repair proposal

Implement this only in a new repair card after independent review. Do not edit the current hold.

1. Add an append-only shared lease ledger under `~/.skcapstone/evidence/fleet-glm-leases/events/<host>.jsonl`. Each event is serializer-produced JSON with schema, event ID, UTC timestamp, host, card, agent, lease ID, kind (`acquire`, `started`, `release`, `abort`, or `expire`), observed hold hash, and prior generation. Parse every existing line before append.
2. Serialize admission with one cross-host lock service or atomic compare-and-swap. Syncthing files and host-local `flock` are not distributed locks. If no authoritative compare-and-swap is available, fail closed and keep the hold.
3. Fold leases by lease ID. Under the lock, reject acquisition unless the exact hold is inactive by separately authorized manual clearance, all existing unexpired leases are distinct and live, and aggregate GLM leases are below nine. Reserve before card claim and tmux launch. Append `abort` and release the claim on any failure. Append `release` after process exit.
4. Use a two-level ceiling: nine leases and a request budget no greater than nine. Before launch, require read-only queue evidence with `active + reserved_request_leases <= 9`, `queued=0` in two samples, and no active backend cooldown. A worker must hold one request lease for every z.ai request, including retries. Do not retry a 429 within the same lease.
5. Ramp `1, 1, 1`, then `2, 2, 2`, then `3, 3, 3`. Advance only after the prior stage has two clean queue samples and no 429. Any 429 appends a new hold event and prevents further lease acquisition. The existing hold remains untouched by this proposal.
6. Keep structural card events separate from evidence outcomes. Lease events are operational evidence and must not imply PASS or lifecycle completion.

## Deterministic simulation requirements

A repair candidate must pass a provider-free simulator using frozen inputs and a fake clock:

1. Run three selectors concurrently from identical zero-session snapshots. Exactly nine unique aggregate leases may be granted and the tenth must fail closed.
2. Pause a selector after reservation, expire it with the fake clock, and prove no replacement is granted before deterministic expiry and fold.
3. Simulate a tmux process exiting while one request remains in flight. The request lease must continue to occupy capacity until its terminal event.
4. Simulate one worker attempting two overlapping requests. The second request must be rejected while nine request leases are active.
5. Replay duplicate, reordered, and Syncthing-conflict event bytes. Folding must be idempotent by event ID and fail closed on conflicting lease generations.
6. Inject a preserved 429 response. No later acquisition may succeed while the resulting hold is active.
7. Assert every JSONL line parses after each append and that structural CardStore events and evidence events remain separate.
8. Assert zero network calls by replacing socket creation and HTTP clients with test failures.

## Rollback

Rollback is append-only:

1. Disable the candidate admission path through its own versioned feature flag or remove the candidate executable from timer selection. Do not alter gateway or provider configuration.
2. Append `abort` for reservations that never started and `release` for terminated workers. Never delete or rewrite lease events.
3. Restore the previously hashed selector bytes only after stopping its timer invocation through an authorized change. Keep the current GLM hold active.
4. Re-run the deterministic simulation against the restored selector and confirm it performs no GLM admission while the hold is active.
5. Preserve the candidate, rollback receipt, simulation transcript, and their SHA-256 hashes for independent review.

## Repository decision

This audit document is the repository change. It intentionally contains no executable fleet, gateway, provider, timer, or configuration mutation. The operational candidate is deferred to a separately claimed repair card after review.
