# Independent Review: GLM Wave Admission Candidate (Card 3f48c457)

**Reviewer**: pi-glm-chiap01-3f48c457
**Review Date**: 2026-08-29T10:15:00Z
**Reviewed Commit**: 3d61ee417664b7e834257eb6eb5da474b4df1cf4
**Reviewed PR**: https://github.com/smilinTux/skcapstone/pull/257
**Reviewed MANIFEST**: SHA-256 8543b667e300f1b2436ec4b1b3be3baa644df8c1b6868864c9b6136d9a128182
**Verdict**: PASS

---

## Executive Summary

I independently reproduced all artifacts from PR 257 commit 3d61ee417664b7e834257eb6eb5da474b4df1cf4 and verified the MANIFEST SHA-256 8543b667e300f1b2436ec4b1b3be3baa644df8c1b6868864c9b6136d9a128182. The candidate is a fail-closed, no-action GLM wave admission protocol that correctly implements:

1. Chiap08-only flock serialization
2. Atomic ledger replacement with temp-write, fsync, rename, and directory fsync
3. Fail-closed state validation on all paths
4. Exact 3-by-3 barrier (exactly nine workers, three per host)
5. Hold binding (active hold denies all admission)
6. No live-generation refill (live status is immutable until completion)

All zero-network tests from card 9ca6efd6 pass, including concurrent admission, stale observations, malformed ledgers, crash scenarios, hold changes, partial reachability, and tenth-worker denial.

## Artifact Reproduction

All shared artifact hashes from MANIFEST 8543b667e300f1b2436ec4b1b3be3baa644df8c1b6868864c9b6136d9a128182 reproduced exactly:

| Artifact | Expected SHA-256 | Reproduced SHA-256 | Status |
|----------|-----------------|-------------------|--------|
| glm_admission.py | e399be48f640129dac176436ffec78e6620024291134ce17779a92294ddd187e | e399be48f640129dac176436ffec78e6620024291134ce17779a92294ddd187e | MATCH |
| test_glm_admission.py | 87239cbb3ad5ef12c5357a04eba1c582fe94d4bc911d40a5cbe53c5ce068ef5f | 87239cbb3ad5ef12c5357a04eba1c582fe94d4bc911d40a5cbe53c5ce068ef5f | MATCH |
| candidate.patch | eac0e1d3ed16bbfb3fa54ab8782faf87acfdf8e837bc191695baf4ca8f376b9c | eac0e1d3ed16bbfb3fa54ab8782faf87acfdf8e837bc191695baf4ca8f376b9c | MATCH |
| rollback.patch | 553e8e6bfeab1031961d5f7bc7525b460aa51ddd42ba9c4b2ea4957d2bcee9dd | 553e8e6bfeab1031961d5f7bc7525b460aa51ddd42ba9c4b2ea4957d2bcee9dd | MATCH |
| no-action-cutover.md | e1b9314d9d387e4ca81fe2e3c02187519a89049561aaab7017315bad1385ee43 | e1b9314d9d387e4ca81fe2e3c02187519a89049561aaab7017315bad1385ee43 | MATCH |
| MANIFEST.json | 8543b667e300f1b2436ec4b1b3be3baa644df8c1b6868864c9b6136d9a128182 | 8543b667e300f1b2436ec4b1b3be3baa644df8c1b6868864c9b6136d9a128182 | MATCH |

## Security Property Verification

### 1. Chiap08-Only Flock Serialization (PASS)

**Evidence**: Lines 20, 264-265, 271 in glm_admission.py
- `AUTHORITY_HOST = "chiap08"` is a module-level constant
- Line 264-265: `if authority_host != AUTHORITY_HOST: _deny("only chiap08 may write or dispatch")`
- Line 271: `fcntl.flock(lock.fileno(), fcntl.LOCK_EX)` creates an exclusive advisory lock
- Lock covers ledger read, evidence validation, monotonicity check, and atomic replace
- The lock is released automatically when the `with` block exits

**Verification**: The lock is opened in append-binary mode (`"a+b"`) and wrapped in a context manager that ensures exclusive access for the entire admission decision. Only chiap08 callers can proceed past the authority check.

### 2. Atomic Ledger Replacement (PASS)

**Evidence**: Lines 221-243 in glm_admission.py (function `_atomic_replace`)
- Line 226: `tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)` creates a temporary file in the same directory
- Line 229: `stream.write(payload)` writes the canonical JSON
- Line 230: `stream.flush()` pushes data to the OS buffer
- Line 231: `os.fsync(stream.fileno())` forces the write to stable storage
- Line 234: `os.replace(temporary, path)` performs an atomic rename
- Line 240: `os.fsync(directory_fd)` fsyncs the parent directory to persist the directory entry
- Lines 244-247: Cleanup ensures no temporary files are left on crash

**Verification**: This pattern guarantees that either the old ledger or the complete new ledger is visible after any crash. Intermediate states cannot be observed by readers.

### 3. Fail-Closed State Validation (PASS)

**Evidence**: Function `_deny()` (line 85-86) raises `AdmissionDenied(RuntimeError)` with a stable reason string. This function is called on every validation failure:
- Missing or malformed ledger (line 180)
- Conflicting authority (line 192)
- Non-monotonic generation (line 195, 276)
- Invalid status (line 197)
- Stale ledger (line 201)
- Invalid hold binding (lines 205, 208)
- Invalid worker bindings (lines 210, 215)
- Complete ledger with workers (line 217)
- Non-authority host (line 265)
- Invalid time (line 267)
- Live generation refill (line 274)
- Hold change (line 283)
- Active hold (line 147)
- Unreachable host (line 155)
- Stale sessions (line 157)
- Stale host report (line 159)
- Incorrect queue sample timing (line 165)
- Stale queue samples (line 168)
- Queue not idle (line 170)
- Invalid worker count or distribution (lines 122, 125)
- Empty or conflicting worker fields (lines 130, 132)
- Non-absolute workspace (line 135)
- Non-host-distinct agent (line 137)

**Verification**: Every invalid state path results in `_deny()` being called, which raises an exception. No fallback or default behavior allows admission when validation fails.

### 4. Exact 3-by-3 Barrier (PASS)

**Evidence**: Lines 121-137 in glm_admission.py (function `_validate_bindings`)
- Line 122: `if len(bindings) != 9: _deny("wave must contain exactly nine workers")`
- Lines 123-125: Verify exactly 3 workers per host using a Counter
- Lines 129-132: Verify all five identity fields (card_id, agent_id, session_id, claim_id, workspace) are non-empty and distinct across all 9 workers
- Line 135: Verify workspace paths are absolute
- Line 137: Verify agent identity contains the host name (host-distinct)

**Verification**: The validation ensures a wave contains exactly 9 workers with no identity conflicts, distributed as 3 each on chiap01, chiap02, and chiap03.

### 5. Hold Binding (PASS)

**Evidence**: Lines 141-147, 273, 283 in glm_admission.py
- Lines 141-147: `_validate_snapshot()` checks hold hash, generation, and active status
- Line 147: `if snapshot.hold.active: _deny("hold is active")`
- Line 273: Hold is checked via `_validate_snapshot(first, now)`
- Line 283: Hold is checked again after the second snapshot: `if second.hold != first.hold: _deny("hold changed during admission")`
- Lines 294-298: The exact hold generation and SHA-256 are bound into the new generation ledger

**Verification**: The active hold is checked twice under the lock. If the hold is active, admission is denied. The hold generation and hash are atomically recorded in the ledger, binding the generation to the exact hold state under which it was admitted.

### 6. No Live-Generation Refill (PASS)

**Evidence**: Lines 196-197, 274 in glm_admission.py
- Lines 196-197: Ledger status must be `"complete"` or `"live"`; any other status is denied
- Line 197: `if ledger["status"] not in ("complete", "live"): _deny("invalid ledger status")`
- Line 274: `if ledger["status"] == "live": _deny("live generation is never refilled")`

**Verification**: Once a generation reaches `"live"` status, any subsequent admission attempt with that same ledger is denied with the message "live generation is never refilled". The only way to advance is for the generation to reach `"complete"` status (which requires external action to set workers to an empty list).

## Zero-Network Test Execution

All 11 tests from card 9ca6efd6 passed on the independently reproduced code:

```
test_concurrent_admission_serializes_one_whole_wave PASSED
test_unsafe_observations_deny[stale sessions-evidence0-glm-auto session] PASSED
test_unsafe_observations_deny[stale in-flight samples-evidence1-stale in-flight] PASSED
test_unsafe_observations_deny[partial reachability-evidence2-unreachable] PASSED
test_unsafe_observations_deny[active hold-evidence3-hold is active] PASSED
test_malformed_missing_stale_and_non_monotonic_ledgers_deny PASSED
test_writer_crash_before_rename_preserves_old_generation PASSED
test_writer_crash_after_rename_exposes_complete_live_generation PASSED
test_hold_change_during_locked_decision_denies PASSED
test_tenth_worker_and_duplicate_custody_are_denied PASSED
test_non_authority_and_active_hold_cause_zero_publication PASSED

11 passed in 0.06s
```

**Independent test output SHA-256**: 7e526c549489033c214ae407452ec0e9564b23bd1fa9d82ec24d4505eed03744

### Test Coverage Verification

1. **Concurrent admission** (PASS): 8 threads compete; exactly 1 succeeds, 7 are denied. Ledger generation is 1 with 9 workers.
2. **Stale sessions** (PASS): Non-zero `glm_auto_sessions` denies with "stale or live glm-auto session"
3. **Stale in-flight samples** (PASS): Queue samples older than 10 seconds deny with "stale in-flight request samples"
4. **Partial reachability** (PASS): Missing host in reachable set denies with "host unreachable"
5. **Active hold** (PASS): `active_hold=True` denies with "hold is active"
6. **Malformed/missing/stale/non-monotonic ledgers** (PASS): All four failure modes correctly deny
7. **Crash before rename** (PASS): Simulated crash preserves genesis ledger; temporary files cleaned up
8. **Crash after rename** (PASS): Simulated crash exposes live generation; refill is denied
9. **Hold change during lock** (PASS): Hold generation change between first and second snapshot denies
10. **Tenth worker and duplicate custody** (PASS): Extra worker or duplicate field denies
11. **Non-authority and active hold** (PASS): Wrong authority host or active both deny

## No-Action and Zero-Network Safety

**No-Action Verification**:
- The module contains no `subprocess`, `os.system`, `os.exec*`, or process launching functions
- The module contains no HTTP client (`requests`, `httpx`, `urllib`) or provider client code
- The module contains no CLI entry point, timer, scheduler, or selector integration
- The only public function `admit_wave()` returns a dict and performs no side effects other than ledger file replacement
- No inference requests are sent; only read-only observations are accepted via function arguments

**Zero-Network Verification**:
- Test suite monkeypatches `socket.socket` and `socket.create_connection` to raise `AssertionError("network access is forbidden")`
- All tests pass with this network block in place, confirming no network access occurs
- The module imports only standard library modules: `fcntl`, `json`, `os`, `tempfile`, `collections`, `dataclasses`, `datetime`, `pathlib`, `typing`
- No external dependencies or network-capable libraries are used

## Rollback Bytes Verification

The rollback patch SHA-256 553e8e6bfeab1031961d5f7bc7525b460aa51ddd42ba9c4b2ea4957d2bcee9dd was verified against the commit. The patch removes:
- `docs/fleet-glm-wave-admission-candidate.md`
- `src/skcapstone/fleet/glm_admission.py`
- `tests/test_glm_admission.py`

Since the candidate is not wired into any runtime selector, gateway, or provider, rollback requires no live system mutation beyond reverting the commit.

## Conclusion

The candidate correctly implements a fail-closed, no-action GLM wave admission protocol with the following safety guarantees:

1. Only chiap08 can write to the admission ledger
2. Ledger updates are atomic with crash recovery guarantees
3. All validation failures deny admission with stable error messages
4. Exactly nine workers in a 3-by-3 distribution are allowed per generation
5. An active hold denies all admission attempts
6. A live generation cannot be refilled until marked complete

All shared artifact hashes reproduce exactly, and all zero-network tests pass. The candidate is safe for distinct review and meets all acceptance criteria from card 3f48c457.

**No operational action was taken**: No hold was cleared, no GLM was dispatched, no deployment occurred, no credentials were accessed, and no gateway or provider configuration was mutated.

---

**Reviewed By**: pi-glm-chiap01-3f48c457
**Reviewed On**: chiap01
**Review SHA-256**: (to be computed for this document)
**Review Commit**: (to be published in a PR)
