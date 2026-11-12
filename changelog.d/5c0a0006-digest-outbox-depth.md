- The fleet digest (`scripts/fleet/skworld-digest.py`) now raises a warning when the
  sync outbox (`~/.skcapstone/sync/outbox`) has held more than 50 files for over an
  hour (measured by the age of the 51st newest file, so one stale seed does not
  alert). Missing or unreadable outbox reads as unavailable, never as an alert.
  Port of #415 onto current main.
