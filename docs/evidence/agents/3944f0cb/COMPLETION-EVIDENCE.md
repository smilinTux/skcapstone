# Candidate evidence for 3944f0cb

Producer: jarvis (Codex family). Claim: 7189153c83a84aecbe242293e09de4d2.
Parent: 3ee76566. Base: bb4aa0750c8c423d2206df7a4523c93cf9514436.
Candidate outcome: PASS_FOR_REVIEW, not runtime activation or task completion.

## Changes

- `ops/fiber-f1be0929/fiber_refill.py`: bounded operator-qualified inbox,
  atomic no-clobber queue publication, durable terminal-request preservation,
  exact pinned receipt and native-source revalidation. No board mutations.
- `ops/fiber-f1be0929/fiber_control.py`: refill under existing authority after
  failure reconciliation and HOLD; qualification checks before and after
  admission probes. Existing dispatch, claims, capacities and spacing untouched.
- Refill and controller tests and `REFILL-RUNBOOK.md`.

## Tests actually run

2026-09-30, Python in the existing operator environment. First test run before
implementation failed with the expected missing `fiber_refill` module.

- `python -m unittest discover -s ops/fiber-f1be0929/tests -p test_fiber_refill.py`:
  19 tests passed.
- `python -m unittest discover -s ops/fiber-f1be0929/tests -p test_fiber_control.py`:
  22 tests passed in the original operator run; revised results recorded below.
- Complete fiber suite with the environment in REFILL-RUNBOOK.md:
  167 tests run, 166 passed, 1 skipped, 0 failures, 2.970 seconds.
  The cgroup-boundary test is skipped outside its bounded service context.
- `git diff --check`: no whitespace errors.

Coverage includes expiry/revocation after queueing and after slow probes,
source/evidence/ownership changes, malformed and symlinked inputs, bounded
inbox and queue, terminal and running identities, interrupted publish/archive,
no-clobber behavior, HOLD and concurrent authority refusal. Existing admission,
provider attribution, capacity and 75-second-spacing regressions remain green.

## Limits and remaining gates

Independent review, runtime installation and live canary have not happened at
this snapshot. No source/hosted CI pass, throughput improvement, automatic claim
handoff, arbitrary backlog qualification, full W7 resumption, merge or deployment
is claimed. Source snapshot admission still has its historical ceiling of 9;
installation must retain the separately qualified live ceiling of 12 and all
current provider caps. Install only the refill helper and controller.

The trust root is operator-owned receipt storage, not the receipt's evidence
hash. Operators retain and actually qualify evidence before writing receipts.
Rollback must park pending refill jobs before restoring the old controller,
which does not know their expiry marker. Existing workers and claims are not
stopped or transferred. See REFILL-RUNBOOK.md for exact activation and rollback.

## First-review response

Review 36486fb4 verified the 19 refill tests and safety properties, but returned
FAIL because its system Python sandbox lacked skcapstone/skcoord, leaving one
controller test unable to run. This does not contradict the documented operator
environment passes, which were actually run and additionally repeated against
the installed admission12 stack (41 tests passed). The review also incorrectly
describes the old import as conditional on an owned/missing dependency, and says
the recheck wrapper catches ImportError: neither statement matches the source.
Preserve that original report and these corrections, rather than rewriting it.

The revised implementation moves the native gate import after deterministic
local denials and translates an unavailable import into the explicit fail-closed
reason `board-gate-unavailable`. No missing dependency can authorize admission.
A new test proves this behavior with a deliberately unavailable board client.
The revised exact snapshot still requires independent review before installation.

Revised actual results: complete operator fiber suite ran 168 tests, 167 passed,
1 cgroup-context skip, 0 failures (2.948 seconds). System `/usr/bin/python3 -S`
with no site packages independently ran all 23 controller tests successfully
(0.108 seconds). The 19 refill tests remain part of the complete suite.
