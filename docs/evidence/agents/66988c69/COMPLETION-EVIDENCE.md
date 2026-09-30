# Native zombie accounting

Card: 66988c69. Actual source author: jarvis, Codex operator.
Claim: 3458861945db4a15b82bd621b90c2391.
Base: 2de74bee6ba14e8c408cf0bc26a9dde2956483f8.
Branch: work/66988c69-native-activity.

Live kernel inspection found chiwk12 PID842442, UID1002, start ticks7756178,
in state Z. Its empty command line was counted as an unknown Codex worker.
No process was stopped. A zombie is terminated; sleeping or stopped sessions
are not treated as terminated.

The existing probe now excludes only two identical kernel stat observations
with exact PID, positive start ticks and state Z. Unreadable, malformed,
changed, live, stopped and unknown states stay counted. Exclusion occurs
before app-server parent deduplication, so a live child is not hidden behind
a dead parent. No rollout logs, prompts, credentials or native session state
are read by this change.

## Verification

- Before the implementation, the expanded node-observation test failed:
  zombie48 was retained and live child49 was incorrectly deduplicated.
  Original focused suite:10 tests,1 failure.
- After implementation:12 focused unittest tests passed in0.009seconds.
  Cases include PID reuse, state change, malformed/truncated input, permission
  errors, disappearance, sleeping/stopped states and live-child preservation.
- A real Linux fork/waitid/waitpid check verifies a live process is counted,
  its unreaped zombie is recognized, and the reaped PID is no longer accepted
  as zombie evidence. This is a kernel test, not a model worker or provider call.
- Full command: `.skenv/bin/python -m unittest discover -s
  ops/fiber-f1be0929/tests -q`:237 tests,234 passed,3 conditional skips,
  2.674seconds. No live sandbox or remote resource flag was enabled.
- `git diff --check`:PASS.

Only the probe, its existing test file, one changelog fragment and this evidence
file change. The controller records the exact post-commit hash separately;
this document contains no guessed or pre-amend candidate hash.

## Remaining acceptance and limits

Independent cross-provider review and hash-pinned installation with backup
are still required. This source evidence does not claim those steps occurred.
No native idle-log predicate is qualified or installed. At the13-count census,
removing this one false count would still leave12 against ceiling12. It does
not establish capacity for another worker or completion of09cf4fec's trial.
The observation is a snapshot, not a promise that no new process can appear
after it. Existing fresh-census admission and conservative unknown accounting
remain required. Ceiling12,provider caps and75-second spacing are unchanged.

Rollback restores the exact prior authority probe under its existing lock
after verifying the deployed hash. Remote node probes receive the authority
script through existing SSH transport; no new node service or scheduler is
installed. Retain the backup and all review and installation receipts.
