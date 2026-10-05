# Retained-owner native Codex boundary

Card `4ceb0aa3` chooses option A for an explicitly authorized native Codex CLI
operator run. Codex uses `--sandbox danger-full-access` and approval `never`
**inside** fleet bubblewrap. There is no unsandboxed host fallback. Automatic
Pi workers through SKGateway are a separate existing route and are unchanged.

The native unit calls `skcapstone.fleet.native_codex.run` after
`production_admission.reserve_launch` and `start_reserved`. It must already
hold the exact doing claim, a consumed admission marker, the current unit
invocation and qualified test profile. The helper checks those bindings, the
registered repository and source ancestor, the retained agent profile directory
and the native resource allowance before launching Codex. Preserved partial
source is allowed; it is not accepted merely because it is present.

## Security boundary

- The outer bubblewrap creates user, PID, IPC and other namespaces, drops all
  capabilities, keeps `NoNewPrivs`, and makes its root filesystem read-only.
  A kernel-state guard refuses the inner command on a normal host shell.
- Only the exact private workspace and container scratch space are writable.
  System libraries and the selected installed Codex runtime are read-only.
  Host home, private packet directories, CardStore and the user systemd bus
  are not mounted. Lifecycle writes remain with the external native controller.
- The existing owned, private Codex authentication file is an explicit
  read-only input. It is never copied into the source workspace or written by
  the worker. It remains readable by the coding process; this does not claim
  credential isolation from that process. An expired credential fails rather
  than granting write access to host authentication.
- Networking is shared for the operator-approved coding provider transport.
  This is not a network-sealed test container. The route admits only explicitly
  dispatch-approved source-only Codex work and refuses private, protected,
  sensitive, human-held and do-not-claim labels. The operator must authorize
  only public source and synthetic inputs. No protected-data permission or
  general provider-route change follows from using this helper.
- `--clearenv` prevents `BASH_ENV`, inherited credentials, Git injection and
  host configuration from crossing the boundary. Codex ignores user config;
  the explicit model and retained owner are recorded by the controller.

## Candidate tests and acceptance

Do not call another bubblewrap or issue host systemd commands from the coding
container. Required candidate tests run through the existing trusted native
host executor **outside** the coding container, with exact source, read-only
candidate/runtime mounts and networking disabled. They have no Codex credential
mount. The coding worker prepares an immutable source candidate and test-file
inventory; the external controller runs the governed tests and retains their
actual output before acceptance. No plain host pytest fallback or fabricated
profile is allowed. A successful coding process exit is not product acceptance.

This requires no host AppArmor profile, sysctl, global Codex config or test
executor change. Deploy only merged main through the normal gate. Native
operators must import the installed helper, not point a production interpreter
at a candidate worktree. For `17818226`, preserve the original and partial
handoffs, retain its owner and claim, verify the rolled-out module, and launch a
fresh generation through the same admission APIs. Do not resume its stopped
historical session or mutate frontend card ownership.
