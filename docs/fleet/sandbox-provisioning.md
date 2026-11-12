# Governed sandbox prerequisites

Card a14e59a4 supplies the missing source for the fleet's previously hand-installed AppArmor attachment and Node 22 prerequisite. The two named AppArmor profiles are packaged together in `src/skcapstone/data/skfleet-bwrap.apparmor`, copied byte for byte from chiap02 and chiap08. Both files were root:root 0644, 951 bytes, SHA256 `309919f9d569a90e8e777d300727c90cfc047bc1cf207733a94467f759bb16a1`; their diff was empty. Preserve global user-namespace restrictions. The child profile still audits and denies capabilities. The source comment records the upstream AppArmor GPL-2.0 provenance.

The existing staged deploy and rollback tool step also processes the `sandbox` toolchain in the private operator allowlist `~/.skcapstone/fleet/qualification-tools.json`. Repository defaults select no hosts. Lumina-nor owns this file and rollout. For the priority 1 SKLegal repair, merge these entries into any existing selection, preserving other hosts and toolchains:

```json
{
  "schema": "skfleet.qualification-tools/v1",
  "hosts": {
    "chiap01": ["sandbox"],
    "chiap03": ["sandbox"]
  }
}
```

Use directory mode 0700 and file mode 0600. Entries may contain `sandbox`, `skstacks`, or both once each. A sandbox-only host receives no restic or ShellCheck changes. Hosts absent from the allowlist perform no downloads, privileged commands or writes. A normal dry run also makes no changes. Do not add chiap02/04/08 to the sandbox selection just to check them.

The sandbox installer uses the official [Node 22.23.3 release](https://nodejs.org/dist/v22.23.3/) and pins both the [published SHA256](https://nodejs.org/dist/v22.23.3/SHASUMS256.txt) of each Linux x64/arm64 tarball and the independently verified regular `bin/node` member. It extracts only the Node runtime executable, bounded to 256 MiB after verifying the bounded 64 MiB archive. npm/npx packages and apt's `/usr/bin/node` are preserved. The runtime goes to `/usr/local/bin/node`, root:root 0755. Node qualification uses this path when present, otherwise the existing `/usr/bin/node`. Both paths are under the sandbox's existing read-only `/usr` mount. The sealed PATH retains the runtime prefix first, then `/usr/local/bin`, then `/usr/bin`.

All checksum and destination checks precede privileged changes. Each replacement is staged root-owned in its destination directory and renamed atomically. Governed rollout requires noninteractive operator sudo for the fixed install/mv/rm commands and `/usr/sbin/apparmor_parser -r /etc/apparmor.d/skfleet-bwrap`. Profile loading or verification refusal makes rollout fail. Matching artifacts preserve their bytes and mtimes; a matching profile that is unloaded is loaded again. No apt, sysctl, global userns exception, exports, storage or service enablement is changed by this provisioner.

Production readiness and drift use the same read-only diagnostics: exact root-owned profile bytes/mode, both enforcing loaded names, and two bounded 10-second bubblewrap executions under `--unshare-all`. The Python probe uses the configured interpreter and asserts a network namespace with only loopback and zero effective capabilities. Node major 22 is checked inside the same mount contract, not from a user's shell PATH. Reading kernel profile names may require bounded read-only `sudo -n /usr/bin/cat /sys/kernel/security/apparmor/profiles`; an unreadable list fails closed. Missing executables, refusal and timeout fail the gate.

After rollout, execute the installed `sandbox_tools.readiness` check on chiap01 and chiap03 and also verify chiap02/04/08 remain ready without selecting them for installation. Report actual results to the fleet lane before asserting sandbox-ready. Existing Node qualification environments remain bound to their executable SHA and source/harness fingerprints; changed Node or harness bytes require real requalification. This source task does not grant qualification PASS or fleet launch capacity.

Rollback to a revision with these sources restores its declared pins through the same governed selection. A revision predating sandbox provisioning skips that provisioning; it does not remove the independent system prerequisites. For emergency reversal, the operator must restore the previously approved profile/Node executable and reload the profile through a separately governed operation. A failed install can leave an earlier verified replacement in place; per-file atomicity does not imply a transaction across the profile and Node. The failing rollout must be corrected and verified before dispatch.
