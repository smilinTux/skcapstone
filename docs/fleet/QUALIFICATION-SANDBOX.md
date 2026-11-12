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

Readiness retains `NoNewPrivileges`, private tmp and read-only home/system
protections. On Ubuntu these filesystem protections create their own user
namespace under `unprivileged_userns`, which cannot create the worker's nested
bubblewrap namespace. Its gate therefore requests a sibling diagnostic service
from the user manager, using only the installed fixed probe module. That short
service uses the same host context as native workers, a 30-second runtime,
one CPU, 256 MiB memory and 64-task limit, and is collected on exit. It accepts
no card, source path, arbitrary command or profile mutation. It checks pinned
root-owned profiles, loaded enforcement and sealed Python/Node execution.
The only elevated diagnostic is the existing fixed read of AppArmor's loaded
profile list; there is no installation, profile reload or global sysctl change.
Probe services run no card work and do not count as builder launches.
