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

The supported environment is the already-qualified isolated Python runner.
SKLegal `make check` and Docker/PostgreSQL/Temporal restart qualification require
a different execution environment and are not enabled by this change. A profile
must not claim those checks are covered by the SKCapstone trial or a subset of
unrelated tests. Qualification does not authorize protected Matter access.

Deployment changes only the reviewed runtime modules and dispatcher. Rollback
restores their exact preimages. Retain profile, plan, request and receipt evidence;
do not rewrite earlier candidates or release existing claims during rollout.
