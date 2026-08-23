# SKCP-00 measurement architecture approval

Date: 2026-08-23
Recorded at: 2026-08-23T19:59:31Z
Approver: Human owner
Architecture card: `9442b3b3`
Human gate: `9508b8fd`
Epic: `6f7fd828`

## Approved candidate

The human owner explicitly stated:

> I approve SKCP-00 candidate manifest sha256:88b78aece092291535177414f159925ce997083c7c51134ed184495c8b9318d3 and the six-sprint breadth-first plan.

The recorded candidate is:

- Path: `docs/review/SKCP-00-CANDIDATE-MANIFEST.json`
- SHA-256: `88b78aece092291535177414f159925ce997083c7c51134ed184495c8b9318d3`
- Manifest version: `1.0.0`
- Candidate status at review: `proposed_for_human_review`
- Artifact count: 11

The hash was recomputed from the local SKDashboard repository immediately
before this approval record was created and matched exactly.

## Approval scope

The approval accepts the following candidate decisions:

1. SKDashboard is the canonical human and approved-agent projection plane, not
   the authoritative owner of project work, ITIL, configuration, model usage,
   policy, service state, or protected Matter content.
2. The canonical local process and origin remain `skcapstone dashboard --port
   7778` and `http://127.0.0.1:7778`, with one future approved tailnet-only
   production origin.
3. New versioned resources use `/api/v1` with explicit ownership, errors,
   freshness, pagination, ETag, streaming, compatibility, and deprecation.
4. Metrics use the approved envelope, truth states, measurement kinds, scopes,
   watermarks, deterministic calculations, classifications, and data-quality
   evidence.
5. Reports are immutable, hash-addressed snapshots with reproducible metric
   definitions, source watermarks, quality statements, model provenance, and
   human review state.
6. AI may observe, explain, compare, conclude, recommend, simulate, prepare, and
   learn from verified outcomes. It does not own canonical math, policy,
   workflow state, Approval, or execution.
7. Recommendations identify evidence, best-practice version, expected impact,
   confidence, uncertainty, counter-indicators, alternatives, risk,
   preconditions, and reversibility.
8. The low-click action path uses one click to open a deterministic
   authorization preview and a separate explicit click to approve and queue the
   exact eligible hash.
9. The six-sprint breadth-first sequence, planning containers, and 22 leaf-card
   catalog are accepted.
10. Active SKCP-04 `8b0ad975` and active SKCP-10 `9936350d` are confirmed as the
    canonical client and qualification cards. Archived duplicate `5ae27468` and
    obsolete qualification card `f3672bc4` remain archived and auditable.

## Workflow effect

This approval authorizes:

- Completion of human gate `9508b8fd`.
- Completion of the accepted SKCP-00 architecture candidate `9442b3b3` after
  its recorded tests and evidence are verified.
- Confirmation of canonical-card gate `d79100a7` after SKCP-00 completes.
- Eligibility of independent review `d0edbff1` after SKCP-00 completes.
- Subsequent implementation only through exact eligible, dependency-complete,
  explicitly claimed leaf cards after the mandatory independent review passes.

## Explicit non-authorization

This approval does not authorize:

- Production deployment or tailnet ingress
- External account creation or connector activation
- Protected SKLegal Matter retrieval or model egress
- HammerTime Inbox search, read, move, or processing
- Additional corpus or Matter migration
- Email, filing, service, mailing, calendar, client communication, or other
  external action
- Generic shell, filesystem, browser, network, or connector authority for a
  model
- Skipping CapAuth, owner policy, exact-version Approval, idempotency,
  verification, receipt, rollback, or audit gates
- Claiming the epic or planning-only sprint containers

## Required next gate

Independent review card `d0edbff1` must recompute the candidate hashes and
challenge ownership, security, metric integrity, report reproducibility, AI
grounding, prompt-injection resistance, authorization separation, accessibility,
and sprint dependencies without repairing the candidate.

Sprint 1 implementation cards remain blocked until both the human gate and the
independent review are complete.
