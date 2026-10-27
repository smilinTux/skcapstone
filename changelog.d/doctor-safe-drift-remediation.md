### Fixed

- `doctor` memory drift no longer suggests `skmemory sync`, which writes truncated SQLite previews into the flat source of truth. The fix now depends on the drift kind (pg unreachable, SQLite-only, flat-only via `reindex --force`, pg-only).
- `doctor` runs `skmemory health` with the SKMEMORY_* settings the services use (`environment.d`, then `pg.env`), so a stale shell DSN no longer reports healthy agents as degraded.
- doctor and dashboard unit tests no longer reach the host's live skcomms rails or `OLLAMA_HOST`.
