# SKCP-00F10 lineage correction candidate evidence

Card: `76642b9e`

## Result

The V1.1.3 candidate corrects the V1.1.2 lineage wording through an append-only
ADR amendment, manifest, and detached receipt. No predecessor file is edited.

The amendment states that both historical PNGs are present as lineage inputs,
pins their exact hashes, and keeps the V2.1 HTML wireframe as the active visual.
The manifest also pins the active contract set and F9 schedule truth repair.

## Verification boundary

Candidate tests recompute every artifact hash, the predecessor manifest and
receipt hashes, both historical PNG hashes, and the detached receipt. Tests
also prove the old contradictory wording remains immutable and the new
amendment supplies the truthful superseding statement.

Human gate `9ad1eeb8` must approve the exact V1.1.3 manifest hash. Independent
rereview `39085b32` remains blocked until that approval is merged and linked.

## Non-authorizations

This evidence does not authorize implementation, deployment, external action,
protected Matter access, board reconciliation, or completion of the mandatory
independent review.
