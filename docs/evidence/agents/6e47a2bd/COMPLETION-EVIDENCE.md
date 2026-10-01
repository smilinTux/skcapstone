# Gateway generation policy consumer

Card: `6e47a2bd`. Producer: `codex-gateway-budget-6e47a2bd`.
Pinned source base: `3263292c87ae58bca7f203faa7c1293a4d2a4de2`.

The trusted host reads the selected model's
`card.generation_default_tokens` from the existing SKGateway `/v1/models`
catalog before model startup. The required positive JavaScript-safe integer
replaces Fiber's hardcoded 8192 request default. This is operator generation
policy, not a claim about a model's physical output capability. The gateway
half is separately implemented under card `f54adfd1` and initially declares
8192 for each eligible route, preserving current generation behavior.

## Changes and acceptance

- `ops/fiber-f1be0929/fiber_worker.py`: checks the local catalog's canonical
  gateway binding before sending the existing resolved host credential;
  fetches with no redirects or environment proxies, a 10-second network
  timeout, and a 1 MiB response bound. Missing, duplicate, malformed, or
  nonrepresentable selected policy refuses before resource setup, sandbox
  startup, workspace creation, and model execution. Errors exclude response
  bodies and credentials.
- `ops/fiber-f1be0929/fiber_sandbox.py`: requires the explicit gateway policy
  and uses it as SDK `maxTokens`, with no local 8192 clamp or fallback. Existing
  context, API, reasoning and compatibility metadata remain intact. No bridge
  request mutation was added. The SDK's intrinsic remaining-context bound
  still applies; explicit caller budgets remain under the SDK's contract.
- `ops/fiber-f1be0929/tests/test_fiber_generation_budget.py`: real loopback
  HTTP tests exercise valid and invalid policy, safe-integer representation,
  ambiguous selection, malformed/oversized responses, HTTP errors, redirect
  refusal, credential boundaries and prelaunch refusal. The actual installed
  Pi SDK builds requests and sends them to a local synthetic HTTP server.
- Existing sandbox catalog tests and `tests/qualify_gateway_sandbox.py`
  callers now supply the required gateway policy. The latter real-provider
  qualification harness was adapted but was not executed for this change.

## Verification

Before implementation, the six initial regression methods failed on the
pinned base: pytest reported 27 failing subcases/tests and 3 passing
subcases. The host test reached old runtime startup instead of refusing
missing policy; the new policy helper and required public-catalog argument
were absent. The seventh method was subsequently added for actual SDK HTTP
payload verification.

Final command:

```text
PYTHONPATH=src:ops/fiber-f1be0929 ~/.skenv/bin/python -B -m pytest -q -ra ops/fiber-f1be0929/tests/test_fiber_generation_budget.py ops/fiber-f1be0929/tests/test_fiber_worker.py ops/fiber-f1be0929/tests/test_fiber_sandbox.py ops/fiber-f1be0929/tests/test_fiber_gateway_bridge.py
```

Result: **63 passed, 0 skipped, 0 failed, 14.43 seconds, exit 0**.
The installed SDK test ran, rather than skipping for a missing SDK. Its real
HTTP payloads carried default budgets 16384 and 32768, and explicit caller
budgets 10000 and 40000, proving the policy is a default rather than a bridge
ceiling. These are synthetic fixture values, not asserted provider limits.
The existing 13 bridge tests retain claim, usage, route attribution,
backpressure, timeout and no-replay coverage. `git diff --check` passed.

External immutable test log:
`~/.skcapstone/evidence/work/6e47a2bd/boundary-tests.log`.
SHA-256: `793e65c80d8036f8aa8ba449579cc8f247efd33af90a3f0dbeaf30fe2ddd197e`.
The baseline failure log is retained alongside it as `before-tests.log`.

## Limits and installation gate

No upstream model completion, gateway configuration change, runtime
installation, push, or merge occurred. This candidate requires independent
exact-version review and qualification together with gateway card `f54adfd1`.
Installing the consumer without valid gateway metadata intentionally refuses
new model work. Codex's existing gateway adapter still discards output-budget
parameters; this change does not claim upstream Codex enforcement.

Root owns composition with the separate `8abc` prompt change, maintenance
HOLD, zero-active-worker checks, exact installed preimages, backups and
rollback. Data migration is unnecessary. No deployed rollback was needed.
The external candidate descriptor binds the final commit, tree, file hashes
and this evidence after commit; this document does not name its own hash or
self-containing final commit. The native card remains claimed pending review
and installation acceptance.
