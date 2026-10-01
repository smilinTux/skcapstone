# Qualified production test contracts

Source-only producers require an operator-qualified private test profile before
new admission. Hosted PR lanes, independent reviewers, existing workers and
explicit native retries retaining an existing claim keep their existing gates.

The operator first proves the exact recipe in the existing isolated Python
runner and records that evidence hash. `production_test_profile.qualify_profile`
then stores an immutable `fleet/test-profiles/<card>.json` using the exact native
card ID, repository, nonempty acceptance criteria digest, policy and interpreter
dependency fingerprints. This operator API is not called by worker prompts.

A recipe contains only `pytest` (explicit `tests/*.py` paths mapped to minimum
positive testcase counts), `compile` and `lint` (explicit Python file lists), and
`changelog` (a boolean). The controller constructs fixed Python, pytest, ruff and
changelog argv. There is no arbitrary shell or model-provided command field.
Every required pytest file must produce its minimum count; all failures, errors,
skips, duplicate cases and unexpected files refuse acceptance. Node quotas and
the existing no-network, read-only source sandbox remain unchanged.

Atlas checks the profile before workspace preparation and rechecks the current
native contract immediately before claiming. Niobe checks before a builder offer;
the existing trusted dispatch request carries the qualified profile. The remote
consumer compares it to the current native repository, criteria and operational
policy before claim and launch. Profiles are immutable. Stopping an already
offered assignment uses the existing native hold, do-not-claim or request
cancellation controls, which the consumer rechecks. Deleting a private profile
is not an assignment revocation API.

After exact source and independent review custody checks, the controller creates
the ordinary private candidate test plan automatically, binding the current
commit, tree, owner, claim, source revision and criteria digest. It then uses the
existing single-attempt native test unit and verifies the raw JUnit receipt.
Missing qualification stays pending; it never becomes a successful test result.
Existing sealed trial plans retain their original twelve-file coverage rules.

The Python environment remains the already-qualified isolated Python runner.
SKLegal `make check` and Docker/PostgreSQL/Temporal restart qualification require
a different execution environment and are not enabled by this change. A profile
must not claim those checks are covered by the SKCapstone trial or a subset of
unrelated tests. Qualification does not authorize protected Matter access.

Deployment changes only the reviewed runtime modules and dispatcher. Rollback
restores their exact preimages. Retain profile, plan, request and receipt evidence;
do not rewrite earlier candidates or release existing claims during rollout.

Already accepted pairs use their retained historical proof after an upgrade.
Both native cards must still be DONE and unowned at the recorded revisions, with
the exact accepted test receipt in their native links. The controller rehashes
the historical plan, receipt, raw logs and JUnit, and verifies the original
context, intent and complete acknowledgement chain without any writes. It does
not require the old plan to match today's runtime or regenerate old commands.
New or unfinished pairs still require the current qualified runtime.

## Node and Vitest variant

Card `c1a30112` adds `skfleet.qualified-node-test-profile/v1` to the same
native service and sandbox. Its recipe is only `vitest`, mapping each exact
`src/**/*.test.ts` or `.test.tsx` file to a positive minimum count. The full
Vitest run must report exactly those files, unique cases and no failures,
errors or skips. TypeScript `tsc -b` and ESLint `.` must also succeed.
Package scripts must exactly match `vitest run`, `tsc -b` and `eslint .`,
without pre/post hooks. Commands invoke the pinned tools directly, never npm
installation or candidate-supplied commands.

The operator binds Node executable bytes, platform, the root package and lock,
web package, Vite, TypeScript and ESLint configurations, plus the complete
dependency artifact hash. Artifacts live only under the fixed node-local
`~/.local/share/skcapstone/test-dependencies/<sha256>` root. The root is
owned and mode `0700`; artifact directories and files have no write bits.
Only internal, existing relative symlinks are allowed. Never put artifacts
in synchronized coordination storage: actual qualification observed
Syncthing normalize read-only artifact directories to `0755`, correctly
invalidating their qualification. Transfer and verify artifacts separately.

Clean source, dependencies and runtime are read-only inside the existing
network-isolated bwrap. A private mount scaffold provides missing dependency
mountpoints and is then remounted read-only. Mount-entry symlinks are rejected
before they could resolve into host paths. The only writable project location
is the TypeScript build-info file, backed by a newly empty private output file.
This preserves the exact `tsc -b` semantics; TypeScript 6 rejects a command-line
`--tsBuildInfoFile` together with `-b`. Vitest uses its runner config loader
and disabled cache to avoid source writes. Scratch, output and node-qualified
service quotas retain their existing boundaries.

Qualification must include an actual positive service run, failure and
missing-result checks, dependency drift, isolation and terminal custody.
Ordinary green Vitest tests do not satisfy scanner card `a8300e02`'s distinct
M1/M2 mutation requirements. No production profile is admitted by the runner
implementation itself. Missing or failed evidence remains unaccepted.

Installation changes the installed SKCapstone runtime fingerprint and therefore
stales existing unfinished profiles and plans. Preserve those immutable records;
do not rewrite them or requalify historical candidates automatically. A fresh
card qualification must use the newly installed runtime fingerprint and actual
candidate evidence. Back up the four existing runtime module preimages before
installation; rollback restores them and removes the newly added Node module
only if absent before installation. Verify hashes and import/targeted regression
checks after install and rollback. No profile, plan, receipt or claim is deleted.
