# Dependency selector correction and qualification

Card: d9f07e2a. Producer: jarvis. Claim:
`cac3dc19020e46d9be92a52f0560ab86`.
Base: `988dce2ef82adcbf20b1b91cfa9b307fbc562c2b`.
Original infrastructure base: `e6d4139775682ae596c182168cd2f059aafff5b9`.

## Changes

- Remove the invented tests-stage-only restriction. Implementation, review
  and test jobs all need to run tests using explicitly qualified dependencies.
- Refuse an explicitly null selector; only an absent selector means the
  previous stdlib-only behavior. Validate the same way in environment_plan.
- Add regression coverage for all three stages and null refusal. No change
  to capacity, routes, claims, request budgets or sandbox privileges.

Changed source paths are fiber_worker.py, fiber_test_environment.py and
their two existing test files under ops/fiber-f1be0929. This document is the
only additional path.

## Exact checks

Before the source correction, four selected test methods ran in the private
namespace: two failed subtests and two errored subtests reproduced null
acceptance and the implementation/review restriction. These are subtest
counts, not four failed methods.

After correction, the environment module's 18 tests and six focused worker
tests passed: 24 tests, 0.006 seconds, exit 0. Commands used python3 -m
unittest within ops/fiber-f1be0929, selecting tests.test_fiber_test_environment
and the six environment-related WorkerTest methods.

The combined fleet suite used this candidate's ops directory plus the exact
separately reviewed capacity12 admission module and tests from
`0fed1d40ad5774e5ead8f7694ac373d93639b8ad`:

```text
python3 -m unittest discover -s tests -q
Ran 148 tests in 1.221s
OK (skipped=1)
```

That means 147 passed and one existing cgroup check was skipped. The run was
inside bubblewrap with no host network or credentials. Only the operator's
installed Python site-packages were additionally bound read-only for this
operator test, at /opt/operator-deps. This is not a model-worker mount or a
new worker privilege. FIBER_CANDIDATE_DIR=/work, BASH_ENV=/dev/null and the
existing candidate fill/prompt filenames selected the exact copied suite.
The deployment must retain the separately approved capacity12 module; this
source branch still carries its older capacity9 base.

git diff --check passed. fiber_worker.py is 491 lines.

## Actual application dependency artifact

On ZIOWK01, an operator-built Python3.12.14 venv was created inside a private
namespace at /opt/test-env, with its portable interpreter under that same
prefix. Dependencies were exported using the already qualified uv0.12.5
from exact SKLegal source `14123f5e80d5311af3e2d43112e32b81fdb53260`.
uv.lock SHA256:
`08521e3ac1f5d9f2c9bb7dd67f2c5a8d5e34f0b162508fa3b81c44deb3eca743`.
Exported requirements SHA256:
`95b86fd90e7739dd504eba3cf063b698497c1982e561c4965f561cf956b18f72`.
Locked downloads used --require-hashes. PGPy0.6.0 has no matching wheel;
its hash-verified source distribution was built offline in the namespace.
Build tooling was setuptools84.0.0 and wheel0.48.0. No model downloaded or
installed anything, and no host interpreter environment was modified.

Native build invocation `312488c1d49940d18649611e6054246b` exited0 with
pip check reporting no broken requirements. Its limits were two CPUs,
3GiB RAM,512MiB swap,256tasks and NoNewPrivileges.

With that artifact bound read-only, unchanged application tests passed:

```text
python -m pytest -q tests/test_model_gateway.py \
  tests/test_claim_grounded_drafting.py tests/test_work_product_drafting.py \
  tests/test_domain_entities.py --tb=short
168 passed, 55 subtests passed in 0.53s
```

Native invocation: `897e32d008cc4c01b66ea999943d102a`.
Separate real denial probes confirmed EROFS on writes to /opt/test-env,
absent host SSH/Docker/user-runtime paths and no external network connection.
These operator artifact checks used the installed sandbox plus the proposed
read-only bind, not an installed worker repair. Actual repaired worker
preflight and canary remain required before rollout acceptance.

## Preserved failures and superseding corrections

- Original DeepSeek card14380835 committed its candidate, then exited1
  after the64-request guard. Its receipt records64gateway requests and no
  upstream bridge failures. The worker run is FAILED, not a successful
  completion. Its clean commit and evidence remain preserved for review.
- The original evidence's tests-only contract is superseded here. Its claim
  that fiber_test_environment.preflight exists is incorrect: the actual
  wired probe is fiber_sandbox.environment_probe, in check and execute.
- The original all-wheel dependency download refused PGPy's source-only
  release. The subsequent hash-verified source build succeeded offline.
- A first disposable-prefix build refused its copied managed-Python marker.
  No override was used. A real venv with its own nested portable interpreter
  replaced that disposable build approach; the earlier prefix was retained.
- Load-driver qualification reported20passed,3failed,1skipped because
  tests/test_load_saturation.py calls /work/.tools/bin/uv, not PATH's uv.
  It is not claimed as passing. The targeted168-test application check above
  independently establishes dependency access without changing application
  tests or granting network. The load-driver setup remains a separate limit.

## Remaining gates and rollback

Independent GLM review must inspect the complete combined diff from the
original infrastructure base, not just this small correction. Hosted CI
has not run and is not claimed. No worker runtime changes from this repair
are installed yet. A local-only snapshot commit is authorized by this card.

Install only the reviewed worker, sandbox and new environment helper with
per-host backups under HOLD. Do not overwrite the approved capacity12
admission module. Rollback retains HOLD, restores those exact runtime files,
and parks the new helper/artifact recoverably; no session or workspace is
deleted. No push, merge, deployment or protected Matter processing occurred.
