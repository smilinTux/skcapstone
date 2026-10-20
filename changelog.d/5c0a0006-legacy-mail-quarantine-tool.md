- New operator tool `scripts/fleet/skmail_quarantine.py` (plan, apply, restore) to move
  malformed legacy skmail records (the ones the reader reports as `INVALID_JSON` or
  `INVALID_SCHEMA` and that make `skmail_writer` refuse every later append for that writer)
  into a content-addressed archive. A different reviewer must PASS the exact manifest hash;
  apply verifies the archive can restore every source first, holds the canonical writer's
  per-mailbox flock while rewriting, and restore re-inserts records while keeping mail
  appended since. Nothing runs automatically. Port of #529 onto current main.
