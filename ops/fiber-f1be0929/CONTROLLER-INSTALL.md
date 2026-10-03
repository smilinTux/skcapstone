# Fiber dispatcher installation checkpoint

Card: f1be0929. Writer: jarvis. Observed 2026-09-29 21:30 UTC.
This supersedes the earlier report that the dispatcher was absent.

## State, not completion

- Installed: chiap08 controller and native service/timer; chiap02/03/04
  worker adapters and approved named agent catalog aliases.
- Enabled: no new dispatcher timer. Timer is disabled and inactive.
- Held: ~/.config/skfleet-fiber/HOLD exists. Installed controller --once
  returns admission-held with no reconciled workers.
- Running: existing crew preserved. No new model worker launched here.
- Verified: component tests, catalog migration, remote runtime checks,
  installed adapter hashes, and fresh node readiness.
- NOT verified: live distributed implementation/review/test pipeline,
  useful integrated work per hour, or a complete deployment.

## Runtime and safety changes

Installed library: /home/skuser01/.local/lib/skfleet-fiber on chiap08.
Remote hosts have fiber_admission.py and fiber_worker.py at the same path.
The dispatcher persists reservations before remote mutation, does not retry
unknown launches, and releases capacity only for exact terminal receipts.
Admission enforces nine estate workers, 75-second spacing, initial host
roles, independent provider review, exact source binding, and claim checks.
Remote workers use native resource limits and refuse existing workspace
paths or branches. No existing session or claim was stopped or reassigned.

The hold command takes the admission lock and stops only new admissions.
Rollback begins with:

```sh
/home/skuser01/.skenv/bin/python /home/skuser01/.local/lib/skfleet-fiber/fiber_control.py --hold
systemctl --user disable --now skfleet-fiber-dispatch.timer
```

Do not stop worker units, delete workspaces, clear reservations, or restore
the unsafe legacy launcher as part of rollback.

## Exact checks

- Local: unittest discover, pattern test_fiber*.py: 34 passed, 1.171 s.
- chiap04 same suite: 34 passed, 0.392 s. Native unit
  f1be0929-controller-tests-r3-X6sJff.service, invocation
  38a86585979b45ddb1fb83ae1e96f056, exit 0, runtime 529 ms.
- Catalog migration regression: 1 passed locally and on chiap04. Remote
  unit f1be0929-catalog-tests-X6sJff.service, invocation
  8c19fa52ff004a44b1dfdc40158a72a5, exit 0, runtime 111 ms.
  Tests cover preservation, conflict refusal, idempotence, mode 0600,
  no credential output, and byte-exact restoration from backup.
- Remote suites ran with CPUQuota 200%, MemoryMax 1G, MemorySwapMax 256M,
  TasksMax 64, RuntimeMaxSec 60. These are test-unit limits, not the
  worker limits of MemoryMax 3G, MemorySwapMax 512M, RuntimeMaxSec 3600.
- Earlier native receipt check on chiap04 used /usr/bin/true, not a model
  worker: skfleet-fiber-f1be0929-b49bbd6bdb07451bb397e13a4c7808c4.service,
  invocation 74be2f54d62e48c98a9b0b8617be1381. Adapter reported terminal,
  PID 0, exit 0 despite active/exited RemainAfterExit state. Only that
  completed diagnostic unit was stopped afterward.
- All nine remote check combinations passed: chiap02/03/04 each with
  deepseek-flash, gpt-5.6-sol, and sk-zai-m. This checks catalog and runtime
  availability only. No completion requests were made and no cards claimed.
- Fresh fleet snapshot at epoch 1790717407.5087874: complete, zero errors,
  all eight hosts ready. Slots remain chiap02=2, chiap03=1, chiap04=1,
  all other hosts=0. This preserves gateway headroom and excludes default WAN
  placement. Capacity collector gives 26 conservative process observations;
  that is not 26 verified independent workers. Fifteen named working Herdr
  sessions were corroborated with live PIDs, already exceeding nine.

## Additive catalog deployment and rollback

The migration uses jq and native flock/mktemp/mv, no added dependency.
Descriptors whitelist nonsecret fields from chiap08's approved agent catalog.
Only deepseek-flash, gpt-5.6-sol, and sk-zai-m were added under their named
provider aliases. No Kimi alias was added. Existing skgateway entries were
preserved, including chiap04's expanded catalog. Credentials never crossed
hosts and remain in each existing models.json boundary. Backups are inside
the same .pi/agent directory, mode 0600. No credential values are recorded.

All paths below are /home/skuser01/.pi/agent/models.json with suffixes:

| Host | Backup suffix | Before SHA256 | Installed SHA256 |
| --- | --- | --- | --- |
| chiap02 | .fiber-backup.feSfLX | b559ef6465ce4277ebd6ba9c647ff0db110ed7de33702c369574f6e918544503 | 6e3ef38b7a8120d8faa708a8467973a045d5f16cf883f590761afe70b188584d |
| chiap03 | .fiber-backup.GbBu1l | b559ef6465ce4277ebd6ba9c647ff0db110ed7de33702c369574f6e918544503 | 6e3ef38b7a8120d8faa708a8467973a045d5f16cf883f590761afe70b188584d |
| chiap04 | .fiber-backup.OB54WA | b0ff69ea2ac0d3c9a41104d529a9e77bfd2bbe3099b134df72091e2eb51d76e5 | 0b386ad640b378483c7e2b0b87d0911c1045d1bc0715e70a88043efb7d1ad698 |

Rollback must first compare the live file with its installed hash and the
backup with its before hash. If either differs, stop; do not overwrite
concurrent changes. Under the same models.json.fiber.lock, restore from the
exact backup via a mode-0600 temporary file and atomic rename. Verify the
before hash. Byte restoration was tested with synthetic credentials in a
scratch directory, not by disrupting live catalogs. Original backup modes
and bytes remain available; do not copy these secret-bearing files elsewhere.

## Installed code hashes

| File | SHA256 |
| --- | --- |
| fiber_admission.py | fa68d0bd4750dabbed5dec2a3dbffc867f5da0b92b391a5b8fc2fac5f63138ea |
| fiber_control.py | b63bf048c3227efc4524902359178ba9ec2bae78e528183d6afdabb5f41dcb47 |
| fiber_dispatch.py | 58880ec86497a8aedd7f7f66c1282851d16bbd7859fab630d504bb131d39046e |
| fiber_probe.py | 9b754f20c344c864e5a77efd9f4226b072a43d2a4c9605a9158119a07cb37e39 |
| fiber_worker.py | 2f2323412c56ae518909f084ce177e765e281ea1f7f7dab9b7ecdb478d1e41de |
| skfleet-fiber-dispatch.service | 5ded1af4adb6ca6e92ec12f6a4c5f5e78d86203c20af183dbd20d0481bae0f5a |
| skfleet-fiber-dispatch.timer | f13a0682928762ed375ba7ca26e62b688077084cd0efafbc93f8723e4e2e8c05 |

Admission and worker hashes match on all three remote hosts.

## Remaining acceptance gates

1. Accurate logical worker/provider attribution and reconciliation of all
   remaining launch paths with the shared budget. Unknowns still consume
   capacity conservatively; no override to admit workers above nine.
2. Reviewed, eligible source-bound stage cards and explicit artifact handoff.
   Legacy queue is not replayed. No reviewed requests are currently queued.
3. Independent review before activation, plus fresh route completion and
   qualification evidence when capacity permits. Catalog presence is not
   qualification. Kimi remains fenced pending entitlement restoration.
4. Actual implementation on chiap02, independent review on chiap03, isolated
   tests on chiap04, external artifact verification, and linked outcomes.
5. Useful completed work, failure, memory pressure, and integration rework
   measurements. No throughput improvement claim is supported yet.

Self-review and behavioral tests cover this checkpoint. They are not a
substitute for the remaining independent review and live canary.
