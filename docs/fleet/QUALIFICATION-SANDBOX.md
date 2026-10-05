# Qualification sandbox provisioning

Production rollout installs `systemd/apparmor/skfleet-bwrap` at
`/etc/apparmor.d/skfleet-bwrap` and reloads that profile using noninteractive
sudo. The asset preserves the deployed chiap08 profile bytes. Rollout saves
any previous bytes under the user's private fleet
`rollout-apparmor-preimages/<sha256>/skfleet-bwrap` directory. It does not
change global namespace sysctls or unrelated profiles. Nonproduction hosts
skip this installation.

The attachment grants bubblewrap setup capabilities and transitions executed
children to the capability-denying `skfleet_unpriv_bwrap` profile. Source
mounts, network isolation, native admission and systemd limits remain the
governed execution boundary. The profile is intentionally broad for file
operations inside that namespace; it is not an independent filesystem
allowlist.

The operator running rollout needs permission to install this one profile
and run `apparmor_parser -r`. Permission, parser or namespace failures halt
rollout. Production readiness executes `/usr/bin/true` through the installed
qualification sandbox on every host, even where the authority timer is
inactive. The probe uses temporary empty source/output directories, strips
`BASH_ENV`, and has bounded timeouts; it runs no card work and claims no test
qualification.

Rollback reinstalls the older profile when that checkout contains the asset.
For a checkout predating this asset, rollback retains the working kernel
profile. A privileged operator can restore preserved bytes if required.
Candidate worktrees must never install or reload profiles on live hosts.


## Readiness must use the worker host context

Both shipped readiness service templates execute the probe directly in the
same namespace/privilege context as `production_builder.service_command`
and `production_tests.service_argv`. They do not add `PrivateTmp`,
`ProtectSystem`, `ProtectHome`, `ReadWritePaths`, or `NoNewPrivileges`.
Their existing timeout remains 120 seconds and their output umask is 0077,
matching worker confidentiality. No additional transient service or launcher
is needed.

The user manager implements mount restrictions using its own user namespace.
That makes the executor's bubblewrap namespace nested, which the deployed
AppArmor/userns policy refuses. On chiap01 the operator measured plain bwrap
and NoNewPrivileges alone succeeding, while each of PrivateTmp,
ProtectSystem=strict and ProtectHome=read-only independently caused refusal.
ReadWritePaths also creates a filesystem namespace, even without
ProtectSystem, as described in the [systemd execution documentation](https://github.com/systemd/systemd/blob/main/man/systemd.exec.xml).
Leaving that old writable exception would retain the same mismatch.

NoNewPrivileges is absent from actual worker unit properties. An independent
local child-process check confirmed it also prevents the existing bounded
read-only sudo call from reading both loaded AppArmor names, producing a
second false readiness failure after the namespace mismatch is fixed. The
trusted observer therefore retains the worker's ordinary agent privileges,
including its existing permission to read that fixed kernel profile listing.
This is a deliberate change to the observer service's host restrictions;
it does not change the candidate sandbox or grant new sudo policy.

Bubblewrap still uses the real sealed executor, unshares all namespaces,
mounts source and runtime read-only, clears its environment and gives the
child private temporary/output paths. The deployed AppArmor profiles still
deny child capabilities, network isolation remains enforced, and native
admission and worker quotas remain unchanged. Readiness still fails on
missing executables, refusal or timeout and checks every production host.
It does not skip the sandbox probe or turn a failed probe into PASS.

`tests/fleet/test_readiness_worker_context.py` compares both readiness
service templates with the actual generated builder and qualification unit
properties for namespace/privilege controls, umask and interpreter selection.
It also preserves the existing sealed-executor and fail-closed assertions.
After rollout the operator must verify the effective installed unit settings
and readiness results on all five hosts. A source test or plain-shell probe
alone is not evidence that the deployed readiness service passes.
