# Independent review of GLM nine-worker concurrency audit

Card: `334b8d63`

Source card: `0b07aeeb`

Verdict: `BLOCKED`

Blocked on: `capability`, referent `ac:1`

This review used preserved files, CardStore events, repository objects, GitHub PR metadata, and a provider-free deterministic state model. It did not dispatch GLM, clear the hold, send provider traffic, merge, deploy, restart, mutate gateway or provider configuration, access credentials, or clean up runtime state.

## Ownership and dependency

The folded event streams show this card claimed by `pi-codex-334b8d63`. The latest visible claim revision during review was `7bd66653c30245e69035af5a87ed530d`. Source card `0b07aeeb` has a separate completion event, so the declared dependency was complete before review work began. Lifecycle state was used only for the dependency gate, not as evidence of the source verdict.

## Exact byte reproduction

| Item | Result | Reproduced identity |
| --- | --- | --- |
| Source audit document | PASS | `501f64cf72a03e49da6a633144d62b4bbb9f28750a19510422186690d24f3aea` |
| Source verdict JSON | PASS | `3e70b68274d269346ececcbedb8ebfdacdd40befa6c7aaee22db70522a1c7aad` |
| Active hold | PASS, still active | `bc5ccfd3fae165bacec626683c485fd2496e63e72032c8795c5f2fff3198dc05` |
| chiap01 429 log for `534fffec` | PASS | `385add1adc8bd5307335621b32a5f514f1df4da76ffaa416280d03faab390ab7` |
| chiap03 429 log for `910da7a0` | BLOCKED, bytes absent on this review host | expected `2c1f8d17ed4bc68a6e53e18eaef6aa15254c76a3c347be70fc6d5cf25401bd25` |
| chiap03 429 log for `11fae2b7` | BLOCKED, bytes absent on this review host | expected `562c37aa86a362908e6e00de0349f6f1f4ebcc14f9551bb70e9180f4849184b9` |
| Source commit | PASS | full commit `ea07b4c86dc0bbc878fed91633826d58827b9a03` |
| PR 246 | PASS | open PR head is exactly `ea07b4c86dc0bbc878fed91633826d58827b9a03` |

The source audit in the commit and the shared evidence copy compare byte for byte. Its SHA-256 is the expected audit hash. The fetched commit has parent `15382c7c6d311851f8da124728e3ee303e5994a2`, one added file, and commit object payload SHA-256 `31c058d00a4bca5dc93a293f55ae48d7b93c409c0c12319b9454c4ae208fc7bf`. The committed audit file is exactly the shared audit artifact.

PR 246 is open from `chore/0b07aeeb-glm-concurrency-audit` to `main`, contains one commit, and adds only `docs/fleet/GLM-NINE-WORKER-CONCURRENCY-AUDIT-0b07aeeb.md`. A captured `gh pr diff --patch` byte stream had SHA-256 `2c8a2055857d1c375092cd6a5ee06b9a2fbcc017d97f23061e4739d5855fa2e8` and length 9087 bytes. The remote branch and `refs/pull/246/head` both resolved to the full source commit.

The two chiap03 worker logs were not present in the synchronized evidence area or on this host. An exhaustive local file hash scan did not find either expected digest. The source verdict only records their host-local paths and expected hashes. Fetching them from another host would be an external action prohibited by this card. Therefore acceptance criterion 1 cannot be completed with this agent's permitted local tool reach. This is the machine-readable blocking reason for the verdict.

## Independent causal review

The preserved selector bytes take one local tmux snapshot, classify local names by prefix, and calculate each lane's free slots as local target minus local busy count. The GLM target defaults to three. Immediately before each launch, `_still_assignable` checks card state, but there is no aggregate GLM capacity reservation or fleet-wide compare-and-swap in the launch path.

Four preserved action receipts independently reproduce the concurrent behavior:

| Receipt | SHA-256 | Relevant observation |
| --- | --- | --- |
| `20260828T105727Z/actions.log` | `e3e38636b0695a07baf104b7e562551414b2922f34b957b596c9ceab31efb354` | chiap03 saw GLM 0 of 3 and launched `11fae2b7` |
| `20260828T110000Z/actions.log` | `2bcec98055dd0288b8f3b875a51ab152efc59a287c3443bdc0b53867f0b8a7ae` | chiap01 saw GLM 0 of 3 and launched three GLM workers |
| `20260828T110121Z/actions.log` | `f8bad4a2a5c11e610f2c219d396e780b99a57afd79497291a846086e3cc8ef9b` | chiap02 saw GLM 0 of 3 and launched `103edd61` |
| `20260828T110212Z/actions.log` | `cba11ef56f206a4244b7c4a3fde0d425021c7ca4f8659a52e1d503e32ecf8f68` | chiap03 saw GLM 2 of 3 and launched `597cf198` |

This proves independent host-local admission and concurrent selectors. It does not by itself prove more than nine simultaneous tmux workers in the observed wave. With exactly three correctly counted hosts, three local slots each sum to nine. However, the selector's own rotation-host list contains five hosts, while every host uses the same local target of three. Host-local invariants therefore permit an aggregate of 15 if all five selectors run. No shared counter constrains the sum to nine.

Stale state provides a separate over-admission route even with only three hosts. A selector snapshots tmux before launching. A session can disappear after issuing a request while that request remains in flight. The next snapshot sees a free local worker slot and admits a replacement. The stale request plus nine current workers can yield ten concurrent requests. The source correctly distinguishes gateway `peakActive` from a tmux roster: jarvis's preserved receipt reports z.ai `max=10`, `queued=0`, `peakActive=10`, and `totalProcessed=10328`. That is proof of ten overlapping requests during the gateway boot, not proof of ten simultaneous GLM tmux sessions.

A deterministic, zero-network state model gives the following results:

1. Five independent host-local counters, each bounded at three, grant 15 aggregate worker admissions. The tenth through fifteenth admissions are locally valid while violating a fleet ceiling of nine.
2. Three hosts at three workers each remain at nine workers, but one request surviving session exit followed by one replacement produces ten active requests.
3. Two concurrent selectors that read the same authoritative count of eight and update without compare-and-swap can each grant, producing ten. A single serialized compare-and-swap grants one and rejects one.
4. Treating malformed, missing, stale, conflicting, or incompletely synchronized state as unavailable causes zero grants. Treating it as zero capacity use recreates over-admission.

The primary design defect is absence of authoritative aggregate admission. The observed `peakActive=10` is consistent with stale or overlapping request lifetime and is not enough to attribute the tenth request specifically to a tenth tmux worker. Backend cooldown is a consequence and active safety condition, not the admission root cause.

## Repair proposal review

The source proposal contains important correct requirements:

* reserve before card claim and launch;
* append serializer-produced lease events and parse existing JSONL before append;
* keep lease evidence separate from structural CardStore events;
* count aggregate worker leases with an exact ceiling of nine;
* separately budget every request, including retry and in-flight lifetime;
* fail closed while the current hold is active and on conflicting generations;
* test with a fake clock, concurrent selectors, stale sessions, duplicates, reordered events, conflicts, a preserved 429, and socket or HTTP failures;
* rollback by append-only abort and release events while retaining the hold;
* avoid gateway and provider configuration mutation.

The proposal is not yet an implementable repair candidate and must not clear the hold:

1. It says to use "one cross-host lock service or atomic compare-and-swap" but does not select or identify one authoritative service, key, generation source, or failure model. A Syncthing-replicated per-host ledger is eventually consistent and cannot itself be the lock or authoritative counter.
2. It does not define fail-closed freshness bounds for host liveness, lease expiry, clock skew, lock-service partitions, incomplete replicas, or unavailable queue samples. The deterministic test list asks for some of these outcomes but is not executable policy.
3. It specifies exact ceiling nine in prose, but provides no candidate implementation or simulation bytes that enforce it.
4. It lists deterministic simulation requirements but publishes no simulator, frozen input, transcript, or expected-output bytes.
5. It refers to "previously hashed selector bytes" for rollback but does not identify the selector path and hash in the rollback section or publish a rollback patch, executable, feature-flag bytes, or expected restored-tree hash.
6. It correctly prohibits gateway or provider mutation, but a later implementation must prove this with a scoped diff and zero-network test, not only prose.

Actionable repair: publish a separate candidate that chooses one linearizable admission authority, defines a single generation and aggregate worker and request counters, gives explicit fail-closed freshness and partition semantics, includes executable deterministic tests with frozen expected bytes, and includes an exact append-only rollback artifact with hashes. Keep the current hold active until that candidate passes a distinct review.

## Safety and repository decision

This review is a documentation-only repository change. It does not implement, enable, or exercise fleet admission. No gateway, provider, timer, service, live configuration, or credential bytes were changed. The active hold remains authoritative.
