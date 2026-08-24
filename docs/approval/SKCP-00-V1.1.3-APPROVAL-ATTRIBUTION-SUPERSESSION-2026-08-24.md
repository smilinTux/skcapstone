# SKCP-00 V1.1.3 approval attribution supersession

Date: 2026-08-24
Repair card: `af31a281`
Human gate: `9ad1eeb8`
Independent review: `026d71a5`

## Purpose

This append-only record resolves the attribution conflict identified by R2
without editing or deleting either predecessor record.

The first approval record contains the exact human approval supplied for the
current gate:

> I APPROVE SKCP-00 V1.1.3 manifest SHA256 9d06d085d4297cbad9a2f018daba091de011b98e3ed8eb788b59f80b8d36c4fb at release v0.1.29 and merge revision e39b1b4cf2d546ea2c309174cce30b69eb43373c. I confirm the archived PNG lineage narrative matches the retained bytes and hashes. This authorizes only completion of 9ad1eeb8 and independent review 39085b32. It does not authorize deployment, activation, external action, protected Matter access, board reconciliation, or any safety-gate bypass.

PR #37 later asserted that the human owner did not supply the PNG confirmation
and revision language, and labeled different text as verbatim. That absolute
attribution claim conflicts with the exact approval above and is superseded by
this record. The PR #37 file remains preserved as historical evidence of the
conflict, not as the authoritative transcription of the current gate approval.

## Immutable predecessor hashes

- Exact approval record SHA-256:
  `7e4a84c70beb394c58493acb8e5e89ccfae24423dfeaee2351c17cd1fa5efc86`
- Conflicting PR #37 correction SHA-256:
  `d34be0489b202e548ea6dfb033185a30ab3211b349c8de70701331b800d4f58d`
- R2 FAIL evidence SHA-256:
  `4eec9f3211779a24e1299c42b3f762a395c578e39b99f466af0574941b96a42e`

## Authority boundary

This record corrects attribution only. It introduces no new approval or
authority. It does not authorize deployment, activation, restart, external
action, protected Matter access, board reconciliation, or any safety-gate
bypass. Independent review `026d71a5` must publish PASS or FAIL before the
control-plane implementation chain can advance.
