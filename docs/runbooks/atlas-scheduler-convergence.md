# ATLAS scheduler convergence and rollback

The checked-in ATLAS source of truth is `data/systemd/skoperator.service` and
`data/systemd/skoperator.timer`. `skoperator schedule-doctor` is read-only: it
compares those files with `systemctl --user cat`, including local drop-ins.

## Safe migration

1. Keep ATLAS frozen and run `skoperator schedule-doctor`.
2. Save the current effective definitions with `systemctl --user cat`; ensure the
   backup is mode 600. Do not copy environment values into the repository.
3. Review every reported extra/changed directive. Remove obsolete drop-ins or
   deliberately fold them into the checked-in source through review.
4. Install the reviewed source, daemon-reload, and run the doctor again.
5. Run one report-only ATLAS pass before enabling the timer.

## Rollback

Stop/disable the timer, restore the protected unit backup and any reviewed drop-ins,
daemon-reload, and leave ATLAS frozen until `schedule-doctor` reports clean.

Credential rotation is independent of systemd. Rotate values in the protected
SKOS scheduler env file atomically (owner-only regular file), run a dry-run job, then
retire the old credential. Never place secret values in a unit or crontab.
