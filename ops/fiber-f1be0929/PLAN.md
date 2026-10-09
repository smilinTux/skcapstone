# Fiber-first deployment f1be0929

User authorization: deploy the fiber-first layout and review the work before activation.

## Required outcome

One dispatcher on chiap08 admits new work under a shared nine-worker limit and
75-second launch spacing. Existing work counts and is preserved. Initial targets
are two implementation workers on chiap02, one independent reviewer on chiap03,
and isolated tests on chiap04. chiap01 retains gateway headroom. chiwk12 requires
verified runtime readiness. ZIOWK01 is WAN overflow, not a default target.

Preserve existing GLM, DeepSeek, Codex, Pi, and Herdr stage orchestration.
One admission authority must account for those stages, not erase them.

## Execution and evidence

1. Preserve runtime scripts, unit configuration, node specifications, and their hashes.
2. Eliminate destructive workspace recreation and unsafe claim-stealing instructions.
3. Reuse the installed coordination claim, review, and worker mechanisms. Reconcile
   local and remote launch paths behind one admission authority. Do not activate
   the separately failed automatic-crew package.
4. Collect live process/service/heartbeat/gateway evidence. Count unknown occupancy
   conservatively. Stop new admissions above capacity without killing sessions.
5. Prepare bounded host roles and CPU/RAM/swap limits; use local workspaces and
   task-owned test resources. Preserve gateway and application services.
6. Review code and configuration, run regression tests and rollback validation,
   then enable only the reviewed dispatcher path.
7. Run one implementation, independent review, and test canary on assigned hosts.
   Check actual artifacts and results. Record throughput, failures, pressure,
   rework, and remaining blockers. No claim of completion before this evidence.

## Current findings

- The original fill-slots.sh created local chiap08 workers, used 59/45 settings,
  and removed queue-supplied worktrees. Its unsafe launch path is now retired
  behind a fail-closed managed-service entry point. The replacement dispatcher
  is now installed on chiap08, with admission held and its timer disabled.
  This is not a completed fleet deployment.
- The prompt generator's claim-stealing and proceed-after-refusal instructions
  are removed. Original files have hash-verified backups and scratch rollback
  tests. See SAFETY-INSTALL.md for precise verification and limitations.
- The builder consumer only admits actuating builder-standby nodes. ZIOWK01 is
  the only inspected node with that role. Other fiber hosts have empty roles.
- Inspected dispatcher units were stopped with SIGTERM. Cause/actor not yet
  established. Old Niobe activation is scoped to specific products and uses a
  fixed Codex-only target of three; it cannot simply be repurposed by changing
  environment variables.
- A previous capacity rollout reports failed preclaim admission. Preserve that
  gate until current behavior is verified.
- chiwk12 has an sknoded process under /home/mrarch/.skenv, while the SSH account
  is skuser01. Checking only skuser01's user service misidentified its runtime.
- Many existing Pi processes are running on chiap08. They need attributed
  occupancy evidence before any new admissions.
- Herdr reports 15 named working sessions. DeepSeek has recent successful
  tool-call records. Inspected GLM panes show skgateway/sk-zai-m. Profile
  defaults and old session logs alone must not establish current model or
  liveness; verify the actual running session.
- chiap02/03/04 have live sknoded services and can reach gateway /health.
  chiap02's 220 kB free-swap blocker was repaired with a separate 2 GiB native
  swap reserve, not by weakening the gate. Rollback was rehearsed and original
  swap preserved. See RESOURCE-RESULT.md.
- chiwk12's actual mrarch user service is verified live. The collector now
  uses the correct account; no replacement daemon was installed.
- Two enabled-but-inactive legacy dispatch timers are now disabled to prevent
  boot-time reappearance. Worker sessions and service definitions remain intact.
- Central admission, persistent reservation lifecycle, actual-start spacing,
  lost-acknowledgment refusal, remote unit receipts, and a lock-protected hold
  are implemented. Thirty-four tests pass locally and on chiap04. Worker
  adapters and named provider aliases are installed on chiap02/03/04.
  Runtime readiness checks pass for all three aliases on all three hosts.
  These checks do not prove provider completion or qualification.
- Explicit new-worker provider/model attribution and live legacy Pi footer
  attribution are installed, with 39 passing local and chiap04 tests. Attached
  Codex app-server components no longer count as independent workers. See
  ACCOUNTING-RESULT.md. Unknown and native coordinator occupancy remain
  conservatively counted pending stronger task-level evidence.
- Reconciliation of remaining launch paths, reviewed
  queue preparation, independent review, artifact handoff, and the live
  distributed canary remain incomplete. Existing crew occupancy is above nine.
  Do not enable the timer or remove HOLD solely because unit tests pass.
- Eight safety tests ran on chiap04 under native cgroup limits, including
  assertions of actual memory.max, memory.swap.max, cpu.max, and pids.max.
  This proves remote bounded tests only, not the full agent canary.
- See CONTROLLER-INSTALL.md for current installation, exact test receipts,
  catalog backups, rollback, and explicit remaining gates.
- ZIOWK01's old builder consumer is now fenced through the native per-node
  report-only setting, with the same sknoded PID and fresh later heartbeat.
  No fleet-managed service placements or active builder jobs were present.
  See ZIOWK01-ADMISSION-FENCE.md for tests, rollback, replica delivery delay,
  and the cross-host claim-authority gate that must be resolved before launch.
- Twelve verified current crew owners were sent non-interrupting FYI notices
  to finish their current cards and hold self-refill. Delivery acknowledgements
  and natural capacity drain are not yet proven.
- Authority-side claiming is installed: only chiap08 claims; remote startup
  requires the exact live authority claim plus local replica agreement before
  request/workspace creation. The 46-test suite passes locally and on chiap04,
  and installed helpers accept the exact existing claim and reject a wrong
  generation on all three worker hosts. See CENTRAL-CLAIM-RESULT.md for
  evidence and conservative recovery limitations. Source-bound canary and
  artifact handoff preparation are next; admission is still held.
- Canary implementation card 4ce90fdd is created with do-not-claim and an exact
  published source revision. Its request remains outside the live queue.
  Native Git bundle transfer/import verified the same base commit/tree on
  chiap03 and chiap04. Imported exact candidate commits can now be reused by
  the worker adapter without a remote push. Forty-eight tests pass locally
  and on chiap04. See CANARY-PREPARATION.md; no agent canary has run yet.
- The external verify-card.sh now fails closed on board read/state failures,
  wrong card identity, invalid candidate history, and missing/empty/escaping
  evidence. Recorded BLOCKED is explicitly not successful completion. Four
  regression tests pass locally, on chiap04, and against the installed script
  using stubbed board reads. Original is backed up. See VERIFIER-RESULT.md.

## Rollback principles

Stop new dispatch first. Preserve worker processes, claims, workspace bytes,
branches, and receipts. Restore only backed-up deployment-owned configuration.
Do not restore dangerous launcher behavior automatically. A rollback is proven
in a scratch environment before enabling production dispatch.
