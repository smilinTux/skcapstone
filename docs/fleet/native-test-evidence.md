# Native test evidence

Date: 2026-10-01. Source card: `5b672ea1`. Parent: `9b230773`.
Status: source implementation. Installation and a real systemd qualification
remain required. Unit fixtures do not establish a production test PASS.

The existing production controller calls `production_tests.run_or_read_tests`
only after independently proving exact producer and reviewer termination and
checking current source ownership, claim and native revision. It passes an
exact clean source checkout, not the review evidence commit checkout.
No generic command, model-provided callback, success Boolean, CI link or model
statement is accepted as test evidence.

## Qualification and immutable input

The operator qualifies the installed runner, Python 3.12 dependency prefix
`~/.skenv`, sandbox and host resources before explicitly calling `seal_plan`.
This operator API is not part of worker dispatch. It requires a qualification
evidence digest, operator identity and the current validated production policy.
The plan pins the interpreter bytes and a bounded dependency fingerprint:
all installed distribution `METADATA`/`RECORD` and startup `.pth` files; Python
and native modules in pytest, ruff, skcoord, skcapstone, pydantic, pydantic_core,
pluggy, yaml, rich and click; and the actual ruff executable. Reads are bounded
to 10,000 files and 64 MiB. Exact inode/size/mtime/ctime observations cache file
hashes, while metadata is restatted on each validation. The worker validates
this fingerprint before and after execution. This is the explicit qualified
dependency coverage, not a claim to hash every file in the virtual environment.
The plan is created exclusively at
`fleet/test-plans/<source_card>-<source_head>.json`, mode `0600`, in an owned
`0700` directory. Existing plans cannot be overwritten by this API.

The exact binding contains `source_card`, `source_owner`,
`source_claim_revision`, `source_head`, `source_tree`, `source_revision` and
`criteria_sha256`. The last is SHA-256 of canonical JSON of the native criteria
list (`sort_keys=True`, separators `(',', ':')`). Seal after authorized source
card updates, then leave its revision unchanged until guarded acceptance.
No plan link is added to the source card, avoiding a revision/digest cycle.

The initial fixed profile is the twelve approved `89508f83` test files in one
pytest invocation, dispatcher `py_compile`, the exact regression file's ruff
check and changelog `--check`. It requires at least 226 test cases, at least
one per approved file, at least 11 terminal-review regressions, zero failures,
errors or skips, and all four commands exiting with integer zero. Broader test
profiles require their own source change and operator qualification.

## Execution and evidence

The plan digest is the unique service request ID. Durable launch intent is
written and fsynced before starting `production_builder.service_command`.
The test-only service also sets `Type=exec` and `RemainAfterExit=yes`, retaining
the terminal invocation instead of losing it when systemd garbage-collects a
successful transient unit. Existing CPU, memory, task and runtime quotas apply.
Memory
admission includes Pi workers, native builder/test services and pending test
launch reservations. Repeated calls poll the same unit. Lost launch
acknowledgement retains the reservation and never launches a replacement.

The installed trusted host executor runs each fixed argv under `bwrap
--unshare-all --clearenv`. Source, `/usr` and the qualified dependency prefix
are read-only. Only private temporary storage and one output mount are writable.
Environment contains only the qualified PATH, temporary HOME, source import
path, temporary bytecode and ruff caches, disabled pytest plugin autoload and disabled
global/system Git config. No host credentials or private agent home are mounted.
The controller must already have custody of the clean source and its stopped
producer so another actor cannot change the source during execution.

The host executor drains bounded raw output, records the actual command/exit,
copies JUnit after sandbox exit and binds source before/after, plan SHA, service
invocation and worker PID. The controller independently records systemd's exact
terminal invocation, PID and exit. A successful unit must be loaded,
active/exited, MainPID zero, and have either a named cgroup with zero
TasksCurrent or the observed removed cgroup state (empty ControlGroup and
TasksCurrent `[not set]`). Missing or other unknown states fail closed. After
fsyncing that immutable observation, the controller rechecks the same invocation,
stops only that retained unit and reaps its owned systemd-run child. A retry
also recognizes the same invocation already loaded inactive/dead with the same
empty-cgroup proof, or an already collected unit; it never stops a different
invocation. Failed
terminal services retain failure evidence and release their resource reservation
without accepting the source. Receipt validation rehashes every raw log,
JUnit and receipt; recomputes coverage and checks the actual source. Validation
uses the original binding without requiring a still-live claim after completion.

`run_or_read_tests(home, binding, workspace, policy)` returns `None` for a missing
plan or active test, or a verified object. `validate_test_receipt(home, binding,
workspace)` returns that object or raises `TestEvidenceError`. Result keys are
`plan_sha256`, `receipt_sha256`, `source_head`, `checks`, `counts`, `receipt_path`.
The acceptance controller must call the validator at every guarded mutation
and historical receipt check. It must use `active_resource_units(home)` for its
resource admission; the lane-specific Pi worker parser is not sufficient.

## Recovery and rollout

No automatic retry creates another attempt. Missing exact terminal custody,
failed checks, changed source, quota-policy drift or malformed evidence keeps
acceptance withheld. Preserve the source, plan, service state and output for
native operator disposition. Do not remove a launch intent simply to retry.

This source change does not install a runner, seal a live plan, start a production
test service, complete source card `89508f83`, or alter invalidated review `3054d5f1`.
Operator-authorized inert unit probes verified retention, raw terminal properties,
exact stop and reaping. The approved trial argv also passed in an inert sandbox:
250 tests and all three other checks. Those are readiness checks, not an
acceptance receipt. Original source caches and refs were preserved; production
acceptance must use a separate clean imported source checkout.
Before installation, independently review the exact commit and run a real unit
qualification, including the systemd terminal properties and installed module
resolution. Source rollback removes the new modules/docs and reverts the shared
resource query change; no data migration occurs. Preserve any later live
evidence and service custody when reverting installed code.
