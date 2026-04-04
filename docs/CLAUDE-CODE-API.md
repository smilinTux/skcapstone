# Claude Code API — OpenAI-compatible wrapper

**File:** `scripts/claude-code-api.py`  
**Port:** `127.0.0.1:18782`  
**Service:** `claude-code-api.service` (systemd user unit)  
**Deployed:** 2026-04-04

## Purpose

Wraps `claude --print` in an OpenAI-compatible HTTP server so OpenClaw (and any
OpenAI-compatible client) can route inference through Claude Code's subscription
instead of a raw Anthropic API key.

This replaces the `anthropic-token-watch` + OAuth injection approach. Instead of
syncing an OAuth token into `openclaw.json` every few minutes, requests go through
the local wrapper which calls `claude --print` directly. Claude Code handles its
own authentication transparently.

## Architecture

```
OpenClaw / client
    ↓  POST /v1/chat/completions
claude-code-api (port 18782)
    ↓  asyncio.Semaphore(1)  [serialise — claude CLI is single-threaded]
    ↓  claude --print --output-format {json|stream-json}
Claude Code CLI  →  Anthropic API (subscription-covered)
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/v1/models` | List available models |
| POST | `/v1/chat/completions` | Non-streaming chat completions |
| POST | `/v1/chat/completions` (stream=true) | SSE streaming chat completions |

## Supported Models

| Model ID | Name |
|----------|------|
| `claude-opus-4-6` | Claude Opus 4.6 |
| `claude-sonnet-4-6` | Claude Sonnet 4.6 |
| `claude-haiku-4-5` | Claude Haiku 4.5 |

OpenAI GPT names (`gpt-4`, `gpt-4o`, `gpt-3.5-turbo`) are accepted and mapped
to equivalent Claude models. Shorthand aliases (`opus`, `sonnet`, `haiku`) also
work.

## OpenClaw Provider Config

Provider name: `claude-code`

```json
{
  "claude-code": {
    "baseUrl": "http://127.0.0.1:18782/v1",
    "apiKey": "none",
    "api": "openai-completions",
    "models": [...]
  }
}
```

### Agent Aliases

| Alias | Model |
|-------|-------|
| `opus-cc` | `claude-code/claude-opus-4-6` |
| `claude-cc` | `claude-code/claude-sonnet-4-6` |
| `haiku-cc` | `claude-code/claude-haiku-4-5` |

## Streaming

Non-streaming requests use `--output-format json` and return a single response.

Streaming requests use `--output-format stream-json --verbose --include-partial-messages`
and emit SSE deltas as Claude produces tokens. The semaphore serialises all
requests regardless of streaming mode.

## What Changed (2026-04-04)

### Stopped
- `anthropic-token-watch.service` — disabled. The OAuth token injection into
  `openclaw.json` and the systemd override is no longer required since the
  `claude-code` provider uses `claude --print` directly.

### Started
- `claude-code-api.service` — new service running on port 18782.

### OpenClaw config updates
- Added `claude-code` provider to `models.providers`
- Added aliases: `opus-cc`, `claude-cc`, `haiku-cc`
- Lumina primary model: `claude-code/claude-opus-4-6`
- Artisan primary model: `claude-code/claude-sonnet-4-6`
- Default fallback list includes `claude-code/claude-sonnet-4-6`

### Fallback chain (Lumina)
```
claude-code/claude-opus-4-6
→ claude-code/claude-sonnet-4-6
→ anthropic/claude-opus-4-6      (OAuth token, may expire)
→ anthropic/claude-sonnet-4-6   (OAuth token, may expire)
→ nvidia/moonshotai/kimi-k2.5
→ nvidia/moonshotai/kimi-k2-instruct
→ ollama/qwen3:14b
```

## Service Management

```bash
# Status
systemctl --user status claude-code-api.service

# Logs
journalctl --user -u claude-code-api.service -f

# Restart
systemctl --user restart claude-code-api.service

# Test
curl http://127.0.0.1:18782/health
curl http://127.0.0.1:18782/v1/models
curl -X POST http://127.0.0.1:18782/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-haiku-4-5","messages":[{"role":"user","content":"hi"}]}'
```

## Known Limitations

- **Single-threaded:** Claude Code's CLI is not concurrent. Requests queue via
  `asyncio.Semaphore(1)`. High request rates will result in latency, not errors.
- **No tool use:** `claude --print` does not expose tool_calls in the standard
  OpenAI format. Tool calls are consumed internally by Claude Code.
- **Session isolation:** Each request uses `--no-session-persistence`, so there
  is no cross-request memory at the API level.
- **Streaming granularity:** Token-by-token streaming requires `--include-partial-messages`.
  Streaming granularity depends on how frequently Claude Code emits partial events.
