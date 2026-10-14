# Pending acceptance recovery

Five sealed pairs fail refresh solely because the link uses review_evidence_sha256 instead of reviewer_evidence_sha256. Validate the supported evidence aliases consistently, rejecting disagreements. Keep all other identity, candidate, applicability and native-outcome checks.

Thirteen released reviews have no native verdict, and three released reviews reference superseded source outcomes. Do not accept these or recreate reviewer identities. Expose an operator-attributed coord retirement using the existing supersede_generation archive and receipt format. Require exact live card, request and status hashes, an exact released reviewer claim, no acceptance transaction, no live owner, and either no native outcome or a changed source generation. Reprove the exact terminal invocation even if node status still says running. Keep normal supersession callers unchanged. Preserve all packets and history; clear only the exact stale offer/status after receipt and event readback.

Validate with focused regression tests plus existing acceptance and retirement suites. No deployment or merge.
