# Native Node test recipes

Root Node projects with `package.json`, `package-lock.json`,
`tests/_setup.mjs`, and `tests/*.test.mjs`, without `apps/web`, now receive a
`node_test` recipe. Each explicit target maps to its positive minimum case
count. A `test_profile_recipe` link can carry a smaller, evidenced baseline:

```json
{"node_test":{"tests/router.test.mjs":1,"tests/config.test.mjs":1}}
```

The existing Node qualification binds the runtime, platform, immutable
`node_modules` artifact and source configuration. For native projects the
configuration fingerprints are the root package, lock and test bootstrap.
The artifact must already be qualified on the execution host. A native
artifact contains read-only `node_modules`, `package.json` and
`package-lock.json`; both manifests must match the source fingerprints.
A Vitest dependency artifact cannot stand in for the root Node dependencies. This feature
neither installs dependencies nor executes package lifecycle scripts.

The trusted worker emits one command per sorted target, equivalent to:

```sh
node --test --import ./tests/_setup.mjs --test-reporter=junit \
  --test-reporter-destination=/output/node-000.xml tests/config.test.mjs
```

Node 22's native JUnit has no file identity. Combining all files into one
`node.xml` cannot establish per-file coverage, so each sealed command has
its own `node-NNN.xml`. The worker runs inside the existing private network,
read-only source and dependency sandbox at `/work`. Independent acceptance
checks every command, exit status, output hash, raw report and positive
per-file count. Nested suites are supported; failures, skips, cancellation,
missing reports and undercounts refuse acceptance. Vitest remains unchanged.

The baseline curator can remove a native file after its exact sealed
qualification command exits 1 and its JUnit failures prove assertions
(`ERR_ASSERTION`). It binds the report to the command and log, preserves
all other targets, and qualifies the reduced recipe again. Import errors,
timeouts and crashes cannot silently become exclusions. Operator recipes
are never overwritten; curator recipes extend only if the failed run used
the current linked recipe. An exclusion does not claim the omitted test passed.

## skgateway verification on 2026-10-12

The five blocked cards' pinned bases contain 158 or 161 native test files
and the required bootstrap and lock. None contains
`tests/metrics-delivery.test.mjs`. On freshly fetched main
`41cc2a00f70430ac55a533c652345afa551d0e1a`, that module is present and its
17 tests pass in the native sandbox. Therefore it is not globally removed
by filename. Where a failed qualification proves its reported historical
assertion failure, the curator excludes the file using the same governed
baseline path as any other native assertion failure.

No live card profile, deployment, or service was changed for this PR.
