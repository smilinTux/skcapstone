# Fleet lane admission health

`scripts/fleet/skfleet-rotate.py` creates one health snapshot before selecting
work. It fetches the configured SKGateway `/health` and `/queue` endpoints once,
resolves the exact Git revision of the process serving that endpoint port, and
atomically replaces `~/.skcapstone/evidence/fleet-lane-health.json`. No separate
publisher, timer, service, installation, or deployment is required.

Each endpoint response and the sealed snapshot are capped at 65,536 bytes. The
snapshot expires after 120 seconds. Its version 2 contract includes:

- selector cycle ID and observation time
- normalized SKGateway endpoint
- exact active gateway Git revision
- fleet lane and requested model
- configured capacity domains
- per-domain health, quarantine, owner availability, and queue capacity
- bounded acquisition errors

Admission uses only the in-memory snapshot returned by that cycle's atomic
write. It requires exact cycle, endpoint, runtime revision, lane, model, and
capacity-domain matches. A domain is usable only when SKGateway observed it as
`up` or `degraded`, did not quarantine it, and reports positive queue capacity.
The backend observation in `lastCheck` must also be within 120 seconds of the
snapshot observation time. Missing, non-numeric, non-positive, older, or more
than 120 seconds future-dated values fail closed as `unknown`. A fresh snapshot
does not make stale backend evidence current. This deliberately requires an
idle backend to refresh its serving evidence before the scheduler admits new
work to a shared logical bucket.

## Bootstrap after a gateway restart

`observed: false` is the correct state for a backend on a freshly started
gateway, and it is refused, because nothing has served and nothing is known.
That is the intended fail-closed behaviour and it is not weakened here.

The consequence is that immediately after an SKGateway restart, on a fresh node
install, or after 120 seconds without a current serving observation, no lane is
admissible until one request has succeeded on each capacity domain the fleet
wants to use.

That refusal clears with a current success. A warm-up completion per capacity
domain refreshes admission evidence:

```
curl -s -X POST "$SKFLEET_GATEWAY_URL/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"<a model served by that domain>",
       "messages":[{"role":"user","content":"ok"}],"max_tokens":1}'
```

Confirm with `curl -s "$SKFLEET_GATEWAY_URL/health"`: the domain should read
`observed: true` with a non-down `status`. Repeat per capacity domain, since the
health row is per backend and a success on one domain says nothing about
another.

If a lane is refused with `unknown` while `/health` shows the domain `up` and
`observed`, check both snapshot age and the backend `lastCheck` age.

Failure is lane scoped. If one model owner or capacity domain is down, ordinary
compatible work may use another healthy lane. A lane with multiple configured
capacity domains remains usable while at least one exact domain is healthy.
Missing, malformed, oversized, stale, partial, mismatched, or ambiguous evidence
does not authorize a claim. Repeated blocker records remain limited to one per
card per UTC hour.

Logical buckets remain provider-neutral. The scheduler may bind `sk-s`, `sk-m`,
`sk-l`, or `sk-xl` work to any qualified compatible backend with current
capacity evidence. Qwen remains a separate sovereign lane and its controls are
not relaxed by shared-bucket admission.

The endpoint defaults to `http://chiap01:18790`. Operators may set
`SKFLEET_GATEWAY_URL`, `SKFLEET_GATEWAY_SSH_USER`, or the existing per-lane
`SKFLEET_*_CAPACITY_DOMAINS` variables without changing model mappings or lane
capacity.
