# Card 4ceb0aa3: native Codex namespace boundary

The `17818226` operator run stopped because Codex's shell sandbox refused a
nested bubblewrap namespace before pytest ran. It preserved its original
source and partial handoff; no new preparation tests ran. Outside that coding
sandbox, the installed trusted bubblewrap executor ran successfully with
AppArmor user-namespace restrictions still enabled.

The change adds an explicit guarded native Codex operator route, choosing
option A. Only the enforced fleet outer container executes the Codex command
with its own sandbox disabled. The registered source, exact consumed admission,
current claim/profile, private retained identity and real unit are required.
The outer container has a read-only root, bounded writable workspace/scratch,
dropped capabilities and a kernel guard; it never falls back to host execution.
The independent qualified test executor remains unchanged.

Validation:

```text
env -u BASH_ENV PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 ~/.skenv/bin/python -B -m pytest tests/fleet/test_native_codex.py tests/fleet/test_production_admission.py tests/fleet/test_monorepo_test_sandbox.py tests/fleet/test_production_test_profile.py -q --tb=short -p no:cacheprovider
```

141 tests pass on chiap08. This includes a real outer namespace/mount execution
with a synthetic Codex executable and synthetic authentication file, direct-host
guard rejection, environment/mount restrictions, exact admission and claim
refusals, changed profile and source binding refusals and Git environment
injection refusal. Existing admission, sealed monorepo/asyncio test execution
and profile tests remain passing. No provider or production credential is used
by these tests. Black and Ruff pass for the affected Python files.

Hosted full-suite checks run on the PR. This evidence does not claim a real
Codex provider completion from the candidate branch, a production installation,
an `17818226` artifact PASS, frontend trial release or full-browser acceptance.
The intended post-merge operator relaunch is the first live adoption of this
new helper. Authentication visibility and shared coding transport networking
are stated explicitly in `docs/fleet/native-codex-boundary.md`; acceptance tests
remain separately sealed and do not mount that credential.

No data/schema migration, AppArmor change, user-namespace sysctl change,
runtime installation or global sandbox relaxation was performed. All changes
are forward source commits, with deployed adoption left to the rollout owner.
