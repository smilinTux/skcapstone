# SKCP-00F12 approval source receipt v2 evidence

Card: `8f2a2cf3`

The v2 receipt preserves the exact extended V1.1.3 approval identified by the
human owner as authoritative. It records the exact rejection of v1, preserves
every predecessor byte, and remains proposed until exact human attestation
card `8a2331a2` is complete.

The later H4 attestation of rejected v1 is preserved at its exact hash but is
marked invalidated by the human rejection that preceded it. V2 does not erase
or silently treat that contradictory record as authority.

The receipt introduces no authority. Deployment, activation, restart,
external action, protected Matter access, board reconciliation, and gate
bypass remain unauthorized. Independent review `526bb17f` remains blocked on
the exact v2 attestation.
