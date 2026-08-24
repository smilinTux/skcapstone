# SKCP-00F11 exact-source approval receipt evidence

Card: `2632ebc5`
Human attestation gate: `651f68fc`
Independent review gate: `cb8796b0`

## Result

The machine-readable receipt at
`docs/approval/SKCP-00-V1.1.3-APPROVAL-SOURCE-RECEIPT-v1.json` preserves the
displayed source message, normalization rule, normalized decision text,
extracted decision fields, explicit unknown metadata, and attribution
supersession map.

Receipt SHA-256:
`9a7e902774ea1b3dc5ac550766a2e21cd51bc31a6d04a475996c60cbc8cdad81`

Normalized decision text SHA-256:
`3427620b09ac23049ade1894ebbd52d9213439a3e112704dad37f7bd013f3cbe`

The first approval artifact remains immutable but is classified as a
nonverbatim expansion mislabeled as a quote. The append-only correction remains
immutable and is classified as an accurate transcription superseded by this
machine receipt. The R2 FAIL evidence also remains immutable pending R3.

## Verification boundary

Tests recompute the receipt, raw source, normalized text, and predecessor
hashes. Sensitivity checks prove case or punctuation changes alter the exact
source result. Message ID and message timestamp remain null because the
conversation interface did not expose them.

Human gate `651f68fc` must attest the exact receipt hash. Independent R3
`cb8796b0` must then publish PASS or FAIL before any implementation root can
advance.

## Non-authorizations

This receipt does not authorize deployment, activation, restart, external
action, protected Matter access, board reconciliation, or gate bypass.

