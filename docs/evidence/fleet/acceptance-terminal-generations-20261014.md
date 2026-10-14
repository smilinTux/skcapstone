# Terminal acceptance follow-up

The 16 operator retirements authorized after PR #1211 were applied through coord as lumina-nor, one at a time with fresh hashes and native readback. Each recorded one supersession event and a valid immutable receipt, removed only the old request/status pointers, and left the source unchanged. The next rotation counted 96 historical generations versus 80 before; none of the 16 appeared as pending.

Remaining cases:

| Review | Cause | Resolution |
|---|---|---|
| 0a10fcae | Source a69ec3a8 was explicitly superseded after subsequent producer claims. Old reviewer still held its claim. | Verified stopped worker, released exact review claim as lumina-nor with the governed finished-work override. Existing sweep recorded REVIEW_GENERATION_SUPERSEDED. |
| 0e8fb518 | Negative review uses review_evidence_sha256 rather than reviewer_evidence_sha256. | Covered by #1211; preserve FAIL and allow ordinary negative disposition. |
| 19b5b8a6 | Unchanged source retains a pre-claim content link. | Covered by #1211; live corrected custody proof is RELEASED_CLEAN. |
| c2c83162 | Old FAIL belongs to a source whose later producer generation is BLOCKED. Retained imported-review directory prevents retirement despite no context or journal. | This follow-up permits imported artifacts only and preserves them. Context, journal, disposition and unknown root files still refuse. Earlier attempted apply refused before any write. |
| 89398501 | Source 38119435 was completed by lumina-nor after the acceptance journal stopped at step 08. Review is also done. | This follow-up classifies completed-outside-acceptance with accepted=false and no writes. It does not fabricate finished.json or bypass the finish journal. |

The external-completion classifier requires the sealed context and finish-intent hash to agree, the same source/review relationship, both native cards done and unowned, and native completion events. Reopened, mismatched, archived or incomplete pairs do not qualify. A valid existing finished.json retains its stricter accepted replay checks.

Operator records and readbacks are retained in ~/reviews/acceptance-pending-20261014/retirement-apply/ and ~/reviews/acceptance-remaining-20261014/. Any later apply must recompute its expected hashes from live state.
