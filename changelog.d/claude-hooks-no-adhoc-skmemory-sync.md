### Fixed

- The lumina `claude-hooks.md` default no longer tells its reader to run an ad-hoc `skmemory sync` to index flat-only memories. It starts the managed `skmemory-sync@lumina.service` instead (serialized with its timer, logged, carries the unit env); an ad-hoc sync on an older skmemory could write truncated SQLite previews into the flat source of truth.
