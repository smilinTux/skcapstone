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

Those three are the whole domain contract. In particular, HOW LONG AGO the
domain was observed is not a condition. SKGateway writes a backend health row
only from proxied request outcomes (`Backend.recordOutcome()` is the only writer
of `lastCheck`, and the gateway runs no active backend health checker), so
`lastCheck` records when that backend last carried traffic, not when the gateway
last looked at it. A perfectly healthy backend that nobody has called for an
hour carries an hour-old `lastCheck`. Snapshot age is bounded, at 120 seconds,
because admission is a same-cycle decision; observation age is not, because
bounding it would mean the fleet could only dispatch while somebody else was
already sending traffic to that exact domain. `lastCheck` is still read: a
missing, non-numeric, non-positive, or future value contradicts the `observed`
claim and fails closed as malformed.

## Bootstrap after a gateway restart

`observed: false` is the correct state for a backend on a freshly started
gateway, and it is refused, because nothing has served and nothing is known.
That is the intended fail-closed behaviour and it is not weakened here.

The consequence is that immediately after an SKGateway restart, and on a fresh
node install, NO lane is admissible until one request has succeeded on each
capacity domain the fleet wants to use. Measured on 2026-09-04 against a gateway
restarted with no traffic: every domain read `status=unknown observed=false` at
+0s, +10s and +30s, and every lane resolved to `(False, "unknown")`.

That refusal clears with the first success and does not come back. One warm-up
completion per capacity domain is the entire bootstrap:

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
`observed`, the snapshot itself is the suspect (cycle, endpoint, revision, or
age), not the backend.

Failure is lane scoped. If one model owner or capacity domain is down, ordinary
compatible work may use another healthy lane. A lane with multiple configured
capacity domains remains usable while at least one exact domain is healthy.
Missing, malformed, oversized, stale, partial, mismatched, or ambiguous evidence
does not authorize a claim. Repeated blocker records remain limited to one per
card per UTC hour.

`SKFLEET_GATEWAY_URL` is REQUIRED and has no default. The hardcoded
`http://chiap01:18790` default was removed in `e1ada0e7`, and the dispatcher
now exits with `SKFLEET_GATEWAY_URL is required` when it is unset.

**Set it to the gateway ORIGIN, with no path**: `http://chiap01:18790`, never
`http://chiap01:18790/v1`. `/health` and `/queue` are served at the gateway
root; only chat completions live under `/v1`. `gateway_root()` normalizes a
path away in BOTH places that matter, the snapshot seal and the endpoint
comparison in `lane_health()`, so the `/v1` form is tolerated end to end.
Write the origin form anyway: normalizing in one place only relabels the
outage rather than fixing it, and that mistake is invisible because the
probe then succeeds and reports no errors at all.

This mattered: on 2026-09-18 all three chi rotate hosts carried the `/v1`
form, so the probe requested `/v1/health` and `/v1/queue`, both 404ed, every
lane went `unknown`, and lane admission blocked every card. The fleet had not
launched a worker in three days (373 consecutive NOOP cycles) while the
gateway itself was healthy the whole time. Note the shape of the trap: the
chat-completions example above uses `$SKFLEET_GATEWAY_URL/v1/chat/completions`
directly beside `$SKFLEET_GATEWAY_URL/health`, which makes folding `/v1` into
the variable itself the natural mistake.

Operators may set `SKFLEET_GATEWAY_URL`, `SKFLEET_GATEWAY_SSH_USER`, or the
existing per-lane `SKFLEET_*_CAPACITY_DOMAINS` variables without changing
model mappings or lane capacity.
