# Bounded qualified-request refill

Card: `3944f0cb`, parent `3ee76566`.

This extends the existing chiap08 dispatcher. It does not discover arbitrary
backlog, certify source allegations, transfer claims, accept work, publish PRs,
merge, or deploy applications. Operators still qualify exact requests. Source
qualification and measured throughput remain separate from launch capacity.

## Input and trust

The opt-in directory is `~/.config/skfleet-fiber/qualified`. Its absence
disables refill. This directory and `jobs` must be owned by the dispatcher
account and not group/world writable. Use directory mode 700 and file mode 600.
Workers cannot access them from their sandbox. Never admit model-written
receipts as operator qualification.

Each `<8-hex-card>.json` contains exactly:

- `schema`: `skfleet.qualified-request/v1`
- `job`: the exact explicit controller request, without `claimable` or
  `_qualification`; obtain route/source fields using the existing describe tool
- `job_sha256`: SHA256 of canonical job JSON
- `card_links_sha256`: SHA256 of canonical native card links, including evidence
- `qualified_at`, `expires_at`: finite Unix timestamps, lifetime at most 24 hours
- `evidence_sha256`: hash of the operator's retained qualification evidence

Canonical JSON uses sorted keys and separators `(',', ':')`. The evidence hash
is a provenance reference, not a signature or proof that evidence was reviewed.
The operator must retain and actually review that evidence before installing a
receipt. Exact card source, task contract, eligibility, ownership, dependencies
and route policy are checked again by code.

At most 32 receipts are read and at most two pending requests are queued.
Running requests count toward that queue bound. The queued job preserves the
exact request fields and adds `_qualification`, the complete pinned receipt.
The controller checks the current receipt, expiry, source and native links
again before and after potentially slow admission probes. A removed, replaced,
expired or newly ineligible qualification fails closed. It never becomes a
legacy manual request. Repeated faults use the existing three-fault HOLD.
An operator must inspect and preserve a stale queued request before replacing
it; automatic refill never overwrites it or resets a run record.

Confirmed terminal requests move to `jobs/terminal/<card>-<digest>.json` using
durable no-clobber hard links. Their original bytes and admission history stay
intact. Any prior run identity prevents automatic replay, including failures.
An archive interrupted between link and unlink resumes idempotently.

## Qualification and activation checklist

1. Claim the exact infrastructure card and preserve existing user edits.
2. Run the refill and controller tests, then the complete fiber test suite.
3. Commit the local infrastructure snapshot and independently review that exact
   diff through another qualified provider family. Do not claim hosted CI ran
   merely because local source tests passed.
4. Under the existing authority lock, HOLD new admission without stopping
   workers. Preserve the old controller bytes, hash and queue directory mode.
5. Install only `fiber_refill.py` and `fiber_control.py` on chiap08. Do not copy
   snapshot admission modules: the live approved ceiling is 12, while this
   historical snapshot still tests a ceiling of 9. Preserve all live caps,
   provider restrictions, 75-second spacing and automatic failure HOLD.
6. Stage one genuinely qualified canary receipt. Verify preview causes no writes,
   execute through the existing dispatcher and externally verify the exact unit,
   source and completion. Repeat a cycle to prove no duplicate admission.
7. Retain exact test results, reviewer evidence, file hashes and canary receipts
   on the native card. Report incomplete gates honestly.

The standard test command is:

```sh
BASH_ENV=/dev/null FIBER_CANDIDATE_DIR="$PWD/ops/fiber-f1be0929" \
  FIBER_FILL_NAME=fill-slots.candidate.sh \
  FIBER_PROMPT_NAME=make-prompt.candidate.sh \
  PYTHONPATH=ops/fiber-f1be0929 ~/.skenv/bin/python -m unittest discover \
  -s ops/fiber-f1be0929/tests -p 'test_*.py'
```

## Rollback

HOLD new admission under the existing authority lock. Existing workers, claims,
workspaces, ledger and terminal evidence remain untouched. Preserve pending
refill requests outside the active `*.json` queue before restoring the old
controller: it does not enforce `_qualification`. Restore the backed-up
controller and the original queue directory mode. The unused helper and
qualification evidence may remain for audit. Unhold only after checking the
remaining explicit requests against the old admission contract. No old timer
is reactivated by this change.
