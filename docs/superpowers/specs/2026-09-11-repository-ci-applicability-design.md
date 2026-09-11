# Repository CI applicability design

Card: `15dbb9fd`. Source base: `46914a4502541491052f782a0416b0b5b85a8abd`.
Scope: design and plan only; no runtime installation, live policy change, or source implementation.

## Decision and alternatives

Use a checked-in `.skcapstone/ci-profile.json`, copied into immutable card birth metadata after verification against exact Git objects. This makes repository policy reviewable and permits offline completion without trusting a mutable checkout. Per-card check lists would permit reviewers to weaken their own gates. Automatic language detection would confuse absent, skipped, and genuinely inapplicable checks. Both are rejected.

At this base, `review_verdict.py` requires canonical `PASS` plus six exact `SUCCESS` links. `coord_completion.py` invokes it for CLI/MCP complete and move-to-done. Despite an outdated module comment, tests and executable code reject `FAIL` and `BLOCKED` for terminal completion. Preserve that behavior. `CardCore.meta` already holds immutable source bindings; folded links and amended descriptions are not policy authority.

## Version 1 contract

The manifest is UTF-8 JSON, at most 64 KiB, with exactly `schema_version` (integer `1`), `repository` (credential-free HTTPS URL), and `checks` (nonempty object). Reject duplicate JSON keys, unknown fields, unsupported versions, and empty strings. Repository identity uses exact URL equality after removing one terminal `.git` and trailing slash; do not case-fold paths.

Each check key matches `ci_check_[a-z0-9_]+`; every legacy key below must appear. Additional Node or repository-specific keys are allowed. Each check has exactly `expected` and `reason`: `expected` is `SUCCESS` or `NOT_APPLICABLE`; `reason` is empty for `SUCCESS` and a nonblank, concrete repository-level explanation for `NOT_APPLICABLE`. At least one check must require `SUCCESS`. These are policy declarations, not observed CI conclusions.

Legacy keys: `ci_check_docs`, `ci_check_gitleaks`, `ci_check_lint`, `ci_check_shim_imports`, `ci_check_python311`, `ci_check_python312`.

For example, a Node repository declares Python and shim checks `NOT_APPLICABLE` with reasons and adds `ci_check_node_test: {"expected":"SUCCESS","reason":""}`. A Python repository keeps its Python checks required. A mixed repository requires both language suites. Applicability is repository-wide; changed-file heuristics and per-card overrides are out of scope.

The new creation input `ci_profile` has exactly `candidate_revision` (40 lowercase hex), `profile_sha256` (64 lowercase hex), and `repository_path` (absolute existing local Git repository). CLI accepts this JSON through `--ci-profile`; MCP accepts the same object. Existing `repository`, `base_ref`, and `base_revision` source binding fields are mandatory when this input is present. CLI review `head_revision`, when supplied, must equal `candidate_revision`.

The shared creation validator performs bounded, read-only Git calls with argument arrays, no shell, a 10-second timeout, and no network:

1. Require exactly one `remote.origin.url` and match it to the source binding repository. The requested path must resolve to Git's repository top-level directory; reject subdirectory or ambiguous repository selection. Require base and candidate to resolve as exact commit objects and require base to be an ancestor of candidate.
2. Read the fixed manifest path using `git cat-file` from both commits. Require normal blob mode `100644` or `100755`, not a symlink/submodule, and enforce the size bound before reading content.
3. Require byte-identical base/candidate manifests, the supplied SHA-256 over those exact bytes, and matching manifest repository. Missing or changed policy fails creation. A policy update must first land through the existing legacy completion route and then serve as a new approved source base.
4. Store `core.meta.ci_profile` with exactly `schema_version: 1`, `repository`, `base_revision`, `candidate_revision`, `profile_sha256`, and `manifest_text` (the exact decoded bytes). Never persist `repository_path`. Existing authorization mediates card creation; this input grants no new write authority.

The stored capsule deliberately repeats source identity so completion can cross-check `core.meta.repository`, `core.meta.base_revision`, and any `core.meta.link_head_revision`. Completion recomputes the digest, validates the full schema, and uses birth metadata only. The core store's existing immutability is the trust boundary; defending against an operator rewriting both core and hashes is outside this change.

## Evidence and completion

Profile cards record one JSON string under link key `ci_applicability`. Its exact fields are `schema_version: 1`, `repository`, `candidate_revision`, `profile_sha256`, and `checks`. The three identity values must match the immutable capsule. `checks` must have exactly the manifest's keys. Each result has exactly `state`, `reason`, and `evidence`: `evidence` is a nonblank immutable CI run/job URL or content-digested local artifact reference.

For a required check, only exact `state: "SUCCESS"` with empty reason passes. For an inapplicable check, only exact `state: "NOT_APPLICABLE"`, the manifest's exact reason, and `evidence: "sha256:<profile_sha256>"` with the capsule's literal digest passes. Missing, skipped, pending, failed, cancelled, unknown, case variants, and success-like strings fail. A legacy string `SUCCESS` cannot substitute for the JSON receipt. `NOT_APPLICABLE` is a positive policy/evidence match, never an alias for `SKIPPED`.

Use the latest receipt by parsed timezone-aware timestamp, not per-check merging. Identical duplicate rows are harmless; conflicting receipts with the same latest timestamp block. Malformed matching receipt rows, missing timestamps, or unreadable evidence files block profile completion. A newer invalid or differently pinned receipt blocks rather than reviving an older green receipt. CI evidence remains attributed board evidence under existing writer controls; this task does not add remote CI authentication or provider polling.

`validate_review_completion` keeps its signature and canonical verdict handling. For a governed review with `PASS`, load core metadata once: absent `ci_profile` selects the unchanged legacy six-link gate; present but null, malformed, tampered, incomplete, or mismatched profile fails closed without legacy fallback. Missing/malformed core in this governed path also fails closed. Non-review completion remains unchanged. Mutable card links, tags, descriptions, or a proposed shorter check list cannot select or alter policy. All four completion entrypoints continue through `coord_completion.py` and must fail before mutation.

## Delivery and acceptance

One source implementation card and one independently reviewable commit suffice. Add one helper module; modify the existing verdict and CLI/MCP creation adapters; extend the existing verdict and parity tests plus one focused profile test module. No CardCore schema migration, workflow edits, repository enrollment, runtime installation, or new service is required.

Acceptance covers Node-only, Python-only, mixed, absent profile legacy behavior, partial legacy evidence, missing/malformed manifests, duplicate fields, unsupported versions, wrong repository/commit/hash, symlink manifests, changed base policy, empty/all-inapplicable check sets, conflicting receipts, stale pins, and attempted card-level weakening. Exercise successful and rejected profile completion through CLI complete, CLI move, MCP complete, and MCP move; rejection must preserve the claim and lifecycle state.

Rollback is source reversion before deployment. Existing cards are never rewritten. A future runtime rollout must either retain profile support or leave profile cards open pending restoration; an old runtime is not qualified to complete profile cards. This design does not authorize that rollout.
