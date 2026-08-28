# Review c7641b7f: GLM central wave admission candidate

Verdict: `BLOCKED`

Source card: `9ca6efd6`

Reviewed PR: <https://github.com/smilinTux/skcapstone/pull/257>

Reviewed commit: `3d61ee417664b7e834257eb6eb5da474b4df1cf4`

## Reproduced identity and evidence

The independent chiap03 review reproduced:

- `MANIFEST.json` SHA-256 `8543b667e300f1b2436ec4b1b3be3baa644df8c1b6868864c9b6136d9a128182`
- candidate patch SHA-256 `eac0e1d3ed16bbfb3fa54ab8782faf87acfdf8e837bc191695baf4ca8f376b9c`
- rollback patch SHA-256 `553e8e6bfeab1031961d5f7bc7525b460aa51ddd42ba9c4b2ea4957d2bcee9dd`
- open PR 257 head commit `3d61ee417664b7e834257eb6eb5da474b4df1cf4`
- RCA SHA-256 `501f64cf72a03e49da6a633144d62b4bbb9f28750a19510422186690d24f3aea`
- blocked review SHA-256 `c31e9bc703e064cf7bd64984711befdff4e961fe34f2ada702068ab348b6deca`
- active hold SHA-256 `bc5ccfd3fae165bacec626683c485fd2496e63e72032c8795c5f2fff3198dc05`
- source manifest SHA-256 `b9e8e33b7bf9a79e25a7c4f8595e5d1616d50ebbd067084db1bcd20f43e3cabb`
- all three preserved 429 logs at their recorded hashes

Every file listed by `SHA256SUMS` passed `sha256sum -c`. The three 429 logs each contain `rate_limited_all_candidates` with response code 429.

## Independent qualification

`python3 -m pytest -q tests/test_glm_admission.py` completed with 11 passed tests on chiap03. Static inspection and those tests confirm the intended same-lock behavior for:

- an exact nine-worker 3-by-3 wave and tenth denial
- no refill from a live generation
- two drained queue samples exactly five seconds apart
- exact hold generation and hash binding checked twice
- distinct card, agent, session, claim, and absolute workspace values
- denial for active hold, malformed or missing ledger, stale observations, non-monotonic generation, conflicting snapshots, partial host reachability, and simulated crashes before and after rename
- canonical temp write, file fsync, rename, and directory fsync
- no network client, provider request, dispatcher, launcher, or gateway mutation path in the candidate

Applying the exact rollback patch to the exact candidate commit produced a tree byte-identical to parent commit `15382c7c6d311851f8da124728e3ee303e5994a2`. The shared hold hash remained `bc5ccfd3fae165bacec626683c485fd2496e63e72032c8795c5f2fff3198dc05`, and its parsed `active` value remained true.

## Actionable blocker

Acceptance criterion 2 requires chiap08 sole-writer locking. The candidate does not establish that boundary. `admit_wave` accepts `authority_host`, `ledger_path`, and `lock_path` from its caller. It checks only whether the caller-supplied string equals `"chiap08"`. It never verifies the actual local host, and it does not bind a ledger to one fixed authority lock.

A deterministic zero-network probe run on physical host chiap03 passed `authority_host="chiap08"` and successfully published generation 1. This proves a non-chiap08 process can satisfy the current check. Separately, two callers can provide different lock paths for the same ledger, so their advisory flocks do not serialize one another.

Required repair:

1. Derive and verify the physical authority identity from a non-caller-controlled host source, failing closed unless it is exactly chiap08.
2. Bind the ledger and its single lock to one reviewed, fixed authority directory. Do not accept independently caller-selected lock and ledger paths.
3. Add zero-network tests proving a chiap03 process cannot spoof chiap08 and proving alternate lock paths cannot bypass serialization.
4. Republish exact candidate and rollback bytes and request another distinct review.

Blocked contract: `blocked_on=card`, referent `ac:2`.

No hold clearance, GLM dispatch, provider traffic, merge, deployment, restart, gateway or configuration mutation, credential access, or external operational action occurred.
