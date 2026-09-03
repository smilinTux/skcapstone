# Fleet worker cgroup migration

Card: `3b227de2`

## Why this changes

The rotation unit is a `Type=oneshot` launcher. Under systemd's default
`KillMode=control-group`, its descendants were killed when the launcher exited.
The existing `keep-workers.conf` changed that to `KillMode=process`, which kept
workers alive but also kept the tmux server and worker descendants in
`skfleet-rotate.service`'s cgroup. The source history and the comments in the
removed drop-in record both observations, including zero-byte worker logs from
the original control-group teardown.

The retained cgroup is resource-accounting hygiene, not timer liveness. The
oneshot main process exits successfully and `skfleet-rotate.timer` can schedule
the next activation while descendants remain in the inactive service cgroup.
The warning means the prior service cgroup is not empty. It does not mean that
the timer stopped.

## New launch model

New workers use one systemd transient user service per card:

```text
systemd-run --user --quiet --collect --service-type=exec \
  --unit skfleet-worker-<lane>-<card>.service \
  --property=KillMode=control-group \
  --working-directory <workspace> bash -lc <worker-wrapper>
```

This command asks the user manager to create a sibling service beneath the user
manager, rather than leaving a descendant in `skfleet-rotate.service`. `--collect`
removes inactive transient unit metadata. `KillMode=control-group` gives the
worker service ownership of its own descendants. The existing wrapper remains
unchanged: it traps `HUP`, `INT`, `TERM`, and `EXIT`, and releases only the exact
owner and claim revision that it launched.

The migration is additive for observation. Slot counting and live publication
read both:

* legacy `codex-auto-*`, `glm-auto-*`, `qwen-auto-*`, and `esc-auto-*` tmux
  sessions;
* running `skfleet-worker-<lane>-<card>.service` transient services.

Therefore existing tmux workers keep their slots, publication, claim fencing,
and reaper protection until they finish naturally. No tmux session is moved or
stopped. New services keep the same lane, card, owner, claim revision, launch
record, zero-log stall rule, quorum publication, and exact-generation release
semantics used by the reaper.

## Install after review

Do not perform these steps as part of this source card.

1. Merge and install the reviewed scheduler source through the normal deployment
   process.
2. Remove the installed `skfleet-rotate.service.d/keep-workers.conf` drop-in,
   which this change deletes from source.
3. Run `systemctl --user daemon-reload`.
4. Let the next timer activation launch new workers. Do not stop existing tmux
   workers.
5. Verify new worker units with
   `systemctl --user list-units 'skfleet-worker-*.service'` and verify that
   `systemctl --user status skfleet-rotate.service` no longer reports retained
   descendants after its oneshot exits.

## Rollback

1. Restore the prior scheduler source and the reviewed
   `keep-workers.conf` drop-in containing `[Service]` and `KillMode=process`.
2. Run `systemctl --user daemon-reload`.
3. Do not stop transient services or legacy tmux sessions. Both remain visible to
   the prior migration-aware release only while rolling back the whole commit,
   so schedule rollback after active transient workers finish, or preserve the
   dual-observation code until they do.
4. Confirm the timer remains active. The old retained-cgroup warning is expected
   until all legacy descendants exit.

Rollback changes future launches only. Claims must never be released merely to
perform the rollback.
