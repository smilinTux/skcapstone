# Dreaming Engine — Setup & Usage

The dreaming engine (`src/skcapstone/dreaming.py`) gathers recent memories, reflects on
them with an LLM, and stores resulting **insights / connections / questions** as new
memories (`~/.skcapstone/agents/$AGENT/memory/dream-log.json`). It is driven by the
**consciousness daemon** on an idle cycle.

## How it's triggered
- Runs inside the **consciousness daemon**: `skcapstone daemon start|stop|status`.
- Fires when the agent is idle (`idle_threshold_minutes`), respecting `cooldown_hours`
  and `max_per_day`. There is **no on-demand CLI trigger** — restart the daemon to load
  config/code changes; it dreams on the next eligible cycle.
- Daily reflection (separate): `agents/$AGENT/scripts/daily-dream-reflection.sh` (4am cron)
  summarizes the last 24h to Telegram.

## LLM provider (the important part)
`DreamingConfig` (in `dreaming.py`) picks the model. Providers: `claude` (CLI), `nvidia`
(NIM), `ollama` (any OpenAI-compatible host). **Default since 2026-06-08: `ollama` →
BeeLlama (abliterated Qwen3.6-27B) on the local GPU.**

### Sample config (consciousness `dreaming:` section, or the dataclass defaults)
```yaml
dreaming:
  enabled: true
  provider: ollama                          # claude | nvidia | ollama
  ollama_host: "http://192.168.0.100:8082"  # BeeLlama, OpenAI-compatible /v1/chat/completions
  ollama_model: "qwen3.6-27b-abliterated"   # served by skai-beellama.service
  temperature: 1.0
  creativity_mode: unhinged                 # conservative | balanced | creative | unhinged
  max_response_tokens: 4096
  idle_threshold_minutes: 30
  cooldown_hours: 2.0
  max_per_day: 1
  # --- anti-repetition guard (the "fix dreaming repetition bug" work) ---
  dedup_lookback: 10                        # compare against last N dreams
  dedup_overlap_threshold: 0.60             # skip insights >60% keyword-overlapping recent ones
  graduation_consecutive_threshold: 5       # graduate a theme after 5 appearances (stop re-surfacing)
  diversity_lookback: 5
  diversity_min_unique_ratio: 0.40          # force memory diversification if recent dreams too similar
```

> The `ollama` provider calls **`/v1/chat/completions`** (OpenAI format) and strips
> `<think>…</think>`. Point `ollama_host` at any OpenAI-compatible server (BeeLlama,
> Ollama's `/v1`, etc.) and set `ollama_model` to a model it serves.

### History / gotcha
The engine stalled **2026-05-03**: the old default `provider: claude` died with an OAuth
degradation, and the ollama fallback was hardcoded to `deepseek-r1:32b` (not present on the
host). Fix = repoint to BeeLlama abliterated (above) + make `_call_ollama` speak OpenAI
chat format. See `BeeLlama` / `skai-beellama.service` on the GPU host.

## Reviewing dreams
- **Interactive:** the `dream-review` skill (`~/clawd/skills/dream-review`) — extract →
  present → ComfyUI Flux art (`scripts/generate-dream-art.sh`) → file to GTD/seeds.
- **Weekly (automated):** Hermes cron `weekly-dream-reflection` (Sun 9:13) → Lumina
  summarizes the week + a mood image → Telegram DM → **archives that week's dream items out
  of the GTD inbox** (`~/.hermes/scripts/dream-week-prep.sh`).
- **Catch-up / backlog (one-off):** `~/clawd/scripts/dream_catchup.py` batch-summarizes the
  unique backlog via BeeLlama; `dream_catchup_complete.py` meta-synthesizes, files
  `dream-summaries-<date>.md` **and ingests them into the pg `docs` store
  (`source=dream-summaries`)** for future meta-analysis, then archives the backlog.

## Files
| File | Purpose |
|---|---|
| `src/skcapstone/dreaming.py` | the engine (gather → reflect → dedup/graduate → store) |
| `agents/$AGENT/scripts/daily-dream-reflection.sh` | 4am daily reflection → Telegram |
| `~/.hermes/scripts/dream-week-prep.sh` | weekly digest + GTD archive (Hermes cron) |
| `~/clawd/scripts/dream_catchup*.py` | backlog catch-up (summarize → synth → ingest → archive) |
| `~/clawd/skills/dream-review/` | interactive review skill (+ ComfyUI art) |
