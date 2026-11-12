# Provision native Node dependency artifacts

Run this as the execution user's operator. It installs dependency artifacts,
not a card PASS or qualified test profile. Use source at the card's exact base.

```sh
python -m skcapstone.fleet.production_node_artifact prepare \
  /absolute/sealed/source /absolute/new/private/run --offline
```

Preparation creates a fresh directory, copies `package.json`,
`package-lock.json` and `tests/_setup.mjs`, and runs `npm ci --ignore-scripts`
with a private HOME and empty npm configuration. It uses the governed Node
binary's directory first in PATH. No inherited `NODE_OPTIONS`, npm hooks or
user npm configuration are used. Omit `--offline` only when locked packages
must be fetched. npm verifies package integrity against the lock.

Native dependencies require a separate explicit build or verification. Do not
enable all lifecycle scripts to fix one native package. For skgateway's
`better-sqlite3`, the 2026-10-12 operation compiled the locked package using
node-gyp and verified Node 22.23.3 headers inside a network-isolated bwrap
sandbox. Set CC and CXX to the compiler binaries because `/etc/alternatives`
is not mounted. Record the header archive hash, build command and log.

After that step:

```sh
python -m skcapstone.fleet.production_node_artifact install \
  /absolute/sealed/source /absolute/new/private/run/artifact \
  --operator jarvis --receipt /absolute/new/private/run/install.json
```

Installation copies only dependencies and source manifests, checks exact
bootstrap bytes, removes write permission, verifies the production artifact
digest and publishes under the digest in
`~/.local/share/skcapstone/test-dependencies`. Existing matching artifacts
are reused without modification. Wrong manifests, redirected inputs and
escaping dependency links refuse publication. Failed staging directories have
`.node-artifact-` names and cannot resolve as published artifacts.

The opaque `qualification-marker` retains the existing literal `after!`
format. It is covered by the digest but is not a PASS, provenance receipt or
parsed authority token. The separate operator receipt records who installed
what and explicitly says product tests were not run. The recovered original
procedure at `main1621-frontend-artifact-native-v4-20261005/qualify.py` used no
marker and ran independent tests after publication.

Install each artifact on every execution host, preserve modes and recompute
`artifact_digest` there. Use `validate_environment` and
`qualified_environment` against the exact source, then smoke-test native
bindings inside `production_test_worker.sandbox_command`. Full card tests
remain the responsibility of the card's normal test run. Node runtime, CPU and
OS must match; matching source manifests alone do not prove native ABI support.

`tests/_setup.mjs` is fingerprinted from each workspace. Runtime lookup matches
artifact package manifests; it may reuse either of two artifacts with identical
dependencies and different copied bootstrap bytes. The workspace's exact
bootstrap remains bound in the returned environment and executed from source.
