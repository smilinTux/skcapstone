# Fleet service runtime artifact contract v1

`skfleet-service-runtime/v1` is the source and policy contract for service
runtime delivery. It does not authorize deployment. Callers must separately
satisfy fleet actuation policy before binding the provider-free promotion
boundary to a service manager.

## Immutable inputs

Every manifest is closed and pins:

- service and target host
- canonical repository, full commit object ID, and full tree object ID
- runtime kind and one or more named artifact SHA-256 digests
- dependency lock, configuration, and unit SHA-256 digests
- bounded health probe
- rollback artifact digest
- opaque data and credential references, never their values

The canonical remote rule is mechanical. SKLegal and HammerTime use
`https://skgit.skstack01.douno.it` only. Every other repository uses GitHub.

Python artifacts are wheels used to construct a dedicated version directory.
Node and script services use sealed bundles. A runtime version is derived from
immutable manifest inputs. Checkouts, worktrees, temporary paths, shared venvs,
editable installs, and unversioned copied source are not runtime artifacts.

## Transaction order

`runtime_artifacts.promote` performs this order:

1. Hash every source artifact before creating or changing runtime paths.
2. Copy into a new version staging directory and hash the copies again.
3. Rename staging to its content-derived version directory.
4. Atomically replace the `current` symlink.
5. Atomically record `installed.json` with commit, tree, and artifact identity.
6. Invoke the injected startup boundary, then the injected health qualifier.
7. Restore the prior `current` target and record rollback if either fails.

No service command, downloader, secret reader, restart, or live mutation is
implemented by this contract.

## Mechanical guard

`guard_service_runtime` resolves service executable and interpreter paths and
rejects mutable locations. For Python it invokes the exact interpreter with
`-I` and inspects each distribution's `direct_url.json`. This detects PEP 610
editable metadata regardless of whether a worker used `python -m pip`, a shell
wrapper, or a pip executable by absolute path. Explicit, isolated developer
roots can be allowed by callers, but must never be used in service definitions.

An example closed manifest is in
`examples/service-runtime-manifest-v1.json`. Digests and object IDs there are
shape examples only, not deployment authorization.
