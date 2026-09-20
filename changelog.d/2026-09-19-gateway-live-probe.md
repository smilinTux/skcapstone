Add `scripts/fleet/skgateway-live-probe.py`, which validates a gateway backend by
completing a real request instead of reading outcome counters.

`/health` cannot answer "is this backend alive". Its counters are written only by
`Backend.recordOutcome()`, which fires when a request FINISHES, so a backend that
hangs forever reports healthy at a 0% error rate. That is not a hypothetical: on
2026-09-19 `kimi-for-coding` read healthy for hours while every request to it hung
past 45s, and `docs/fleet/lane-admission-health.md` already records the underlying
reason ("the gateway runs no active backend health checker").

The probe encodes four failure modes that each, on their own, produce a confident
wrong answer. All four were hit during that incident:

- A bare client never reaches the model. Cloudflare fronts `api.kimi.ai` and
  answers a request with no recognised `User-Agent` with `403 error code: 1010`,
  which reads exactly like an expired credential. A valid credential was declared
  dead on this evidence and the lane stayed disabled on that conclusion.
- A reasoning model returns EMPTY content on a small budget. `kimi-for-coding` at
  `max_tokens=8` returns `finish_reason=length` with no content; at 64 it answers
  using 30 completion tokens. A probe with a tiny budget fails a working model.
- A reasoning model can REFUSE a pinned temperature. `temperature=0` returns HTTP
  400 `invalid temperature: only 1 is allowed for this model`. The probe sends no
  temperature: liveness asks whether the backend answers, not whether it is
  deterministic.
- A stale in-process credential outlives the file on disk, so the probe reports
  what the GATEWAY returns. Only that describes what the fleet will experience.

Exit status is the contract: 0 only when every named model produced real content.
