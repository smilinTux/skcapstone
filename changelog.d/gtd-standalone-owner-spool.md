### Fixed

- Standalone skcapstone (no skos, i.e. the chi fleet) now honors `coordination/gtd/OWNER`. On a non-owner host, GTD capture, upsert, clarify, move, done, reopen, the dreaming someday writer, and any legacy whole-list save (the skcoord ITIL fallback sink and orphan reaper) append ops to `spool/<host>.jsonl` instead of rewriting the Syncthing-shared lists, which is what forked `inbox.json`/`archive.json` into `.sync-conflict` copies. The owner folds spools in before its own locked writes. New module `skcapstone.gtd_spool` uses the skos#35/#37 record format, spool dir and offsets file, so either implementation can drain the other's spool. Behavior is unchanged with no OWNER file.

### Added

- `skcapstone gtd drain-spool` and the `skcapstone-gtd-drain.{service,timer}` user units (every 10 minutes, install on the GTD owner host only).
