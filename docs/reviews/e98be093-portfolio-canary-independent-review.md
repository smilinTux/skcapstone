# e98be093 independent review of Portfolio Steward canary

Verdict: FAIL

Reviewed candidate: card `096e93e2`, PR `https://github.com/smilinTux/skcapstone/pull/299`, commit `5936c87b0824c6402ad277c09ec98425563b69b2`.

This review changes no implementation, activation, credential, deployment, board completion, or external state. It adds only this review record.

## Provenance recomputation

The remote PR was open and its head was reachable as `origin/feat/096e93e2-simulation-canary` at the reviewed commit.

| Item | Recomputed value | Result |
| --- | --- | --- |
| commit | `5936c87b0824c6402ad277c09ec98425563b69b2` | matches PR and card link |
| tree | `d204d6ba27b537d39e5cdd5ffd016f59bf91da62` | matches producer verdict |
| parent | `73d5e294ab4b7e5d450375a983978b4e76e1107b` | matches manifest source commit |
| parent tree | `819f3d150f2bc83f4cfc85f518b3748813d2fb72` | matches manifest source tree |
| stable patch id | `ace5aab48a7edfd85f1067887adcbdb1c57bce6f` | candidate commit and durable patch agree |
| durable patch SHA-256 | `d7960a8cb78d7ce9408e1f8c5ddd49af4383336ae67640816dcbc0c6fa22fd97` | matches `SHA256SUMS` |
| durable bundle SHA-256 | `011b6ff6872ff2db2f1e201812d49e004ec1e4b4acb190c459dd3b99d384ab8e` | matches card link and `SHA256SUMS` |
| durable `SHA256SUMS` SHA-256 | `41ebb88e8645720f663c3576aa8e9ea92cc1c8379cfc21dae26708d51f5650ab` | recomputed |
| implementation SHA-256 | `dff6592f7dbc5bf76b783618080457d0f0c87b205d1476df478743591b5c5c19` | recomputed |
| test fixture SHA-256 | `b5d79a47a8d09b6c9245637771f57256a41557ed79c15792dbb5306eeb2394b0` | recomputed |
| runbook SHA-256 | `48e592b53606af28e710e578abd7c3c8eb5e75f4a07f4f8d40b1ca3dc6b2e9bc` | recomputed |
| configuration `pyproject.toml` SHA-256 | `280086d0e5a7185f68bfc0802eb4cfa7b1aba6a32881c312d50df1cd766991d3` | recomputed but not bound by manifest |
| producer card definition SHA-256 | `38ad970fde43c1536e9c66e7b0f2496aa76ca63123c9607e3d65c6905505918d` | recomputed current projection |
| manifest fixture hash | `1fa457417245b0a14d5c5c9b42a37cbcdc8616b74417de9fa720816eda605d16` | recomputed from test fixture |
| proposal fixture hash | `8de780e7923db90398cdefbcd4ae57b2eecca335174211edd5dfb8385666850c` | recomputed |
| allocation fixture hash | `de6398756617fc4f9f743a85ea27fdb54542aabb0c69cc118f92f8b6ea58821a` | recomputed |
| selected card id SHA-256 | `68160aa8c0635c77b8494a1383bdfaf63e7bc11043f0030c23a747e3ee369301` | recomputed from `public-synthetic-canary-096e93e2` |
| selected card revision | `5a9c502585d2d932f0baed375bbbfba761706685621df24269971984f941bab2` | recomputed from fixture expression |
| simulated result hash | `6abe135a3fcdd78be2b33e8bf72e55efb557060bc886805a4fbd021635a906ef` | deterministic recomputation |
| simulated claim receipt hash | `3fb7d1c4896ee9c81291e153189d68853f208e4b27fb36938bf517b773a5ee2b` | deterministic recomputation |
| handoff envelope hash | `6019ef6f6219fa52cd20037cb332784da56375d4e073cf1afed9a2673d4d36d0` | deterministic recomputation |

`git bundle verify` confirmed a complete history and the exact feature branch ref. Every line in the producer `SHA256SUMS` parsed and verified. The stable patch id of the durable patch equals the commit diff patch id. The producer's closed changed-path list exactly equals the three paths changed by the commit.

The component tree and evidence values in the runbook agree with the current folded evidence links for cards `d5c6f539`, `2850e05b`, `048c5de2`, `de712b36`, and `fe3877e5`. All direct dependencies of `096e93e2`, including later-added gate `c744a521`, currently fold to done.

## Blocking findings

### F1: Policy, schema, configuration, and selected-card provenance are fixture assertions, not closed bindings

The manifest uses `"a" * 64` as policy hash and `"b" * 64` as schema hash. The runbook explicitly calls these placeholder-looking public-synthetic bytes. There is no policy artifact, schema artifact, selected-card artifact, or configuration artifact in the changed paths whose bytes can be hashed to those values. The selected-card revision is only `sha256(b"public-synthetic-canary-096e93e2:r1")`; there is no serialized selected-card record whose full revision is checked. `pyproject.toml` is required to execute the imported schema models but its recomputed hash is not in the manifest.

Acceptance criterion 1 requires independently recomputing exact policy, schema, configuration, selected-card, and fixture hashes and verifying a closed path manifest. Literal test constants cannot establish provenance to the policy, schema, configuration, and selected-card bytes they purport to bind. This criterion fails.

### F2: Principal separation is declared but not enforced

`CanaryManifestV1.bounded()` checks only duplicate executors, WIP, and sorted changed paths. It does not require the reviewer to differ from the executor, producer, mutation simulator, or any proposal principal. A manifest with `reviewer_principal_id="portfolio-canary-executor"` returns `simulated` with no reason codes.

The proposal recommendation's executor principal is also not compared with `decision.target_executor_principal_id`. A recommendation whose executor is replaced by `substituted-presenter` still returns `simulated`. Therefore wrong reviewer and persona substitution do not fail closed.

### F3: Dependency revisions and several authority bindings are ignored

`decision.dependency_revision_vector` is never validated against a pinned vector. Changing it to `{"dependency":"changed"}` still returns `simulated`. The current producer card gained dependency `c744a521`, but neither the card definition revision nor its complete dependency vector is in `CanaryManifestV1`.

The front door also accepts and emits a simulated result after changing `requested_lease_seconds` from 300 to 999. Manifest `lease_seconds` is never checked. It accepts a changed proposal `policy_hash`, because only `decision.policy_hash` is compared to the manifest. It accepts recommendation changes to repository and ranking key when the decision is regenerated from the changed proposal. This does not bind deterministic allocation to a frozen candidate input.

Acceptance criterion 2 explicitly requires changed card revision, dependency changes, lease expiry and principal substitutions to fail closed or abstain deterministically. Expiry, stale snapshot, decision card revision, WIP mismatch, replay conflict, wrong decision executor, allocation denial, and multiple-card challenges do abstain. The ignored bindings above do not.

### F4: Missing receipt and writer unavailability cannot be challenged

The API takes only a proposal, decision, and clock. It has no fenced writer availability input, writer receipt input, policy receipt input, or authenticated handoff producer input. `_compose()` manufactures the writer fence, claim receipt, authorization literal, and authentication receipt locally after partial validation. Consequently there is no representable missing-receipt or writer-unavailable condition to fail closed on. Static absence of an effectful writer is useful for zero mutation, but it does not satisfy the required receipt and writer-availability challenges.

## Zero-effect and one-card assessment

The implementation imports only standard-library hashing, JSON, threading and datetime facilities plus Pydantic and read-only SKCoord contract models. AST review found no CardStore, coordination writer, provider, HTTP, socket, subprocess, filesystem, credential, protected-data, or external-action call. The candidate exposes no effectful adapter. Its counters are immutable literals with zero live claims, CardStore mutations, provider traffic, protected-data accesses, and external actions.

The selection guard requires exactly one proposal recommendation and exactly one match to the manifest card. A widened two-card proposal abstains. A substitute card cannot pass the manifest card and decision checks. Within this module, selection of a second card and live effects are not reachable.

This supports acceptance criterion 3, but does not cure F1 through F4.

## Test results

Independent focused run at the exact candidate commit:

- `PYTHONPATH=src python -m pytest -q tests/test_portfolio_canary.py`: 12 passed.
- The producer's recorded 22-test log hash verified as `7cb824eb70afd476ed3f3e60499343cf6755b1384e05d20ec527b72b7d4a9b8a`.
- A local 22-test reproduction produced 19 passed and 3 environment failures because this reviewer environment lacks `pytest-asyncio`; the three failures were existing async MCP guard tests, not candidate behavior.
- Direct adversarial probes proved six unsafe `simulated` outcomes: reviewer equals executor, changed dependency vector, changed lease duration, changed proposal policy binding, substituted recommendation executor, and changed recommendation repository and ranking inputs.

## Stop conditions and rollback

Documented rollback is non-effectful and bounded to discarding process memory, deleting generated evidence, and reverting the three candidate paths. No migration exists. The module's validated abstentions are deterministic. However, required stop conditions are incomplete because F2 through F4 remain accepted or unrepresentable.

## Final disposition

FAIL

The candidate demonstrates a genuinely effect-free, one-card in-process simulation, but it does not provide closed artifact provenance and does not fail closed for all required authority, dependency, persona, receipt, and writer-availability challenges. No implementation changes were made as part of this review.
