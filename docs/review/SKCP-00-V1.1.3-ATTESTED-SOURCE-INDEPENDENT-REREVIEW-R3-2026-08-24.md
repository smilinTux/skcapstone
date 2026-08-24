# SKCP-00 V1.1.3 attested-source independent rereview R3

Card: `cb8796b0`
Verdict: `FAIL`
Reviewed revision: `4acb11829a3d1a578e629fb4dee23e171ea15acb`
Candidate release: `v0.1.29`
Authoritative source receipt release: `v0.1.34`
Human attestation release: `v0.1.36`

## Scope and independence

This review independently challenged the exact V1.1.3 source receipt, H4
attestation, candidate, F9 schedule repair, active contracts, browser
presentation, AI boundary, action preview, and live dependency topology. The
reviewer made no candidate, contract, source receipt, attestation, wireframe,
test, service, or board repair while reaching the verdict.

The stale R2 and earlier R3 FAIL artifacts remain historical evidence. They
were not rewritten or treated as current approval sources.

## Exact reviewed inputs

- V1 authoritative source receipt SHA-256:
  `d6c5a0245ca42c3f32ffa73c3c0843154e66391ff40ad350ee58e3b7db91ac18`
- Raw approval source SHA-256:
  `8756eeeb8075de8ac020c757f1c596739fcd6b4e5b221a7dd10b564044ddaa3e`
- Normalized approval text SHA-256:
  `3427620b09ac23049ade1894ebbd52d9213439a3e112704dad37f7bd013f3cbe`
- H4 attestation SHA-256:
  `dc1a54c080e98ffa0fa817109dc5d1eab438b92b367aa7a051aed82ef24dbab8`
- V1.1.3 manifest SHA-256:
  `9d06d085d4297cbad9a2f018daba091de011b98e3ed8eb788b59f80b8d36c4fb`
- V1.1.3 detached receipt SHA-256:
  `846ce6853fd386d549b7e2b4d5d7d1c1d985411be4529b6ca9a7c4fd8b42242c`
- F9 schedule SHA-256:
  `b1f05fd98aa1d9dc940302321efcf57b5209a8020a1cff02ab658b3e5ec0911e`
- F9 active wireframe SHA-256:
  `b3636c0017f5f3289094873b0ebed03806fbaa3bbc92bc705e03e0f7c32037c9`
- Concurrent proposed V2 receipt SHA-256:
  `bf1c9d48c7721857d19f522a7aa36780f0a9fdb6cfa2c5a7bd6317c25fd213d3`
- H5 V2 attestation document SHA-256:
  `84c99131840cebcc07cdea6d0020527107a92354d0ef2edf4a3d1a673da8fbe7`
- R4 V2 review document SHA-256:
  `16b3842c8c957c6b3ef3c392b5c289a24aede0d571bb663f17726e331a9bb459`

All 20 V1.1.3 candidate, predecessor, artifact, and lineage hash checks
matched. All 132 local JSON references resolved. The active compatibility,
metric, recommendation, action-preview, insight, report-snapshot, and OpenAPI
contract hashes matched the V1.1.3 manifest.

## Source authority result

V1 and H4 are internally consistent. The exact human H4 attestation names V1
receipt SHA-256 `d6c5a024...`, states that the initial expanded quote was not
verbatim, and authorizes only review `cb8796b0`. The earlier REJECT event is
preserved as history. The later exact H4 attestation and current folded
`decision=ATTEST` supersede it without deleting any event.

The V2 receipt, H5 attestation, and R4 PASS merged concurrently are not
authority. The H5 document quotes a supposed human statement that does not
appear in the authoritative conversation. That statement says V2 contains the
human's exact extended approval text and V1 remains rejected. The actual later
human statement says the initial expanded quote was not verbatim, attests V1
receipt SHA-256 `d6c5a024...`, and limits authority to R3 `cb8796b0`. The user
did not authorize H5 `8a2331a2` or R4 `526bb17f`.

V2 labels the later exact H4 attestation
`invalidated_by_prior_human_rejection`. Its evidence document repeats that
claim, the changelog calls V2 corrected, and H5 now falsely presents that V2
text as exactly human-attested. A prior decision cannot invalidate a later
explicit attestation. These merged statements contradict the exact H4 source
order and are unsafe current-main provenance. This review does not treat V2 or
H5 as authority. R4 repeats the unsupported attribution as a premise, so its
PASS cannot repair or supersede the missing human source authority.

## Blocking finding F1: unknown preview state becomes ready

The active V2.1 wireframe fails closed-state handling for an unrecognized URL
preview state.

Exact counterexample:

```text
?preview=1&state=unsupported-state
```

Fresh Chrome CDP execution produced:

- selected preview state: `ready`;
- status: `Ready for human authorization`;
- reason: `Authorization is available for the exact synthetic preview after revalidation.`;
- authorization button `disabled=false`; and
- authorization button `aria-disabled=false`.

The cause is in
`docs/wireframes/control-plane-estate-pulse-v2.1.html`: `setPreviewState`
maps every unknown state to `previewStates.ready`, and the URL initialization
passes the untrusted query value directly to that function. Unknown input must
remain unknown, unavailable, denied, or disabled. It cannot become ready.

The prototype still does not dispatch an action, but a false ready state is an
authorization-boundary defect and prevents R3 from passing.

## Blocking finding F2: unsupported H5 attribution reverses H4

Current main contains an H5 attestation document whose quoted human statement
is absent from the authoritative conversation. It asserts that V2 contains the
human's exact extended approval text, rejects V1, and authorizes H5 and R4.
Those claims directly conflict with the later exact human H4 attestation of V1
and its R3-only authority boundary.

The unsupported statement was folded into H5 `8a2331a2` as `decision=ATTEST`
and H5 was completed. R4 `526bb17f` then published PASS and completed by
treating that unsupported statement as exact human attestation. This is a
concrete provenance and authorization-boundary failure. Repository, test,
review, or board state cannot create human authority that the source
conversation did not grant.

## Browser controls that passed

The same fresh Chrome 151 CDP session confirmed:

- both closed dialogs were hidden, inert, and `aria-hidden=true`;
- 18 top-level Tab operations never entered a hidden dialog;
- forward and reverse focus trapping, Escape close, and focus return worked;
- Legal source truth stayed `unknown` while visibility stayed
  `policy_filtered; authorization denied; source truth preserved`;
- declared `stale-target`, `denied-policy`, `expired`, and
  `changed-parameters` states disabled authorization;
- prompt-injection text remained literal input and created no executable node;
- a ready prototype click displayed that it was not authorized or queued;
- five browser requests were local GET requests, with zero non-GET and zero
  external requests;
- no Runtime exception occurred;
- no console error occurred other than the permitted local favicon 404; and
- Ask AI contrast was `6.839648875:1`, with foreground `rgb(6, 17, 30)` and
  background `rgb(154, 140, 255)`.

## Contract and adversarial result

An independent 80-case contract matrix accepted 14 valid controls and rejected
all 66 attempted governance bypasses. The rejected cases covered false healthy
or zero values, truth and visibility conflation, current results with errors,
missing evidence and watermarks, ungrounded AI proposals, missing model
provenance, capability or command material, actionable abstentions, missing
best-practice and impact grounding, and ready actions without exact targets,
versions, policies, or current exact approvals.

## Live topology

- F11 `2632ebc5`: `done`.
- H4 `651f68fc`: `done`, folded current decision `ATTEST`.
- R3 `cb8796b0`: in progress during review.
- H5 `8a2331a2`: `done` with the unsupported V2 attribution.
- R4 `526bb17f`: `done` with PASS based on the unsupported H5 attribution.
- All 29 implementation roots: `backlog`.
- All 29 roots contain `cb8796b0` exactly once.
- All 29 roots retain historical review gate `39085b32` exactly once.
- Zero implementation roots were eligible.

The concurrent V2 implementation, H5, and R4 cards are done. Neither H5 nor R4
has source authority, and no implementation root may treat their state as
authorization.

## Verification

- Focused source, attestation, candidate, F9, contract, architecture, and UX
  suite: `76 passed`.
- Full repository suite: `336 passed`.
- Ruff check: passed.
- Ruff format check: failed on 25 pre-existing files, including the proposed
  V2 and unsupported H5 tests. They remain unmodified.
- Git diff scope and prohibited-path checks: passed before publication.

## Verdict and authority boundary

`FAIL`. The exact V1 source receipt and H4 attestation pass, and the contract,
truth, no-dispatch, accessibility, contrast, and dependency controls otherwise
hold. The unknown preview-state counterexample presents authorization as ready.
Current main also contains an unsupported H5 human attribution that treats V2
as attested, rejects the authoritative V1 source, and reverses the exact H4
event order. R4 compounds that failure by publishing PASS from the unsupported
H5 premise.

This FAIL authorizes only publication and linkage of this review result. It
does not authorize a repair, implementation, deployment, activation, restart,
external action, protected Matter access, board reconciliation, completion of
`cb8796b0`, or any gate bypass.
