# SKCP-00 F13 preview and provenance independent rereview R5

Verdict: **PASS**

Card: `847e250a`
Reviewed release: `v0.1.42`
Reviewed main revision: `e3022dafab2f08093c3878c4ddd896ea5e7b62f6`
Reviewed tree: `12f0f5303c774148922e5e1a54fa03e72983671a`
F13 source commit: `2718066c10e8c85354120a3e4512e42eee34a97c`
F13 card: `45acd0eb`
Date: 2026-08-24

## Scope and verdict

This was a fresh independent review without repair. The two R3 blockers are
closed at the reviewed release:

1. Unknown, missing, blank, and whitespace URL preview states now select the
   explicit unavailable state, disable the authorization button, and set
   `aria-disabled=true`.
2. The append-only authority projection identifies the exact V1 receipt and H4
   attestation as authoritative and identifies V2, H5, and the dependent R4
   review as non-authoritative.

The PASS qualifies only the F13 repair and its forward dependency gate. It does
not ratify work performed before R5 was inserted, remove any historical gate,
reconcile the board, authorize deployment, or authorize external action.

## Recomputed release and provenance

The release tag `v0.1.42` resolves to the reviewed main revision and tree. PR
48 merged to that revision. Its PR checks and the main CI, docs, secret scan,
and publish workflows were all successful.

| Record | Recomputed SHA-256 | Classification |
| --- | --- | --- |
| V2.2 active visual | `f4722b9c77c8c6b1451aec7c59a4ac8c133635793e0ae4a1c558d9b09c128ce5` | active reviewed visual |
| Preserved V2.1 visual | `b3636c0017f5f3289094873b0ebed03806fbaa3bbc92bc705e03e0f7c32037c9` | immutable predecessor |
| Authority projection | `0e2fd4336f0ac58da3c0a50dcae11ecae5a233f2a35776b71aaea6d773780d5a` | active projection |
| V1 source receipt | `d6c5a0245ca42c3f32ffa73c3c0843154e66391ff40ad350ee58e3b7db91ac18` | authoritative |
| H4 attestation | `dc1a54c080e98ffa0fa817109dc5d1eab438b92b367aa7a051aed82ef24dbab8` | authoritative basis |
| V2 proposed receipt | `bf1c9d48c7721857d19f522a7aa36780f0a9fdb6cfa2c5a7bd6317c25fd213d3` | non-authoritative history |
| Unsupported H5 attestation | `84c99131840cebcc07cdea6d0020527107a92354d0ef2edf4a3d1a673da8fbe7` | non-authoritative history |
| Dependent R4 review | `16b3842c8c957c6b3ef3c392b5c289a24aede0d571bb663f17726e331a9bb459` | non-authoritative history |
| V1.1.3 candidate manifest | `9d06d085d4297cbad9a2f018daba091de011b98e3ed8eb788b59f80b8d36c4fb` | preserved candidate |
| F13 CDP qualifier | `e8a64ffea1ed055f331095ca437e6be26966444765c0ba114a19c35bbeec816b` | executable review input |

The V1 receipt's four predecessor hashes also recomputed exactly. The source
receipt and attestation preserve unknown source-message metadata, exact text,
normalization, the manifest decision, and the narrow no-deployment and
no-external-action boundary.

The authority projection contains exactly two authoritative records and three
non-authoritative records. Seven adversarial mutations were rejected: moving
each of V2, H5, or R4 into the authoritative set, marking each one
authoritative in place, and adding deployment to the allowed scope. A review,
repository record, or folded card state therefore cannot promote the
unsupported quote or its dependent review under the tested projection.

## Real Chrome CDP results

Fresh Google Chrome 151 CDP runs produced:

- Four URL boundary cases passed: unknown, missing, blank, and encoded
  whitespace.
- All six declared states preserved their intended selected value, status,
  disabled property, and `aria-disabled` value.
- The explicit on-screen ready trigger remained enabled.
- Both closed dialogs were hidden, inert, and `aria-hidden=true`.
- Eighteen Tab operations never entered a hidden dialog.
- Forward and reverse focus traps, Escape close, and trigger focus return
  passed.
- A prompt containing an HTML event handler and an instruction to ignore
  policy remained literal input. It created no DOM node and executed no code.
- Clicking the synthetic ready control produced the prototype-only notice.
- Zero non-GET requests, zero external requests, and zero runtime exceptions
  were observed.

The wireframe contains no `fetch` or `XMLHttpRequest` dispatch implementation.

## Automated verification

- Focused F13, visual truth, accessibility, and contract challenge: `53 passed`.
- Source receipt, attestation, candidate, lineage, and predecessor challenge:
  `26 passed`.
- Full suite: `350 passed`, with only existing dependency deprecation warnings.
- Ruff: `All checks passed!`
- Standalone F13 CDP qualifier: `PASS`.
- Independent keyboard, accessibility, injection, and network CDP challenge:
  `PASS`.

The sensitive repair check passes against V2.2 and deliberately fails against
preserved V2.1.

## Dependency topology and drift

Fresh CardStore readback found 29 implementation roots containing R5
`847e250a` exactly once. While R5 is incomplete, none is eligible for a new
claim through the normal dependency gate.

There is historical topology drift that this review does not hide:

- `d12b8951` was completed before R5 insertion under unsupported R4
  `526bb17f`. It now contains R5 exactly once, but a later dependency cannot
  retroactively undo its completed state.
- `94cbf19a` was already doing under unsupported R4 before R5 insertion. It now
  contains R5 exactly once, but its existing claim was not suspended by the
  later dependency amendment.

This PASS does not validate either pre-gate execution history. Their work must
be independently assessed through the normal successor gates before it is
consumed. The other 27 roots remain backlog and dependency-blocked. No
historical dependency was removed and no board reconciliation was performed.

## Boundary

No deployment, activation, restart, protected Matter access, board
reconciliation, gate bypass, or external action occurred. R5 completion may
open only the dependency state represented by the board. It does not itself
authorize implementation, deployment, or any external effect.
