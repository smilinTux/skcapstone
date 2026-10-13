# Fiber rollout readiness and blocked audit

Parent: f1be0929. Producer: jarvis. Date: 2026-09-29.
Live fleet observation epoch: 1790720355.889434 (22:19 UTC).
This is an observation and review index, not a PASS or deployment completion.

## Current authoritative observations

- Collector completed with no errors. All eight hosts passed live process,
  service, heartbeat, memory-headroom and gateway-health checks. Heartbeat
  ages ranged from 2.8 to 56.7 seconds. Gateway health is not a completion
  qualification result.
- Fourteen named GLM/DeepSeek Pi workers on chiap08 were corroborated with
  live processes: nine DeepSeek and five GLM. Those alone exceed the estate
  ceiling of nine, independently of conservative Codex/unknown accounting.
- DeepSeek PIDs: 869558, 874677, 880290, 885249, 890383, 895465, 900601,
  905746, 3908170. GLM PIDs: 1329473, 1349585, 1354585, 1359621, 1364634.
  This is a point-in-time observation, not permission to signal these PIDs.
- chiap08 MemAvailable: 9086712 KiB. SwapTotal: 239075288 KiB;
  SwapFree: 121346196 KiB. Swap occupancy does not measure current paging.
- New dispatcher timer: disabled/inactive. Service: static/inactive,
  MainPID=0, Result=success. Installed control --once returned
  {"reconciled": [], "state": "admission-held"}.
- Parent owner remains jarvis, exact claim revision
  fa5f2819810f4cf08fe5a35ea58da010. Canary 4ce90fdd remains unowned,
  backlog, do-not-claim. No claim, queue submission or worker launch here.
- Admission and worker hashes match chiap08 and chiap02/03/04 over SSH.

## Requirement audit

| Requirement | Evidence and remaining gap |
| --- | --- |
| Single dispatcher, cap 9, spacing 75, existing work preserved | Installed and held; controller/admission behavioral tests recorded. Existing occupancy blocks activation. No live distributed admission proof yet. |
| Fiber placement and independent review | Initial 2/1/1 slots configured for chiap02/03/04. Actual implementation/review/test canary has not run. |
| Workspace and claim safety | Installed guards and focused tests recorded in SAFETY-INSTALL.md and CENTRAL-CLAIM-RESULT.md. Existing work untouched. |
| Runtime readiness and stopped dispatchers | Fresh eight-host readiness passes. Earlier SIGTERM records establish stopping, not who requested it or why. Root cause attribution remains limited. |
| Isolation, limits, policy | Native bounded remote test execution verified in earlier reports; actual model-worker canary isolation still unproven. No application deployment or protected-data processing. |
| Reuse and rollback | Native services, fleet actuation, Git bundles and CLI claims reused. Backups and scratch rollback evidence linked in reports. Never restore destructive launcher behavior. |
| Review and behavioral tests | Component tests recorded, but independent infrastructure review remains outstanding. This audit is not independent review. |
| Distributed live canary and external acceptance | NOT ACHIEVED. Held implementation card exists. Base bundle transport is not an implemented candidate. |
| Useful work, failures, memory pressure, rework | Memory snapshot above. This rollout has zero accepted distributed canaries; useful throughput improvement and integration rework are not yet measured. |

## Exact installed review targets

Paths below are relative to /home/skuser01. SHA256 values were freshly read.

```text
fa46f4d87147cf613875daf529505f008b9aed3b9c99dcb41f0dc9f2faa87dee  .local/lib/skfleet-fiber/fiber_admission.py
924fe3a242a0bd2db597837e88a2c2592db2a83d24eb79e9bc8a07593c39a95e  .local/lib/skfleet-fiber/fiber_control.py
58880ec86497a8aedd7f7f66c1282851d16bbd7859fab630d504bb131d39046e  .local/lib/skfleet-fiber/fiber_dispatch.py
107e1e04f86013f39e31b43bc421810d1621bcca7752887fcc44c8dfcfa8aae4  .local/lib/skfleet-fiber/fiber_probe.py
42553364356a590b2ed833c3209a0461bcf02140762b18a36aa4c6ad479148f6  .local/lib/skfleet-fiber/fiber_worker.py
5ded1af4adb6ca6e92ec12f6a4c5f5e78d86203c20af183dbd20d0481bae0f5a  .config/systemd/user/skfleet-fiber-dispatch.service
f13a0682928762ed375ba7ca26e62b688077084cd0efafbc93f8723e4e2e8c05  .config/systemd/user/skfleet-fiber-dispatch.timer
0d6e1e90ac46341cfdd419d33b5aba6be801765b82f3534fb82580db50a8b327  .skcapstone/runtime/llm-orch/fill-slots.sh
83eafc77560ca10c240ceabc93a1c8f871054e5fc9b36c8dd30af6bd1dab054d  .skcapstone/runtime/llm-orch/make-prompt.sh
7b6e958aa3a6db0eb22319b9757db85e7cb759a7992689001d1a221bc246ffdd  .skcapstone/runtime/llm-orch/verify-card.sh
```

Review supporting reports in this directory: SAFETY-INSTALL.md,
RESOURCE-RESULT.md, CONTROLLER-INSTALL.md, ACCOUNTING-RESULT.md,
ZIOWK01-ADMISSION-FENCE.md, CENTRAL-CLAIM-RESULT.md,
CANARY-PREPARATION.md and VERIFIER-RESULT.md. Reports are chronological;
the current hashes above supersede older installed hashes. In particular,
VERIFIER-RESULT.md supersedes the old verifier warning in canary preparation.
The latest recorded fiber suite is 48 passing tests, with four separate
verifier tests. No tests were rerun during this observation-only audit.

Native governed review requires a source-card/head-revision binding, producer
identity, evidence SHA256, and qualified reviewer seat. Installed runtime
files are not the published canary base commit. Do not manufacture a source
binding by pointing review at that unrelated base. No governed review card
or PASS was fabricated here.

## Blocker and resumption

The same estate-capacity blocker has been observed across more than three
consecutive goal work turns, and is freshly corroborated above. No existing
session may be interrupted or claim reassigned to manufacture a free slot.
Current workers must finish naturally, or the operator must explicitly
authorize a different workload policy. Do not silently raise the ceiling.

After capacity changes: recollect complete live occupancy, finish exact-bound
independent infrastructure review, obtain fresh qualified route completions,
and only then admit the held source-bound canary. Run implementation on
chiap02, cross-provider independent review on chiap03, and isolated tests on
chiap04. Externally verify candidate artifacts and board outcomes before
acceptance. Kimi remains fenced; entitlement restoration is not a prerequisite
for qualified non-Kimi lanes. Preserve HOLD and disabled timer until gates pass.

Deployment is incomplete. No acceptance condition is waived by this report.
