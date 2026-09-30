# chiwk12 worker-account migration

Card fb673cd3, operations parent 3ee76566. The human selected a full worker
runtime migration to skuser01, then explicitly approved bubblewrap installation
and a separate local metadata-only Syncthing replica. Existing mrarch agents,
claims, workspaces, credentials and Syncthing stay in place. W7 remains held.

## Install and qualify

1. Create a separate skuser01 Python venv. Install SKCapstone 0.15.181 using
   chiwk12-runtime-constraints.txt, preserving all 48 dependency versions.
   SKSkills 0.2.0 is unpublished: build its wheel from the clean existing
   source at 14266f0a7e1d99464f5f118d7465e3afff3464da. Wheel SHA256:
   d886e768e763ee05e1b8bf67700aeeb5bb364395dc01b4e4aacb94ba5a114b28.
2. Install the existing Node v24.14.0 and Pi 0.84.4 artifacts under skuser01.
   Compare their bytes with the old artifacts. Do not link writable runtime
   files across accounts. Install only the already-reviewed sandbox, bridge,
   worker and twelve-worker admission modules. Run pip check and all nine
   qualify_sandbox.py kernel checks as skuser01.
3. Generate a NEW native Syncthing identity in
   /home/skuser01/.local/state/syncthing-fleet-fb673cd3. No old key is copied.
   Bootstrap paused, with GUI at 127.0.0.1:8385. Execute the reviewed
   chiwk12_metadata_sync.py once as the chiwk12 administrator. It configures
   loopback-only peer transport, disables discovery/relay/NAT, and installs
   the native ignore filter BEFORE sharing with the existing peer. Only
   cards and explicitly listed coordination/fleet metadata directories sync.
   Mail, attachments, workspaces, evidence, profiles, identity, config and
   secret stores are excluded. A nonempty unexpected replica is refused.
4. Verify API ignore readback, peer connectivity, native sync completion,
   exact card/claim reads, expected directories only and a native coord audit
   link round trip. No raw CardStore files are written by the operator.
   Replace the temporary bootstrap with syncthing-fleet-chiwk12.service.
5. Keep the existing gateway catalog as the secret boundary. Save the ACLs
   of /home/mrarch, .pi, .pi/agent and .pi/agent/models.json without reading
   credential values. Grant skuser01 traversal only on those directories and
   read-only access to that one catalog; point its models.json at the existing
   file. No other old-home access is granted and no credential bytes copied.
   Catalog replacement later requires requalification of this read access.
6. Generate a new return SSH key under skuser01. Pin chiap08's host public key
   obtained over the existing trusted administrator path. The chiap08
   authorized_keys entry must use restrict, from=100.120.22.21, and a forced
   /usr/bin/python3 invocation of the reviewed fiber_return_board.py. Permit
   only the exact native coord show contract used by verify_claim. Test a
   valid card read and denial of shell, mutation and forwarding. The key is
   never mounted into model workers.
7. Run two isolated SDK completions using the actual qualified catalog:
   DeepSeek thinking=disabled and GLM low, nonempty responses, pinned route
   attribution and no credential visibility. Check cgroup limits and source
   materialization using the same worker account before admission.
8. Cut over only after the above passes. Recheck that old sknoded's cgroup
   contains only its own daemon, with no workers; save its enabled/active
   state. Stop/disable just that old node service. Install sknoded-chiwk12.service
   as skuser01's sknoded.service, enable lingering for skuser01, start it and
   require a fresh heartbeat. Existing Syncthing and agent sessions remain.
   The node spec must remain report-only, with no actuation or builder role.
9. Use install_chiwk12_controller.sh to install the reviewed account changes on chiap08 under
   HOLD and the authority lock, with .fb673cd3-before backups. Recheck fresh
   fleet census, sandbox preflight and both provider lanes. Only then run an
   infrastructure canary. Completing this card is not permission to skip
   remaining cluster qualification or start W7 production prematurely.

## Rollback

Retain HOLD. Stop/disable only the new sknoded service and restore the old
mrarch node service to its saved state. Restore controller/probe backups under
the authority lock. Stop/disable the new metadata replica. Using the old
native Syncthing API, remove only the new device from the skcapstone folder's
device list, preserving all other entries, then remove its peer definition
only if no other folder references it. Keep all replicated metadata and new
runtime files for inspection, not deletion. Never restore whole old configs.
Remove only this card's restricted SSH authorization, preserve all other
keys, and restore the saved ACLs. Revert skuser01 lingering only if this task
enabled it and no other new service needs it. No existing agent is killed.

## Evidence limits

Metadata-only is a content restriction, not read-only replication. Native
worker execution links and the new node's heartbeats must propagate back;
the old instance is an existing peer, not a separate authorization authority.
Models cannot reach either replica, SSH key or GUI socket from the tested
sandbox. The trusted skuser01 administrator already has passwordless sudo.

The native API provides a devices-array patch, not a conditional or atomic
per-folder device-add operation. Use a single-operator configuration window;
the helper refuses overlapping copies with a local lock and checks old peer
entries before and after the patch. It does not claim protection against an
uncoordinated administrator writing the GUI in that narrow interval. On any
partial failure, keep HOLD, inspect both native configurations, and remove
only this migration's added peer/folder configuration before retrying. Do not
erase replica files, rewrite CardStore data or overwrite whole native configs.

Static independent review is not hosted CI or proof of live cutover. Report
actual tests and activation separately. No copying of CapAuth, SSH, Syncthing,
provider or agent private identities is authorized. The approved capacity
  module comes from separate card 706f6d7d; this branch retains its old base
admission file and must not overwrite the installed twelve-worker module.

Native configuration and ignore semantics:
[Syncthing config API](https://docs.syncthing.net/rest/config.html),
[Syncthing ignores](https://docs.syncthing.net/users/ignoring.html).
