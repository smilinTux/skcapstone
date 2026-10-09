# Live provider accounting checkpoint

Card f1be0929, jarvis, 2026-09-29 21:38 UTC.

## Repair and evidence

The previous collector reported every worker provider as unknown. For a new
fiber worker, this disagreed with its persisted reservation and would refuse
later admissions as ambiguous-worker. The adapter now supplies its validated
provider/model pair in two allowlisted environment fields. The collector
accepts only recognized matching pairs.

Legacy Pi workers are attributed from their current Herdr detection footer,
not names, cwd, profile defaults, or old logs. Unrecognized footers remain
unknown. An optional trailing MCP status line is handled explicitly. No pane
contents, raw command lines, prompts, credentials, or full environments are
retained or logged by the collector.

Native Codex app-server children attached to another observed Codex process
are components, not independent worker sessions. Standalone app servers and
exec children remain counted. Native Codex processes are attributed to the
Codex family, but a process alone does not prove a currently executing task.
Unknown occupancy still consumes all provider budgets conservatively.

- 39 local dispatcher tests passed in 1.259 seconds.
- Same 39 tests passed on chiap04 in 0.419 seconds, native unit
  f1be0929-controller-tests-r5-X6sJff.service, invocation
  ef71fc2d8822470a9f15da36e635116c, exit 0, runtime 556 ms.
- New cases cover explicit provider/model agreement, no name/profile inference,
  footer parsing and refusal, attached versus standalone app-server accounting,
  and one live worker plus its reservation occupying exactly one slot.
- Installed controller --once still returns admission-held. No worker launched.
- Installed live snapshot at epoch 1790717912.2802272: complete, no errors,
  all eight hosts pass process/service/heartbeat/gateway/headroom checks.
  Counts: DeepSeek 10, GLM 5, native Codex observations 6, unknown 1.
  Fifteen named working Pi sessions are live-process corroborated. The larger
  count remains a conservative occupancy estimate, not 22 proven busy workers.
- chiap08 MemAvailable 7,349,160 kB; swap used 118,842,632 kB. This is a
  point-in-time pressure observation, not a throughput improvement result.

## Installed revisions and rollback

fiber_probe.py on chiap08:
107e1e04f86013f39e31b43bc421810d1621bcca7752887fcc44c8dfcfa8aae4

fiber_worker.py on chiap08 and deployed to chiap02/03/04:
d6b876fc24fc6b96ad35c47705ad1a8f7c16bdf9b19dcbf5a73cda71c93bdff7

Prior root files are in backup/accounting-v1 alongside this report. Each
remote prior worker file is retained as
~/.local/lib/skfleet-fiber/fiber_worker.py.before-provider-accounting.
Rollback must hold admission first, compare installed and backup hashes,
and restore only these exact files. No existing worker process is restarted.
The original hashes are recorded in CONTROLLER-INSTALL.md.

## Other launch paths inspected

All five known legacy dispatcher families were inspected on eight hosts:
rotate, niobe-live, estate-glm, seat-cycle, and seraph. All loaded service
instances have MainPID 0 and are inactive or failed; loaded timers on
chiap01/02/03/04/08 are disabled and inactive. No matching services are
installed on chiwk12, chiwk13, or ZIOWK01. Definitions remain intact.

Old rotate configuration on chiap08 explicitly pins its authority to
chiap01. The new controller requires chiap08. Re-enabling that old path would
not be a valid shared-authority deployment. The legacy fill-slots entry point
already delegates to the held new service.

ZIOWK01 still has a separate builder consumer inside its active sknoded
service. Its four current offers expired on 2026-09-29 between 07:04 and
12:29 UTC. Its 69 recorded outcomes are 29 blocked, 28 failed, 12 completed,
with no running outcome. Live collector sees no worker process on that host.
Those are historical outcomes, not proof of 69 current executions. No queue
record or claim was removed. Do not disable sknoded merely to stop admission:
it also owns node reporting and service convergence.

The builder consumer still needs a new-admission fence or integration into
the central authority before activation. Cordon/taint alone is insufficient:
the inspected consumer does not recheck those fields before consuming an
already written offer. Its producer can also reuse existing placement paths.

## Remaining work

The new dispatcher remains held and its timer disabled. Existing model work
is above the nine-worker ceiling. Do not launch a review or canary above that
ceiling. Native coordinator-versus-worker accounting needs stronger evidence
before treating its conservative counts as exact capacity.

Remaining acceptance includes reconciling the builder consumer, source-bound
stage requests and artifact handoff, independent review, fresh completion and
qualification gates, the real distributed canary, and external outcome and
throughput verification. This checkpoint is progress, not a completed rollout.
