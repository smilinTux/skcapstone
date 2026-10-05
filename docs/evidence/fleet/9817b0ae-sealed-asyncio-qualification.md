# Sealed asyncio qualification repair

Card: 9817b0ae, owner jarvis, claim b168149301fe479daf47cb44d6783236.

Native 17818226 qualification on installed 7ac56831, retained owner codex-four-journey-prep and claim a5529b092e924d11b2e96e621f1cca75, actually launched as skfleet-worker-17818226-python-qualification-7ac56831.service, invocation b52cfef266084cdeb4d73e6bca58aa8b. Exact source head 26459ac65bf2ac375caddab3ac9c99dc59ec621b and tree 6ad19d0779986fdc7dc064bc8b514f0bbc56db2d remained clean. Native result and JUnit are retained under /srv/sklegal-fast/private/17818226-native-clear-20261005. The executor returned pytest exit 4: Unknown config option: asyncio_default_fixture_loop_scope. No tests passed and no profile was published. Installed strict occupancy recorded terminal failure and recovered its resource charge. No source/card ownership/history or production install changed.

Cause: the source declares strict asyncio configuration; pytest-asyncio is installed, but PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 excludes it. The smallest repair explicitly loads only pytest_asyncio.plugin through the cleared sandbox environment, preserving autoload isolation, network namespace, read-only source/runtime and trusted output. The scoped fingerprint includes that plugin's code and distribution metadata because both now affect execution. No timeout, source pin or coverage minimum is weakened.

Regression: all three new cases failed on main (strict async execution and two plugin fingerprint mutations). Final affected checks: 201 passed, no skips, in 6.62s:

```
env -u BASH_ENV -u VIRTUAL_ENV PYTHONPATH=src /tmp/skcapstone-base-venv/bin/python -m pytest -q tests/fleet/test_monorepo_test_sandbox.py tests/fleet/test_production_test_profile.py tests/fleet/test_production_tests.py tests/fleet/test_production_test_node.py tests/fleet/test_profile_generations.py tests/test_ci_throughput_workflow.py
```

An intermediate portability check was 200 passed / 1 failed because its temporary virtualenv mount was hidden by the sandbox /tmp overlay. The test now mounts its own isolated interpreter at /test-runtime. The production sandbox mount contract stays unchanged. Earlier broad checks passed 177 cases before this test portability adjustment. Final Black, Ruff and git diff --check pass for changed files. Existing unrelated F811 fixture-import warnings were avoided by leaving their file byte-identical to main.

Diagnostic only: candidate executor, installed runtime mounted read-only, exact transported264 source, installed sealed sandbox, all five selected files: 88 passed in 2.94s, 55 authority + 4 fixture + 2 Claim target + 15 Claim ledger + 12 synthetic dry-run tests, zero failures/errors/skips. Raw log and JUnit are under /tmp/jarvis-9817b0ae-exact264-diagnostic. No qualification, profile, native admission or deployment record was minted from candidate code. A fresh native qualification must run after the merged-main rollout.

Rollout and rollback: no candidate install. Chef/nor rolls merged main through the existing gate. The trusted harness and toolchain fingerprint changes, so both the Python qualification and the exact d53 frontend profile need fresh governed evidence for that runtime. Earlier attempts remain immutable. Reverse this PR through a normal forward revert if required; never rewrite profile history or source pins.
