# Changelog

All notable changes to **skcapstone** are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

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
