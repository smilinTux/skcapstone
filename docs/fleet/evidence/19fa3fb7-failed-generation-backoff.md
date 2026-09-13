# Failed Generation Backoff 19fa3fb7

## Scope

Card 19fa3fb7: an exact unchanged card/source/review generation with a
classified pre-agent rejection must not be immediately reoffered. The
generation-keyed bounded cooldown extends the existing terminal diagnostics
and bounded transport backoff merged under parent f8ba9db8. Source and tests
only: no merge, install, timer mutation, claim release, human gate, SKLegal
source work, or Lumina.

## Live evidence (review card 6dd138ad)

`~/.skcapstone/evidence/fleet-worker-exits/6dd138ad-*.json` shows five seat
worker exits on lane `codex`, every one before any agent output. The
recurring rejection is a structured pre-agent upstream template rejection:

```
400: {"code":400,"message":"Unable to generate parser for this template.
Automatic parser generation failed: ..."}
```

Every record carried `"transport_failure": null`, because the wrapper
classifier only recognized 429/5xx-led and allow-listed markers, so the
scheduler's bounded hold (`_transport_retry_held`) never fired. The CardStore
event stream for 6dd138ad shows the reoffer loop: `claim` (74244b12) ->
`review_assignment_launch` -> rejection -> `release_claim` -> immediate
`claim` (7e9a272f) -> launch -> rejection -> `claim` (258082bd) -> ..., then
the same pattern on a second host (89e4fbfe, ed5e6f19, 8bbe09fa). Each
relaunch mints a new claim revision against the same unchanged review
generation, so a claim-revision-keyed hold can never fire; the hold must be
keyed to the content generation.

## Repair

- `scripts/fleet/skfleet-worker-wrapper.py`
  - `TRANSPORT_PATTERNS` gains `upstream_template_rejection`
    (`unable to generate parser` / `automatic parser generation failed`), and
    the stdout-led first-line check in `classify_pre_agent_failure` recognizes
    the same markers. Agent output merely mentioning the phrase stays
    substantive.
  - New `card_description_generation(card_id)` returns the sha256 of the
    folded card description — the immutable candidate identity (reviewed head,
    outcome generation, candidate digest) that is stable across claim/release
    churn. `record_terminal_exit` stamps it as `card_generation` on every new
    terminal record.
- `scripts/fleet/skfleet-rotate.py`
  - `_TRANSPORT_FAILURE_CLASSES` gains `upstream_template_rejection` (the
    parity test against wrapper `TRANSPORT_PATTERNS` forces both).
  - `_latest_transport_failure_epoch` resolves the card's current content
    generation lazily (only when stamped evidence exists, via
    `_card_description_generation`) and skips stamped records whose
    generation differs: a changed candidate revision stays eligible. Unstamped
    legacy evidence, or an undeterminable generation, keeps the legacy
    card-level hold — no new fail-open path.
  - `_transport_retry_held` keeps its contract and the bounded
    `_TRANSPORT_RETRY_COOLDOWN_S` interval, so after the cooldown exactly one
    recovery probe is eligible and a failed probe starts a new bounded
    interval. No provider, model, or host is named anywhere in the change;
    exact claim fences (`--expected-claim-revision`) are untouched.
  - `_GATEWAY_ERROR_RE`/`_structured_transport_failure` recognize the strict
    one-status-plus-JSON `400` upstream template/parser rejection report as
    `upstream_template_rejection`. This closes the log-side boundary of the
    same family: without it, a pre-agent rejection printed to the worker
    stdout log counted as a substantive report and consumed the coarse
    3-attempt budget, which has no generation key and no bounded probe
    recovery (it would park the card at `attempt_limit` until a material
    change). Arbitrary or mixed `400` output stays substantive; the synced
    worker-exit JSON remains the authoritative cross-host channel.

## Verification

- Reproduction on live evidence: base-revision `_transport_retry_held`
  returns `False` for 6dd138ad (defect); the new wrapper classifies the live
  stderr as `upstream_template_rejection`; with the stamp, same generation +
  fresh rejection -> held, changed generation -> eligible, cooldown expired ->
  eligible.
- Focused tests: `tests/test_skfleet_failed_generation_backoff.py`
  10 passed; with `test_skfleet_transport_retry.py` (now also covering the
  strict `400` log-side recognition and the no-JSON substantive boundary) and
  `test_skfleet_worker_exit_evidence.py`: 48 passed.
- Full fleet/scheduler sweep: 720 passed, 1 failed
  (`test_skfleet_pi_tool_allowlist.py::test_pi_denies_a_direct_mcp_tool_and_measures_schema_bytes`),
  which fails identically on the unmodified base (environment-dependent
  allowlist, unrelated). Occasional extra failures in the combined 700-test
  run vary between invocations on baseline and branch alike (ordering
  pollution); consecutive clean runs confirm they are not tied to this change.
- Static checks: `py_compile` passed for both fleet scripts; black-clean for
  the wrapper and the new test file (rotate baseline was already unformatted;
  no new ruff errors — diff is pure line shift); `git diff --check` passed.
- No live worker, deployment, service, or runtime installation was changed.

## Rollback

Revert the candidate commit. Existing immutable exit records remain inert
audit evidence; the new `card_generation` field is optional on read, so old
and new records coexist with legacy card-level hold semantics.
