# SKCP-00 V1.1.3 independent rereview

Card: `39085b32`
Verdict: `FAIL`
Reviewed revision: `975bcbdfb2f7ce2afab3a1fb4149bdad1cb5ed3c`
Candidate release: `v0.1.29`

## Scope and independence

This review challenged the released V1.1.3 lineage correction, its V1.1.2
predecessor, the F9 schedule truth repair, the exact human approval, active
contracts, schedule presentation, browser behavior, and live board gates.
The reviewer made no candidate, contract, wireframe, service, or board repair
while reaching the verdict.

## Exact inputs

- V1.1.3 manifest SHA-256:
  `9d06d085d4297cbad9a2f018daba091de011b98e3ed8eb788b59f80b8d36c4fb`
- Detached receipt SHA-256:
  `846ce6853fd386d549b7e2b4d5d7d1c1d985411be4529b6ca9a7c4fd8b42242c`
- Approval record SHA-256:
  `7e4a84c70beb394c58493acb8e5e89ccfae24423dfeaee2351c17cd1fa5efc86`
- Approval attribution correction SHA-256:
  `d34be0489b202e548ea6dfb033185a30ab3211b349c8de70701331b800d4f58d`
- F9 schedule SHA-256:
  `b1f05fd98aa1d9dc940302321efcf57b5209a8020a1cff02ab658b3e5ec0911e`
- F9 wireframe SHA-256:
  `b3636c0017f5f3289094873b0ebed03806fbaa3bbc92bc705e03e0f7c32037c9`
- V1.1.2 predecessor manifest SHA-256:
  `257db46aa26297873cd6a769e3f0eb7e6e3cf756224f99ef9a3aad61a45ff5ab`
- V1.1.2 predecessor receipt SHA-256:
  `46b98341094cf06a5f260c0ad1eed1e8d3a0090f27c2f8d570dcb84312028749`

Every manifest artifact and local JSON reference resolved at the reviewed
revision. The V1.1.3 amendment truthfully records both retained historical PNG
lineage artifacts without altering predecessor bytes.

## Blocking finding R1: contradictory human attribution

Concurrent PR #37 merged while this review was running. Its append-only
approval correction says the human owner did not type the PNG confirmation and
revision language in the first approval record. It then labels different,
shorter text as the verbatim human approval.

That correction conflicts with the exact approval presented to this review,
which includes the manifest hash, release, merge revision, explicit archived
PNG confirmation, narrow authorization, and explicit non-authorizations. The
repository now contains two incompatible claims about the human owner's exact
words. Cryptographic hashes cannot resolve contradictory attribution.

The correction must itself be superseded through an append-only, exact-source
record. No existing approval or correction byte should be rewritten or
deleted. Until that repair is independently reviewed, the exact human approval
chain is not internally consistent and this review cannot PASS.

## Truth-state challenge

The contracts and active schedule preserve failed, missing, unreachable,
unavailable, unknown, unauthorized, policy-filtered, and genuinely
not-applicable states as distinct conditions. Policy visibility and source
truth are separate fields. Denied or unknown authorization cannot become
healthy, zero, or not applicable. Negative controls in the focused tests prove
the assertions fail against the superseded mappings.

## AI and authorization boundary

AI outputs remain typed proposals. The schemas require evidence references,
best-practice references, expected impact, uncertainty, risks,
counter-indicators, alternatives, and preconditions for actionable
recommendations. Insufficient or conflicting evidence produces abstention.
Models cannot calculate canonical metrics, issue authorization, receive bearer
capability material, mutate workflow state, or execute an action. The action
route is an exact, deterministic, nonexecuting preview with separate policy
and human gates.

## Browser challenge

A fresh headless Google Chrome session was driven directly through the Chrome
DevTools Protocol against the active V2.1 file at `?evidence=legal`.

- The evidence drawer reported source truth `unknown`.
- Visibility remained `policy_filtered; authorization denied; source truth preserved`.
- Closed dialogs were hidden, inert, and `aria-hidden=true`.
- Opening the authorization preview moved focus into the dialog.
- Selecting the prototype authorization control produced the message
  `Prototype only. Exact preview was not authorized or queued.`
- Network request count remained one before and after the control interaction.
- Escape closed the dialog and restored hidden, inert, and ARIA state.
- No console exception or network failure was observed.
- The URL preserved explicit scope, service, window, baseline, and role state.

## Verification

- Focused candidate, approval, truth, contract, architecture, and UX suite:
  `60 passed`.
- Full repository suite: `320 passed`.
- Ruff: passed.
- Git diff check: passed.
- Pull request and main CI must pass before this evidence is accepted.

## Live gate and parity result

The implementation leaves inspected during review remained blocked, including
`d12b8951`, `94cbf19a`, and deployment card `2906747c`. This PASS does not
complete, claim, deploy, activate, or restart any of them.

Live CardStore parity is unsafe and remains a separate fail-closed deployment
gate:

- checked: `1077`
- matched: `672`
- mismatches: `133`
- missing: `272`
- open-count drift: `11`, above threshold `5`

No reconciliation was authorized or performed. Any deployment or live
allocation must remain blocked until the relevant parity scope is healthy and
every declared implementation dependency is complete.

## Verdict

`FAIL`. The candidate hashes, schedule truth repair, contract behavior, and
browser challenge pass, but blocking finding R1 leaves the human approval
attribution internally contradictory. Review card `39085b32` must remain in
review. This verdict does not authorize deployment, activation, restart,
external action, protected Matter access, board reconciliation, or a
safety-gate bypass.
