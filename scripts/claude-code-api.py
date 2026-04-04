#!/usr/bin/env python3
"""
claude-code-api — OpenAI-compatible HTTP wrapper around `claude --print`

Exposes /v1/chat/completions and /v1/models so OpenClaw (and other tools)
can use Claude Code's subscription-covered inference instead of a raw API key.

Architecture:
  - aiohttp HTTP server on port 18782
  - asyncio.Semaphore(1) serialises claude invocations (single-threaded CLI)
  - All modes: claude --print --output-format json  (reliable, no stream-json timeouts)
  - Streaming responses: result is emitted as chunked SSE after the subprocess finishes
    (avoids the 300s timeout caused by opus extended-thinking blocking stream-json stdout)

Usage:
  python3 claude-code-api.py [--port 18782] [--debug]

systemd:
  ~/.config/systemd/user/claude-code-api.service
"""

import argparse
import asyncio
import json
import logging
import time
import uuid
from typing import AsyncIterator

from aiohttp import web

# ─── Configuration ────────────────────────────────────────────────────────────

PORT = 18782
DEFAULT_MODEL = "claude-sonnet-4-6"
REQUEST_TIMEOUT = 600  # seconds per claude call (opus can be slow with large context)
QUEUE_TIMEOUT = 90     # seconds to wait for semaphore before giving up

VALID_MODELS = {
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "claude-haiku-4-5-20251001",
}

# Map OpenAI / shorthand / provider-prefixed names → canonical claude model IDs
MODEL_ALIASES: dict[str, str] = {
    # GPT compatibility
    "gpt-4":              "claude-opus-4-6",
    "gpt-4o":             "claude-sonnet-4-6",
    "gpt-4-turbo":        "claude-opus-4-6",
    "gpt-4o-mini":        "claude-haiku-4-5",
    "gpt-3.5-turbo":      "claude-haiku-4-5",
    "gpt-3.5-turbo-16k":  "claude-haiku-4-5",
    # Shorthand
    "opus":               "claude-opus-4-6",
    "sonnet":             "claude-sonnet-4-6",
    "haiku":              "claude-haiku-4-5",
    # Provider-prefixed (openclaw strips prefix before routing, but handle here too)
    "claude/claude-opus-4-6":    "claude-opus-4-6",
    "claude/claude-sonnet-4-6":  "claude-sonnet-4-6",
    "claude/claude-haiku-4-5":   "claude-haiku-4-5",
    "anthropic/claude-opus-4-6":   "claude-opus-4-6",
    "anthropic/claude-sonnet-4-6": "claude-sonnet-4-6",
    "anthropic/claude-haiku-4-5":  "claude-haiku-4-5",
}

# ─── Globals ──────────────────────────────────────────────────────────────────

log = logging.getLogger("claude-code-api")
_sem: asyncio.Semaphore | None = None


def sem() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(1)
    return _sem


# ─── Helpers ──────────────────────────────────────────────────────────────────

def normalise_model(model: str) -> str:
    """Return a valid claude model ID, falling back to DEFAULT_MODEL."""
    if model in MODEL_ALIASES:
        return MODEL_ALIASES[model]
    # Strip provider prefix e.g. "claude-code/claude-sonnet-4-6"
    if "/" in model:
        model = model.split("/")[-1]
    if model in VALID_MODELS:
        return model
    log.warning("Unknown model %r, using default %s", model, DEFAULT_MODEL)
    return DEFAULT_MODEL


def messages_to_prompt(messages: list) -> tuple[str, str]:
    """
    Convert OpenAI-style messages list to (system_prompt, user_prompt).
    Single-user message → (system, content).
    Multi-turn → formatted conversation ending with 'Assistant:'.
    """
    system_parts: list[str] = []
    turns: list[tuple[str, str]] = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            # Multi-modal content: extract text blocks
            content = "\n".join(
                c.get("text", "") for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            )

        if role == "system":
            system_parts.append(content)
        else:
            turns.append((role, content))

    system = "\n".join(system_parts)

    if len(turns) == 1 and turns[0][0] == "user":
        return system, turns[0][1]

    # Multi-turn: format as conversation
    lines = []
    for role, content in turns:
        prefix = "Human" if role == "user" else "Assistant"
        lines.append(f"{prefix}: {content}")
    lines.append("Assistant:")
    return system, "\n\n".join(lines)


def make_completion_response(
    model: str,
    content: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> dict:
    """Build an OpenAI-compatible chat completion response object."""
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def make_sse_chunk(model: str, delta: str, finish: bool = False) -> str:
    """Format a single SSE data line for streaming chat completions."""
    obj = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"content": delta} if delta else {},
            "finish_reason": "stop" if finish else None,
        }],
    }
    return f"data: {json.dumps(obj)}\n\n"


# ─── Claude subprocess helpers ────────────────────────────────────────────────

async def _run_claude_json(model: str, prompt: str, system: str) -> tuple[str, dict]:
    """
    Run `claude --print --output-format json` and return (text_result, usage_dict).
    Acquires the global semaphore to serialise calls.
    """
    cmd = [
        "claude", "--print",
        "--model", model,
        "--output-format", "json",
        "--no-session-persistence",
    ]
    if system:
        cmd += ["--append-system-prompt", system]
    cmd.append(prompt)

    log.debug("Running (non-stream): %s", " ".join(cmd[:6]) + " ...")

    async with asyncio.timeout(QUEUE_TIMEOUT):
        await sem().acquire()

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=REQUEST_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"claude timed out after {REQUEST_TIMEOUT}s")
    finally:
        sem().release()

    if proc.returncode != 0:
        err = stderr.decode(errors="replace")[:500]
        raise RuntimeError(f"claude exited {proc.returncode}: {err}")

    raw = stdout.decode(errors="replace").strip()
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"claude returned non-JSON: {raw[:200]}") from exc

    if result.get("is_error"):
        raise RuntimeError(result.get("result", "Claude returned an error"))

    text = result.get("result", "")
    usage = result.get("usage", {})
    return text, usage


async def _fake_stream_chunks(text: str, chunk_size: int = 80) -> AsyncIterator[str]:
    """
    Break a completed response into chunks for SSE emission.

    We always use --output-format json (blocking) for the subprocess — stream-json
    mode causes opus extended-thinking to block stdout for minutes before emitting
    any assistant events. Fake-streaming is more reliable and still lets clients
    receive incremental SSE deltas.
    """
    # Emit in word-boundary chunks to look natural
    words = text.split(" ")
    buf = ""
    for word in words:
        buf += word + " "
        if len(buf) >= chunk_size:
            yield buf
            buf = ""
            await asyncio.sleep(0)  # yield to event loop
    if buf:
        yield buf


# ─── HTTP Handlers ────────────────────────────────────────────────────────────

async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "claude-code-api", "port": PORT})


async def handle_models(request: web.Request) -> web.Response:
    now = int(time.time())
    models = [
        {
            "id": m,
            "object": "model",
            "created": now,
            "owned_by": "anthropic",
        }
        for m in sorted(VALID_MODELS)
    ]
    return web.json_response({"object": "list", "data": models})


async def handle_chat_completions(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception as exc:
        raise web.HTTPBadRequest(text=str(exc))

    model = normalise_model(body.get("model", DEFAULT_MODEL))
    messages = body.get("messages", [])
    streaming = body.get("stream", False)

    if not messages:
        raise web.HTTPBadRequest(text="messages array is required")

    system, prompt = messages_to_prompt(messages)

    log.info("→ %s | stream=%s | model=%s | %d chars",
             request.remote, streaming, model, len(prompt))

    if streaming:
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            }
        )
        await response.prepare(request)

        try:
            # Run claude with json output (avoids stream-json opus timeout)
            text, usage = await _run_claude_json(model, prompt, system)

            # Opening role delta (OpenAI convention)
            role_chunk = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
            await response.write(f"data: {json.dumps(role_chunk)}\n\n".encode())

            # Emit result in chunks
            async for delta in _fake_stream_chunks(text):
                if delta:
                    await response.write(make_sse_chunk(model, delta).encode())

            # Final finish chunk
            await response.write(make_sse_chunk(model, "", finish=True).encode())
            await response.write(b"data: [DONE]\n\n")

        except Exception as exc:
            log.error("Streaming error: %s", exc)
            err_chunk = json.dumps({"error": {"message": str(exc), "type": "server_error"}})
            await response.write(f"data: {err_chunk}\n\n".encode())

        await response.write_eof()
        return response

    else:
        try:
            text, usage = await _run_claude_json(model, prompt, system)
        except Exception as exc:
            log.error("Non-stream error: %s", exc)
            return web.json_response(
                {"error": {"message": str(exc), "type": "server_error"}},
                status=500,
            )

        log.info("← %s | model=%s | %d output chars", request.remote, model, len(text))
        resp = make_completion_response(
            model=model,
            content=text,
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
        )
        return web.json_response(resp)


# ─── App factory & main ───────────────────────────────────────────────────────

def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", handle_health)
    app.router.add_get("/v1/models", handle_models)
    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    # Also handle without /v1 prefix for flexibility
    app.router.add_get("/models", handle_models)
    app.router.add_post("/chat/completions", handle_chat_completions)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Claude Code API — OpenAI-compatible wrapper")
    parser.add_argument("--port", type=int, default=PORT, help=f"Port to listen on (default: {PORT})")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    log.info("Claude Code API starting on %s:%d", args.host, args.port)
    log.info("Supported models: %s", ", ".join(sorted(VALID_MODELS)))

    app = build_app()
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
