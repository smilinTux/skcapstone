# Released acceptance replay diagnosis, 2026-10-13

Read-only inspection on chiap08 found one pending step-08 source PASS event for
each of the five reviews. Every transition request matches its retained intent
exactly. Each stored `guarded_after_card_revision` equals the historical folded
card at the PASS timestamp. Removing only the later exact claim release does
not restore that digest, because operator link corrections remain.

| Review | Source | Native PASS event | Later corrections |
|---|---|---|---|
| 7907fdaf | 2587020f | 583afba446964e65a0cd140276f56f34 | `pr` cleared; `commit` expanded to candidate head |
| 8f5077b2 | 7ad93023 | 11248a349222495a99781da7bb7d703e | `commit` expanded to candidate head |
| a4ed7c20 | 85c1b063 | 15c56d3442854fe7812fd0238cd222a3 | Invalid compare URL in `pr` cleared |
| add19bf8 | 7ad93014 | 2a495572529147fda5bd3ef71857652d | `pr` cleared; `commit` expanded to candidate head |
| bb6da93e | 56fddc0b | fa6b4024a9af4dc49554be6eb22301d6 | `pr` cleared; `commit` expanded to candidate head |

The PASS events were written on October 11 before the corrections. The
corrections were attributed to `lumina-nor` around 20:05 UTC that day in the
mediated legacy event lane. The sweeper release followed on October 12. All
five directories have `test-requalification.json`; none has `reseal.json`.

This is a valid replay refusal, not evidence of an incorrect first-write digest.
Ignoring those changes in ordinary replay would weaken the documented revision
fence. The existing operator-attributed acceptance-reseal path is designed for
these exact PR/commit corrections, but currently requires a held source claim
and the original trusted test receipt. Supporting the clean release and the
already governed requalification in that recovery path is the narrow fix.
Retain exact expected hashes, history checks, operator attribution, artifact
validation, and the step-09 continuation. Never issue another PASS or review
completion, and never reclaim under the producer identity.

No board writes or live acceptance retries were performed for this inspection.
