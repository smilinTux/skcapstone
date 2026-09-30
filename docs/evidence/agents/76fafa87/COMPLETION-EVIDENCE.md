# Fiber usage evidence candidate

Card: `76fafa87`. Producer: Jarvis, Codex family. Source-only candidate.
Base: `a4e3ff1a2eea744e781e31832b7c62d2ac43f684`.

## Changes

- `fiber_gateway_bridge.py` retains aggregate provider-reported prompt,
  completion, total, cached prompt and reasoning token counts when supplied.
  Each field has a response coverage count; absent usage is never reported as
  an observed zero. Streaming usage uses its final observation per response.
- Response bytes remain streamed unchanged. A transient buffer is bounded by
  the existing 16 MiB response ceiling and discarded after aggregation.
- A controller-owned `usage.json` beside the task socket records aggregate
  counters, unavailable/invalid usage counts, bounded failure classifications
  and local request-budget refusals. It does not contain prompts, response
  content, credentials or headers. Exclusive creation and mode 0600 preserve
  previous evidence. Existing receipt paths, including symlinks, refuse startup.
- Gateway route/claim validation, the 64-request budget, payload/response caps,
  sandbox permissions and worker/controller signatures remain unchanged.
- The existing bridge source and test module were formatted with Black and
  imports sorted with Ruff; that explains formatting changes outside the new
  accounting logic. No dependency was added.

## Verification

The first focused run produced three expected errors for absent accounting
features. The first full run exposed a test assumption about the interpreter
recursion limit; non-object JSON responses now explicitly count as invalid
usage rather than relying on that limit.

Final command:

```sh
BASH_ENV=/dev/null FIBER_CANDIDATE_DIR="$PWD/ops/fiber-f1be0929" \
  FIBER_FILL_NAME=fill-slots.candidate.sh \
  FIBER_PROMPT_NAME=make-prompt.candidate.sh \
  PYTHONPATH=ops/fiber-f1be0929 /home/skuser01/.skenv/bin/python \
  -m unittest discover -s ops/fiber-f1be0929/tests -p 'test_*.py'
```

Result: 172 tests in 2.548 seconds, OK, one existing skip. This is 171 passing
tests and one skipped test, not 172 executed successes.

Ruff check and Black check with line length 99 passed for both changed Python
files. `git diff --check` passed. The real synthetic HTTP/Unix-socket test
checks auth replacement, route and claim refusal, budget exhaustion, unchanged
error framing, response limits and receipt privacy. Additional tests cover
fragmented SSE byte preservation, final cumulative usage, missing/invalid
numbers, deeply nested invalid response shapes and preexisting receipt custody.

## Limits and rollout

These counters report provider claims; they do not establish billing, cost,
acceptance or provider accounting accuracy. Usage omitted by a provider stays
unavailable. Interrupted responses contribute no completed-response usage.
The fixed request budget is unchanged. No model, worker, queue or capacity
configuration changed. No live installation or independent review is claimed
by this producer evidence.

After independent exact-candidate review, rollout card `bd9a559a` may install
only this bridge file. Verify each host's preimage matches the expected
reviewed baseline; use the existing authority/install locks and preserve a
private backup and hash before atomic replacement. Existing worker processes
retain their loaded code. Verify new source imports and a real governed run's
usage receipt. Rollback restores each exact preimage without deleting receipt
history, changing claims or restarting work.
