# Host-local liveness: real namespace and exclusive authoritative writer

The publisher observes legacy tmux sessions and native systemd worker units
without selecting, claiming, releasing or launching work. Niobe remains the
sole dispatcher. skfleet-rotate.timer remains disabled on retired per-host
rotation installations; do not enable duplicate schedulers.
skfleet-niobe-live.timer remains the sole centralized dispatcher. The consumer requires literal boolean complete=true and
fresh, host-matching evidence from every configured host before absence-based
reaping. Missing, delayed or ambiguous evidence fails closed.

## Namespace contract

The unit uses the actual shared host /tmp namespace (PrivateTmp=no) and
SKFLEET_TMUX_SOCKET=/tmp/tmux-%U/default. The former runtime-only socket
contained an unrelated liveness anchor on four hosts while actual worker
sessions used the default socket. An anchor is not evidence about another
server.

With SKFLEET_ALLOW_ABSENT_DEFAULT_TMUX=1, an absent default socket is empty
only after a successful own-user process query shows no tmux process.
This opt-in applies only to the exact default path. Missing custom sockets,
permission failures, failed process probes and live tmux with a missing
default socket still fail. No server or session is created.

SKFLEET_AUDIT_TMUX_SERVERS=1 additionally joins all own-user tmux server PIDs
to existing socket files under /tmp/tmux-UID and /run/user/UID/skfleet
(tmux*.sock). The publisher queries each real server PID and sessions and
requires stable process membership before/after the observation. Unknown
unlinked servers, failed socket probes or a changing process set deny
authority. Reachable anchor sockets are included as observed sessions,
never substituted for the real default namespace. Hidden servers in other
namespaces remain unknown until their real socket can be observed.

Report tmux_sessions, tmux_sockets, and systemd_units expose actual observed
names, including operator SKLegal units without inventing card bindings.
Only supported fleet naming patterns produce card IDs and exact current
owner/claim-revision records. Diagnostics remain explicitly incomplete when
either runtime observation fails.

## One authoritative writer

Only fleet_live_publisher writes evidence/fleet-live/HOST.json.
Rotation publish_live now writes its lane measurement under
evidence/fleet-lanes/HOST.json, retaining its existing return value and
observations. It cannot overwrite a complete report with weaker empty
process probes. reporting_capacity reads the separate lane file, with
legacy fallback during rollout. The publisher carries this fresh lane
measurement and original lanes_ts; stale measurements become unknown.
The terminal worker invalidator still removes only an exactly released
generation and preserves the original observation timestamp.

## Rollout and rollback

Do not execute these commands on this
source-repair card without explicit operator rollout authorization.

Before changes, independently review exact source and save hash-bound
per-host installed preimages. Update the actual authority dispatcher lane
writer/read path first, preserving unrelated admission changes. Wait for
any older dispatcher cycle to finish naturally before enabling new publisher
units. Update both module and unit on each host, daemon-reload, and invoke
only the read-only publisher. No worker restart or new worker is required.

Verify real sessions/server PID coverage, truthful idle-host state, fresh
per-host reports and actual authority health after transport converges.
An unlinked server must be recovered safely before claiming full quorum;
the source does not guess it away. Never signal a server to recreate a
socket onto another live server's occupied pathname.

Rollback only exact current installed hashes to recorded preimages.
Preserve the existing completeness consumer guard. Reverting namespace
alignment restores missing/anchor-only evidence limitations; do not
represent rollback as recovered authority. Keep canonical source and
receipts, then remove task-owned scratch clones/staged files.
