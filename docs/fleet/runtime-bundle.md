# Fleet launcher runtime bundle

The fleet launcher is released as a closed directory, never as an individual
script. `scripts/fleet/skfleet_runtime_bundle.py` is the fail-closed verifier and
installer.

## Manifest contract

The JSON manifest has `schema_version: 1`, a unique `release`, a relative
`launcher`, and a `files` array. Every file entry has exactly these fields:

- `path`: safe path relative to the release root
- `sha256`: lowercase SHA256 of the file bytes
- `size`: byte count
- `owner`: operating system account name
- `mode`: four-digit octal mode
- `source_commit`: full 40-character source commit
- `required_by`: paths in the same manifest that require this file

The payload must contain exactly the listed files. Extra files, missing files,
unknown edges, mixed hashes, wrong metadata, unsafe paths, and duplicate JSON
keys are rejected. Static closure follows repository-local Python imports and
explicit `.py`, `.json`, and `.schema` references transitively. This catches the
measured `skfleet-rotate.py` without `skfleet-worker-wrapper.py` failure.

Activation also requires hashed artifacts for `independent_review`, `release`,
`canary`, and `five_host_rollout`. These are separate evidence references. They
do not infer a verdict from release lifecycle state.

## Qualification and activation

Build the payload from one reviewed source commit, populate all manifest hashes,
and run:

```bash
python3 scripts/fleet/skfleet_runtime_bundle.py runtime-manifest.json payload --verify-only
```

Five-host qualification must run this against a clean runtime directory on each
host, invoke a normal worker through the installed launcher, capture its exit
evidence, and separately demonstrate rejection of a payload with the wrapper
removed. Put the independently reviewed evidence artifact hashes into the
manifest only after review, release qualification, canary qualification, and all
five host results exist.

Activate only then:

```bash
python3 scripts/fleet/skfleet_runtime_bundle.py \
  runtime-manifest.json payload --runtime-root "$HOME/.local/lib/skfleet-runtime" \
  --health-command python3 scripts/fleet/skfleet-worker-wrapper.py --help
```

The installer copies into a private staging directory, verifies every staged
byte and metadata item, renames the complete directory into `versions/`, and
atomically changes the `current` symlink. If the post-activation command fails,
`current` is atomically restored to the exact prior release and the failed new
release is removed. It never mutates files in an active release.
