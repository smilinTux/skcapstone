# Selected-host sandbox provisioning implementation plan

Goal: unblock priority 1 SKLegal qualifications on chiap01 and chiap03 through governed main rollout.
Architecture: extend the existing opt-in provisioner with a separate sandbox toolchain. Package the unchanged AppArmor bytes and official Node 22.23.3 archive/binary pins. Install only the regular Node executable in /usr/local/bin, preserving apt, and atomically replace/load the root-owned AppArmor file. A shared read-only check feeds production readiness and drift and executes a bounded network-sealed bwrap probe with the configured interpreter.
Tech stack: Python standard library, existing staged rollout, AppArmor parser, bubblewrap.
Spec: Chef/lumina-nor priority 1 instructions, card a14e59a4.

- [ ] Add failing synthetic tests for selected sandbox hosts, dry run/unselected no-ops, checksum-before-mutation, root ownership/mode and parser invocation, refused/timeout/missing bwrap, loaded-profile and Node-major failures, drift/readiness wiring and actual sandbox Node command path.
- [ ] Run `PYTHONPATH=src ~/.skenv/bin/python -m pytest tests/fleet/test_sandbox_tools.py -q` and retain failure evidence.
- [ ] Extend `selected_host(path, host, toolchain="skstacks")` with strict nonempty unique skstacks/sandbox selection, retaining all safety checks. Add sandbox provisioning behind this exact allowlist to the existing CLI.
- [ ] Add bounded archive/checksum verification and root install calls; no arbitrary shell, global sysctl, apt mutation or host writes from this source task. Install AppArmor bytes root:root 0644, Node root:root 0755, then `apparmor_parser -r` and verify installed checks. Report every subprocess refusal and timeout.
- [ ] Select `/usr/local/bin/node` when present, else `/usr/bin/node`, in Node qualification commands and hashes; require major 22. Add `/usr/local/bin` to the sealed PATH, preserving prefix-first tools and isolation.
- [ ] Wire shared sandbox diagnostics to production readiness and drift; unavailable diagnostics fail closed. Stub host operations in producer tests.
- [ ] Run affected provisioning, deployment, rollback, readiness, drift and Node tests, Black 26.5.1, Ruff and diff checks. Verify a packaged wheel retains exact profile/manifest bytes.
- [ ] Push #941 update with tests, changelog, selected-host instructions and rebase auto-merge. Observe CI through a result or precise blocker. Record source evidence on a14e59a4; lumina-nor rolls out and selects chiap01/chiap03.
- [ ] After actual operator rollout, verify both hosts and unchanged 02/04/08 with read-only probes before notifying w7J:p2 that the hosts are ready. Until verified, report pending and do not claim ready.
