# Opt-in skstacks qualification tools

The existing governed staged deploy and rollback provision restic 0.19.1 and ShellCheck 0.11.0 into `~/.skenv/bin` only on explicitly selected skstacks worker hosts. This location is in the native sandbox's existing read-only runtime mount and sealed PATH. No source, host mount, storage dataset, service or timer is changed by tool provisioning.

The operator supplies `~/.skcapstone/fleet/qualification-tools.json`, owned by the deploying user with mode 0600 and no symlink or hardlink. Its exact schema is:

```json
{"schema": "skfleet.qualification-tools/v1", "hosts": {"worker-example": ["skstacks"]}}
```

Replace `worker-example` with only the selected worker/qualification host names. An absent file or an empty hosts object enables no host. Unknown toolchains, duplicate entries/keys, unsafe host names, non-private files, unsupported architectures and selected non-Linux hosts fail closed. No host is enabled by the repository itself.

Preview without downloads or writes:

```bash
~/.skenv/bin/python -m skcapstone.fleet.qualification_tools
```

The operator's existing staged rollout invokes the module with `--apply`; workers do not install tools. Every required archive and unpacked executable SHA256 is verified before either replacement. Executables are published by the existing atomic rollout artifact writer. An unchanged executable with the pinned bytes and executable mode is left untouched and causes no download. Failed verification preserves both existing tools. Rollback uses that target revision's manifest; a revision predating the provisioner does not remove independently installed tools.

The checked-in artifact manifest supports Linux x86_64 and aarch64, with archive and executable digests verified against official release bytes: [restic 0.19.1](https://github.com/restic/restic/releases/tag/v0.19.1) and [ShellCheck 0.11.0](https://github.com/koalaman/shellcheck/releases/tag/v0.11.0). Only the regular `shellcheck-v0.11.0/shellcheck` member is read from the tar archive; no archive paths are extracted. Archive reads are bounded at 32 MiB and executable output at 64 MiB.

Deploy the separate sealed-runtime package/ansible-core fix before repository qualification. Qualification retains its exact source/runtime/policy/card bindings, and environment failures are not product-baseline exclusions. SKLegal fan-out is priority 1; this provisioning creates no worker or qualification unit and provides no scheduler override. Run skbackup/dashboard native qualification and GLM work only when the existing fleet admission path has capacity available for priority 2 work.
