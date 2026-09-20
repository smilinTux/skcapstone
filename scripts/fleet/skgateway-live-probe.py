#!/usr/bin/env python3
"""Validate a gateway backend by completing a request, not by counting outcomes.

WHY `/health` CANNOT BE TRUSTED FOR THIS.

The gateway's health counters update only via `recordOutcome()`, which fires
when a request *finishes*. A request that never completes therefore never
records anything, so a backend that hangs forever reports as healthy with a 0%
error rate. Measured 2026-09-19: `kimi-for-coding` reported "healthy, 0%
errors" for hours while every request to it hung past 45s, well beyond its own
configured `timeout_ms: 30000`. That hung preflight went on to stall the whole
fleet's dispatch loop, because `resolve_and_preflight()` runs per card against
a shared cycle deadline.

A health surface that cannot observe the failure it is asked about is worse
than none, because it actively argues against investigating.

THREE TRAPS THIS PROBE EXISTS TO AVOID, all of them real, all of them from one
incident on 2026-09-19/20:

1. A bare client is blocked before it reaches the model. `api.kimi.ai` sits
   behind Cloudflare, which rejects a request carrying no recognised
   `User-Agent` with HTTP 403 and body `error code: 1010`. That is a client
   signature block, and it reads exactly like an auth failure. A probe without
   a User-Agent reported a perfectly valid credential as dead, and the fleet
   lane stayed disabled on that false conclusion.

2. A reasoning model returns EMPTY content on a small budget. `kimi-for-coding`
   spends tokens reasoning before emitting anything: at `max_tokens=8` it
   returned `finish_reason=length` with empty content, which the gateway
   surfaces as `empty_upstream_response`. At 64 it returned "ok" using 30
   completion tokens. A probe that asks for a handful of tokens will declare a
   working model broken.

3. A stale in-process credential outlives the file on disk. The probe therefore
   reports what the GATEWAY returns, not what a direct upstream call returns;
   those two disagreed during this incident and only the gateway's answer
   describes what the fleet will actually experience.

Exit status is the point: 0 only when a backend genuinely produced content.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

#: Cloudflare rejects a request with no recognised agent. See trap 1.
USER_AGENT = "skgateway-live-probe/1"

#: A reasoning model needs room to reason before it emits content. See trap 2.
#: 8 was measurably too small; 64 was sufficient with 30 tokens actually used.
MIN_TOKENS = 64


def probe(base_url: str, model: str, timeout: float) -> dict:
    """Complete one real request against `model`. Never raises."""
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
            "max_tokens": MIN_TOKENS,
            # Trap 4, found by this probe on its first live run: some reasoning
            # models REFUSE a pinned temperature. kimi-for-coding answers
            # `invalid temperature: only 1 is allowed for this model` to
            # temperature=0 and returns HTTP 400. Sending none lets each model
            # use its own default, which is what a liveness probe wants: the
            # question is "does this backend answer at all", not "is it
            # deterministic".
        }
    ).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        body,
        {"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:200].decode("utf-8", "replace").strip()
        return {
            "model": model,
            "ok": False,
            "seconds": round(time.time() - started, 2),
            "reason": f"http_{exc.code}",
            "detail": detail,
        }
    except Exception as exc:  # timeouts included, deliberately
        return {
            "model": model,
            "ok": False,
            "seconds": round(time.time() - started, 2),
            "reason": type(exc).__name__,
            "detail": str(exc)[:200],
        }

    elapsed = round(time.time() - started, 2)
    if isinstance(payload.get("error"), dict):
        err = payload["error"]
        return {
            "model": model,
            "ok": False,
            "seconds": elapsed,
            "reason": err.get("code") or "upstream_error",
            "detail": str(err.get("message"))[:200],
        }

    choices = payload.get("choices") or [{}]
    message = choices[0].get("message") or {}
    content = (message.get("content") or "").strip()
    if not content:
        # Trap 2. Say which, because "empty" and "blocked" need different fixes.
        return {
            "model": model,
            "ok": False,
            "seconds": elapsed,
            "reason": "empty_content",
            "detail": (
                f"finish_reason={choices[0].get('finish_reason')} "
                f"completion_tokens={(payload.get('usage') or {}).get('completion_tokens')}; "
                "a reasoning model may need more than "
                f"{MIN_TOKENS} tokens before it emits content"
            ),
        }
    return {
        "model": model,
        "ok": True,
        "seconds": elapsed,
        "served": payload.get("served_model") or payload.get("model"),
        "content": content[:40],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default="http://localhost:18790/v1")
    ap.add_argument("--model", action="append", required=True,
                    help="repeat per model; each is probed independently")
    ap.add_argument("--timeout", type=float, default=45.0)
    args = ap.parse_args()

    failed = 0
    for model in args.model:
        row = probe(args.base_url, model, args.timeout)
        print(json.dumps(row, sort_keys=True), flush=True)
        if not row["ok"]:
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
