# Changelog

All notable changes to **skcapstone** are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

## [0.14.0] - 2026-07-03

### Added
- **Per-sender consciousness rate limiting.** The consciousness loop now
  throttles inbound message intake with a thread-safe, per-sender sliding
  window (`_RateLimiter`). Over-limit messages are skipped (logged, never
  crashing the loop); each sender has an isolated window that resets over time,
  and sender identities are normalized before counting. Configurable via new
  `ConsciousnessConfig` keys `rate_limit_enabled` (default `true`),
  `rate_limit_max_messages` (default `20`), and `rate_limit_window_s`
  (default `60.0`); a non-positive `rate_limit_max_messages` disables limiting.
- **Startup pillar-degradation health check + notify.** New
  `skcapstone.health` module (`startup_health_check` / `degraded_pillars`)
  evaluates every pillar's status at startup and emits a single `critical`
  desktop notification (reusing `skcapstone.notifications`) summarizing any
  `DEGRADED` / `ERROR` pillars. Healthy startups (all pillars `ACTIVE` or
  `MISSING`) notify nothing. Wired into `runtime.py`.
- **Message-classification logging + `consciousness classification` CLI.**
  `ConsciousnessMetrics.record_classification()` tracks per-tag counts
  (persisted in daily snapshots and surfaced in `to_dict()` /
  the `/consciousness` endpoint as `classification_usage`). The loop now emits
  an INFO `Classified message` log record (sender, tags, ~tokens, privacy) and
  records the tag distribution — observability only, routing behavior is
  unchanged. New `skcapstone consciousness classification` command shows
  today's tag distribution as a Rich table (with `--json-out`), reading the
  live daemon first and falling back to today's daily metrics file.
- **Recommended GFS backup cron + docs.** New `scripts/skcapstone-gfs-backup.sh`
  writes compressed, checksummed tarballs of the *irreplaceable* `~/.skcapstone`
  state on a Grandfather-Father-Son rotation (14 daily / 8 weekly / 12 monthly /
  2 yearly), excluding the rebuildable vector store + `index.db` and transient
  churn (comms queues, logs, skwhisper cache, media renders) so a ~0.8 GB home
  compresses to ~80 MB and the whole rotation stays a few GB. Includes a 2 GB
  free-space guard (fires `sk-alert` on low disk) and per-file `.sha256`
  sidecars. Optional **off-site 3-2-1 replication**: set `OFFSITE_DEST` in
  `~/.skcapstone/config/backup.env` and each run also `rsync`s the whole
  rotation to another host (best-effort — a failed push alerts but never fails
  the local backup). Documented in [docs/BACKUP.md](docs/BACKUP.md) alongside the
  portable `skcapstone backup` CLI, with a cross-link from
  [docs/HOUSEKEEPING.md](docs/HOUSEKEEPING.md) (backup preserves / housekeeping
  prunes) and a Documentation-table row in the README.
### Changed
- **ITIL → GTD is now a push adapter on the skos `gtd-ingest` port.**
  `itil.py::_gtd_emit()` builds `GtdCapture(source="itil", source_ref=<id>)` and
  routes incidents/problems/changes through `skos.gtd_ingest.capture()` (deduped by
  ID, idempotent), with a legacy fallback if skos isn't importable. Same sev →
  next-action/inbox routing; the store is now unified with all other GTD sources.
  See skos `docs/gtd-ingest-architecture.md` + `docs/gtd-ingest-SOP.md`.

---

## [0.13.0] — 2026-06-16

### Added
- **Legacy & broadcast comms-outbox sweep in housekeeping.** New
  `prune_legacy_comms()` sweeps the v1 outbox layouts that the v2-only
  housekeeping never reached: `~/.skcapstone/comms/outbox/<recipient>/` and
  every `~/.skcapstone/agents/<agent>/comms/outbox/<recipient>/`. Stale
  `*.skc.json` envelopes (>7d) are deleted; a v1 broadcast subdir literally
  named `*` is removed wholesale regardless of age. Wired into
  `run_housekeeping` as the `legacy_comms` target (with dry-run counting via
  `_count_stale_legacy_comms`) and surfaced in the `skcapstone housekeeping`
  CLI table.
- **Weekly housekeeping default job.** A standalone `jobs.d` drop-in
  (`config/jobs.d/housekeeping.yaml`, schedule `0 4 * * 0`) runs
  `skcapstone housekeeping` weekly as a safety net decoupled from the daemon.
  Bundled in package defaults and installed idempotently into
  `~/.skcapstone/config/jobs.d/` on a fresh `init` (never overwrites an
  existing user file).

### Fixed
- Prevents the unbounded profile growth that overheated a Framework 13 laptop
  (462k files in `~/.skcapstone`). Root cause: ~256k stale v1 `recipient="*"`
  presence-broadcast envelopes accumulating in directories literally named
  `*` under the legacy v1 outbox paths, which the existing v2 housekeeping
  never swept.

---

## [0.9.0] — 2026-03-02

### Sprint 15 — Exception Handlers, LLM Retry, Tests, Docs, Systemd, Deps
- Added structured exception handlers across CLI and daemon entrypoints
- Implemented LLM retry logic with exponential back-off in `LLMBridge`
- Expanded test suite: consciousness E2E, cross-package, agent runtime coverage
- Added `systemd` service unit template with watchdog dependency and consciousness flags
- Updated `pyproject.toml` dev dependencies: `pytest-cov>=4.0`, `pytest-asyncio>=0.21`
- Improved inline documentation and docstrings across all pillars

### Sprint 14 — Production Hardening
- ACK (acknowledgement) protocol for reliable SKComm message delivery
- Message deduplication layer prevents duplicate processing under inotify storms
- Input validation hardened on all daemon API endpoints
- Inotify watcher now auto-restarts on `OSError` (inotify limit exceeded)
- Reduced false-positive self-healing triggers via smarter health-check thresholds

### Sprint 13 — CPU Inference Optimization, Daemon E2E, Ollama Fixes
- CPU-only inference path: batching, thread pinning, reduced context window for low-RAM hosts
- End-to-end daemon test: start → send SKComm message → verify LLM response in < 60 s
- Fixed Ollama model-not-found error when model name included `:latest` tag
- `skcapstone daemon start` now waits for Ollama readiness before accepting messages
- `consciousness status` CLI command shows live backends, message counts, and conversation count

### Sprint 12 — Fallback Cascade Fix, llama3.2 FAST Tier, Timeout Scaling
- Fixed `LLMBridge.generate()` fallback cascade — passthrough tier was never reached
- `llama3.2` (2 GB) promoted to primary FAST tier for CPU-only hosts
- Response timeout now scales linearly with model size (configurable via `SKCAPSTONE_TIMEOUT_SCALE`)
- Tailscale transport hostname matching switched to exact match (fixes substring collision)

### Sprint 10–11 — Model Tier Fixes, Context Loader, Exports, Flutter UI
- Three-tier model routing: FAST (`llama3.2`) → STANDARD → CAPABLE (configurable)
- `context_loader.py`: injects agent identity and recent memories into system prompt
- Clean public exports from `skcapstone.__init__` (`ConsciousnessLoop`, `LLMBridge`, etc.)
- Flutter dashboard: agent status card, consciousness badge (online/offline), message feed
- `skcapstone coord` CLI surface: `status`, `claim`, `complete`, `list`

### Sprint 9 — Consciousness Loop, Prompt Adapter, Self-Healing
- `consciousness_loop.py`: autonomous message-processing loop backed by SKComm inotify watcher
- `prompt_adapter.py`: `ModelProfile` + `PromptAdapter` normalise prompts across Ollama model families
- `self_healing.py`: `SelfHealingDoctor` monitors pillars, auto-remediates common faults
- `ConsciousnessConfig` dataclass — YAML-driven configuration for all loop parameters
- `/consciousness` HTTP endpoint exposes live status (backends, counters, conversations)

---

## [0.1.0] — 2025-11-01 (initial release)

### Added
- Core pillar scaffold: identity, memory, trust, security, sync, skills
- `skcapstone status` CLI with Rich table output
- MCP server with `memory_store`, `memory_search`, `coord_status`, `coord_claim` tools
- CapAuth PGP fingerprint identity verification
- Coordination board (YAML-backed): tasks, agents, priorities
- `skcapstone context --format claude-md` for Claude Code integration
