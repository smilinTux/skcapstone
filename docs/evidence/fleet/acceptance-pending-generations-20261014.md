# Pending acceptance diagnosis, 2026-10-14

The five prior reseals (7907fdaf, 8f5077b2, a4ed7c20, add19bf8, bb6da93e) remain accepted by fresh installed-runtime readback. Both cards in each pair remain done. No repeat reseal was performed.

The 19:02:29 UTC chiap08 log contains 13 missing-current-review outcomes, five native-pair refusals and three unavailable-producer outcomes. This is 21 cards, rather than the earlier count of 22.

No board writes or deployment were performed. The fix PR supplies the code and the exact operator recovery path. All 16 unusable generations passed live check-only retirement under lumina-nor; hashes were computed from live state. Refresh all preimages before any apply.

| Review | Source | Diagnosis | Proposed resolution |
|---|---|---|---|
| 056a4633 | 7f1e645e | Old review source was explicitly superseded or has a newer typed candidate | Exact operator retirement; retain old evidence and use fresh source review |
| 10111783 | b1f004f2 | Released reviewer has no native verdict; zero process exit does not establish a review | Exact operator retirement; fresh independent review if source remains current |
| 1751be81 | e75915f2 | Released reviewer has no native verdict; zero process exit does not establish a review | Exact operator retirement; fresh independent review if source remains current |
| 2a94914b | e1c0d1fb | Released reviewer has no native verdict; zero process exit does not establish a review | Exact operator retirement; fresh independent review if source remains current |
| 400743c5 | bf80259a | Old review source was explicitly superseded or has a newer typed candidate | Exact operator retirement; retain old evidence and use fresh source review |
| 4398f68c | 2d845a5f | Supported review_evidence_sha256 link rejected by refresh check; pre-claim legacy commit/tree links obscure the typed candidate | Fix evidence-key validation and historical binding proof; resume ordinary acceptance and trusted tests |
| 44a54f90 | d11d2601 | Released reviewer has no native verdict; zero process exit does not establish a review | Exact operator retirement; fresh independent review if source remains current |
| 46243acb | 48db6272 | Supported review_evidence_sha256 link rejected by refresh check; pre-claim legacy commit/tree links obscure the typed candidate | Fix evidence-key validation and historical binding proof; resume ordinary acceptance and trusted tests |
| 574f8ef9 | 22244103 | Old review source was explicitly superseded or has a newer typed candidate | Exact operator retirement; retain old evidence and use fresh source review |
| 587ec6eb | 610e14dc | Supported review_evidence_sha256 link rejected by refresh check | Fix evidence-key validation and historical binding proof; resume ordinary acceptance and trusted tests |
| 5bd4261b | 9e70236b | Released reviewer has no native verdict; zero process exit does not establish a review | Exact operator retirement; fresh independent review if source remains current |
| 64eb3cd1 | 8c4a9e21 | Released reviewer has no native verdict; zero process exit does not establish a review | Exact operator retirement; fresh independent review if source remains current |
| 86733334 | ad1a50e9 | Released reviewer has no native verdict; zero process exit does not establish a review | Exact operator retirement; fresh independent review if source remains current |
| 8be94bd4 | d0a4c912 | Released reviewer has no native verdict; zero process exit does not establish a review | Exact operator retirement; fresh independent review if source remains current |
| 9257d50c | 6c8e1885 | Released reviewer has no native verdict; zero process exit does not establish a review | Exact operator retirement; fresh independent review if source remains current |
| b05b45ac | ead91896 | Released reviewer has no native verdict; zero process exit does not establish a review | Exact operator retirement; fresh independent review if source remains current |
| b2600f3d | 9236c767 | Released reviewer has no native verdict; zero process exit does not establish a review | Exact operator retirement; fresh independent review if source remains current |
| b4767acd | b1f018f2 | Released reviewer has no native verdict; zero process exit does not establish a review | Exact operator retirement; fresh independent review if source remains current |
| cea17ec0 | 6e935358 | Supported review_evidence_sha256 link rejected by refresh check; pre-claim legacy commit/tree links obscure the typed candidate | Fix evidence-key validation and historical binding proof; resume ordinary acceptance and trusted tests |
| d1c2da07 | 1ba30814 | Released reviewer has no native verdict; zero process exit does not establish a review | Exact operator retirement; fresh independent review if source remains current |
| d60f8598 | 6ec30d39 | Supported review_evidence_sha256 link rejected by refresh check; pre-claim legacy commit/tree links obscure the typed candidate | Fix evidence-key validation and historical binding proof; resume ordinary acceptance and trusted tests |

The retirement command requires five exact preimages (card, source, request, node status and managed exit), newest custody equal to the original reviewer release, no acceptance transaction, no held claim, no hold, exact launch/exit/request identity, and terminal execution. It rejects an existing review outcome unless the source generation changed. It archives the proof using the existing supersession receipt, appends one operator event and verifies readback before clearing only the old pointers. It never writes a reviewer verdict or completion.

The source-link exception is limited to content aliases whose latest recorded value predates the exact producer claim. Repository and metadata conflicts remain strict. The current typed candidate, owner, claim, ref, tree, evidence and outcome must match. No persisted card or sealed context is rewritten.

Evidence: `summary.json`, per-card coord JSON and diagnosis JSON, `preflight.json`, `fixed-bindings-final.log`, and `source-links.log`. The check-only command lines are saved in `preflight.json`. The expectation files are observations, not reusable authorization for later changed state.
