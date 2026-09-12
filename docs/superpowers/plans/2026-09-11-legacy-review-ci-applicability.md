# Legacy Review CI Applicability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: execute inline with test-first changes. No delegation is required.

**Goal:** Let governed legacy review cards use repository-specific hosted checks without inventing generic check success.

**Architecture:** Keep immutable `ci_profile` validation unchanged. For a review card without that metadata, select a checked-in repository policy only when immutable repository, candidate head, and candidate evidence metadata are complete, then validate one latest append-only whole receipt against those pins and exact hosted check names. Unknown repositories retain the existing six-check fallback.

**Tech Stack:** Python 3.11+, standard-library JSON and URL parsing, Pydantic/CardStore event data, pytest.

**Spec:** SKCapstone card `10a1018b` and target legacy review card `78c57267`.

## Global Constraints

- Base revision is `723e6a989b2c5deafcee26f2514739f9dfd7c2e0`.
- Do not change SKGateway PR155 bytes.
- Fail closed on repository, head, evidence digest, receipt schema, check set, state, or evidence URL mismatch.
- Preserve the immutable `ci_profile` branch byte-for-byte except for shared helper reuse required by tests.
- No merge, deploy, installation, human gate, Lumina, or claim release owned by another identity.

---

### Task 1: Add the legacy policy and receipt validator

**Files:**

- Modify: `src/skcapstone/ci_profile_registry.py`
- Modify: `src/skcapstone/ci_applicability.py`
- Modify: `src/skcapstone/review_verdict.py`
- Test: `tests/test_legacy_ci_applicability.py`

**Interfaces:**

- Consumes immutable `CardCore.meta` fields `repository`, `link_head_revision`, and `candidate_evidence_sha256`.
- Produces `validate_legacy_completion(card_id: str, home: Path, core: dict) -> bool`, returning false only when no governed legacy repository policy applies and raising `ValueError` for every malformed or mismatched governed receipt.

- [ ] Write passing-shape and negative regression tests for the exact SKGateway policy and PR155 receipts.
- [ ] Run the focused test and confirm the missing interface fails.
- [ ] Add the immutable repository policy and strict latest-whole-receipt validator.
- [ ] Route profile-less review completion through the validator before the unchanged six-check fallback.
- [ ] Run focused profile, verdict, completion parity, and CardStore tests.

### Task 2: Publish for independent review

**Files:**

- Modify: `CHANGELOG.md`
- Create: `docs/superpowers/plans/2026-09-11-legacy-review-ci-applicability.md`

**Interfaces:**

- Consumes the tested source candidate and CardStore claim generation.
- Produces one pushed branch, one pull request, hashed evidence, and one distinct review card.

- [ ] Run formatting, lint, docs, secret, and full relevant test gates.
- [ ] Commit the exact scoped files and compute commit, tree, diff, and evidence hashes.
- [ ] Push the branch and open a PR without merging it.
- [ ] Link the source card evidence, move it to review, create a distinct governed review card, and notify the requested SKMail recipients.

Rollback: revert the scoped commits before any runtime installation. Existing card events and review evidence remain append-only.
