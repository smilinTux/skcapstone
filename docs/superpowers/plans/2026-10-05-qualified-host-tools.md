# Qualified host tools implementation plan

Goal: the existing governed staged rollout provisions pinned restic 0.19.1 and ShellCheck 0.11.0 only on operator-selected skstacks worker hosts.
Architecture: a source-controlled artifact manifest holds upstream URLs plus archive and unpacked-binary SHA256 for x86_64/aarch64. A stdlib module reads an owned private bounded host/toolchain allowlist; it plans by default, performs zero downloads or writes on unselected hosts, validates all required downloads before atomically replacing any tool, and reuses the existing rollout artifact writer. The existing deploy and rollback steps invoke it only when that target revision ships the module. No new timer or scheduler.
Authorization: source PR only; lumina-nor performs rollout and supplies the host allowlist. SKLegal fan-out remains priority 1. Missing or empty allowlists enable no host.

- [ ] Write meaningful failing tests for host restriction, dry run, pinned checksum refusal before any write, idempotence, source safety and staged integration.
- [ ] Verify upstream release digests for both supported architectures and record archive/binary pins.
- [ ] Implement the bounded provisioner and existing rollout integration.
- [ ] Run targeted tests, verify real downloaded artifacts in a throwaway prefix only, and required source checks.
- [ ] Publish PR, rebase auto-merge, then await operator configuration and rollout. No live prefix installs.
