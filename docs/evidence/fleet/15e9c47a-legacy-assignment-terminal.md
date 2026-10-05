# Terminal legacy assignment recovery

Card15e9c47a, ownerjarvis, claim877a070d23874709ad2076f3acdf4ef5. Base400af6892e4afed77ecf4bd620ecfd69ee9aaeed. No candidate installation or live service changes.

chiap08 charges reservationd32ba0b5fef9ceb429c54e57e7cce2fabf44bf3a260b93353eb9b5de1331d19b against max_concurrent_workers1. Its immutable intent binds7310cafa, ownerpi-glm-chiap08-7310cafa, claim7cc8da2a00014cd689ff0bde0dbeb20b, unit skfleet-worker-glm-7310cafa.service and memory_max_bytes3221225472. Only intent.json and fenced-start-required.json exist. The absent unit alone does not prove an unstarted intent.

Native assignment launch event1b6ebfe16b0b454a8d307bcba0c678da at2026-10-04T22:59:50.593714+00:00 reports launched=true for the exact owner/claim. User-manager journal records start1791154789924056 and terminal1791156072632676, same invocationfa3f7d0cb3974af3bae169186db12116. MESSAGE_ID39f53479d3a045ac8e11786248231fbf proves start; ae8f7b866b0347b9af31fe1c80b127c0 proves completion. Readback: not-found/inactive/dead, MainPID0, ControlPID0, empty ControlGroup and InvocationID.

Root cause: the legacy dispatcher called reserve_launch and then subprocess.run directly, bypassing start_reserved. Future starts now consume their reservation under the native exact claim fence. Failure retains custody.

Recovery uses a distinct immutable legacy-assignment-terminal receipt, bound to the exact native launch event, intent, unique journal invocation, manager start near the native launch and later terminal within the resource runtime. It requires stable collected-unit absence and refuses ambiguity, malformed or unavailable journal evidence. A launched assignment never reaches prestart-release logic. It does not create start.json or observed.json, release claims, modify candidates, fabricate source bundles or confer review acceptance. Existing live units remain charged.

Validation:

```
env -u BASH_ENV -u VIRTUAL_ENV PYTHONPATH=src /tmp/skcapstone-base-venv/bin/python -m pytest -q tests/fleet/test_legacy_assignment_terminal.py tests/fleet/test_production_admission.py tests/fleet/test_admission_claim_fence.py tests/fleet/test_failed_admission_terminal.py tests/fleet/test_successful_admission_terminal.py tests/test_skfleet_worker_cgroup.py tests/test_fleet_duplicate_admission.py tests/test_skfleet_worker_stop_semantics.py -m 'not host_systemd'
```

146passed,7.40seconds. Before implementation the new tests gave2failed16passed, reproducing the missing discharge. New tests now include22 passing recovery/refusal/cache/dispatcher branch cases. The broader nine-file run including test_skfleet_worker_exit_evidence.py gave171passed2failed9.20seconds. Both failures reproduce unchanged on base400af689 with the same isolated interpreter: test_detached_beat_cannot_strand_wrapper_after_immediate_child_exit and test_wrapper_exit_path_calls_workspace_lifecycle_decision_end_to_end, wrapper exit2. These are local baseline/environment failures, not hidden or newly xfailed. Hosted CI runs its complete installed test environment separately.

Black, Ruff and git diff --check pass. The first source commit preceded broader verification. Formatting and this evidence are a separate forward commit.

After lumina-nor rolls merged main out, normal admission may write the genuine terminal receipt and stop charging the exact dead assignment. It does not automatically convert7310cafa into a source-owned remote review; that still requires genuine retained native producer custody or a governed new producer generation preserving the existing candidate.

Rollback is source rollback through main-only governance; retained terminal receipts and all original claim/source/history bytes remain evidence. No reservation files are deleted.
