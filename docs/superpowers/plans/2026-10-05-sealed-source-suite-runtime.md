# Sealed source-suite runtime implementation plan

Goal: dashboard tests import skcapstone inside the sealed prefix, and skstacks controllers receive a pinned Ansible dependency.
Authorization: Chef directs source PRs; lumina-nor owns rollout. SKLegal fan-out is priority 1; no native qualification launch while it needs fleet capacity.
Architecture: change both governed install directions to a regular prefix-contained package install. Add ansible-core==2.21.3 to fleet-qualify and the dependency inventory so it is checked and fingerprinted with its imported controller packages. Keep source, runtime, policy and exact-baseline boundaries intact.

- [ ] Write and observe failing deploy/rollback packaging and controller-inventory tests.
- [ ] Change the shared install commands; add the pinned extra and fingerprinted controller packages.
- [ ] Run affected checks and a throwaway packaged import proof; do not install in the live prefix.
- [ ] Publish the PR and enable authorized rebase auto-merge.
- [ ] Build a separate opt-in host-provisioning PR with pinned restic/shellcheck versions and SHA256, exact host allowlist, idempotence and checksum failure tests. Confirm repository and hosts with lumina-nor before enabling any host.
- [ ] After operator rollout, rerun native qualifications only with available SKLegal capacity and publish genuine profiles before observing dispatch.
