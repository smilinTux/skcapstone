# Sealed source-suite runtime implementation plan

Goal: dashboard tests import skcapstone inside the sealed prefix, and skstacks controllers receive a pinned Ansible dependency.
Authorization: Chef directs source PRs; lumina-nor owns rollout. SKLegal fan-out is priority 1; no native qualification launch while it needs fleet capacity.
Architecture: change both governed install directions to a regular prefix-contained package install. Add pinned ansible-core to fleet-qualify and check controller/core availability in the dependency inventory. Preserve the #916 scoped pytest fingerprint; do not hash unrelated application bytes. Keep source, runtime, policy and exact-baseline boundaries intact.

- [x] Write and observe failing deploy/rollback packaging and controller-inventory tests.
- [x] Change the shared install commands; add the pinned extra and required controller/core inventory.
- [x] Run affected checks and a throwaway packaged import proof; do not install in the live prefix.
- [x] Publish PR #940 and enable authorized rebase auto-merge.
- [ ] Build a separate opt-in host-provisioning PR with pinned restic/shellcheck versions and SHA256, exact host allowlist, idempotence and checksum failure tests. Confirm repository and hosts with lumina-nor before enabling any host.
- [ ] After operator rollout, rerun native qualifications only with available SKLegal capacity and publish genuine profiles before observing dispatch.
