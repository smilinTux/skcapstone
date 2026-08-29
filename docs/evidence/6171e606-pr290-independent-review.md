# PR 290 independent review for card 6171e606

Verdict: PASS

Reviewer: `pi-codex-chiap02-6171e606`

Candidate preparer: `codex-chiap08-pr290-reconcile`, with candidate commit authored by `pi-codex-9ccc42ec`

The reviewer identity is distinct from both candidate identities.

## Remote and object identity

Fresh GitHub and remote-ref reads proved:

- Repository: `smilinTux/skcapstone`
- PR: `https://github.com/smilinTux/skcapstone/pull/290`
- PR state: open, not draft
- Base branch: `main`
- Base head: `73d5e294ab4b7e5d450375a983978b4e76e1107b`
- PR branch: `codex/d2e6d5a9-blocker-referent-sweep`
- PR head: `14ad1faa55f650c5c53853e94ab35f027f5ec77a`
- Candidate parent: `fe4c33a5750c82b8f11273e8faf15d8d44912c24`
- Candidate tree: `239929653f0595f12207bf6a2229b8c4ddc5b193`

The exact base-to-candidate range changes four paths:

- `CHANGELOG.md`
- `scripts/fleet/blocker-referent-sweep.py`
- `src/skcapstone/blocker_referent.py`
- `tests/test_blocker_referent.py`

No gitleaks baseline, gitleaks configuration, allowlist, or other path changed.

## Independent behavior review

Source inspection and the 55 focused tests reproduced these properties:

1. Referent parsing fails closed. It requires a leading `BLOCKED`, exactly one `blocked_on=card`, one or more distinct exact `referent=card:<8 lowercase hex>` tokens, and no malformed, duplicate, overlong, uppercase, acceptance-criterion, or mixed-category markers.
2. Reads and writes use the same expanded `home`. The command defaults to report-only, and its tested custom home does not fall through to an ambient home.
3. A successful append is accepted only after the exact verdict marker is durably readable. A writer error after a durable append is recognized as success, while a claimed success without the marker is a failure.
4. Every candidate yields a receipt. Total and partial write failures retain per-card details and produce a nonzero command result.
5. The supported card mutation locks serialize same-host writers over the target and all referents. The two-process test produces one label for one verdict.
6. Cross-host locking is not claimed. Two hosts can append duplicate label events because no fleet-wide compare-and-append exists. This is bounded because labels fold as a set, a verdict or approval is not discharged, and reconciliation remains manual.
7. `PASS_FOR_` and `PASS_READY_` heads are provisional and do not discharge a block. A completed `PASS` does.
8. Closed cards remain in stale-block discovery because the stale verdict can still block readers.
9. Current `link_key` and `link_value` events and legacy `key` and `value` events are normalized before validation and work in both sweeps.

## Reproduced checks

- Focused pytest: `55 passed`.
- Ruff check on the script, source, and focused test: passed.
- Ruff format check on the same paths: passed, three files already formatted.
- Python compile on the same paths: passed.
- Git diff check: passed.
- Gitleaks 8.28.0 over exact range `73d5e294ab4b7e5d450375a983978b4e76e1107b..14ad1faa55f650c5c53853e94ab35f027f5ec77a`: two commits, about 40.17 KB, no leaks.
- Hosted checks: all 12 current PR checks passed, including Python 3.11 and 3.12 unit tests, lint, build, shim imports, provider tests, docs checks, two gitleaks checks, and GitGuardian.

The local full-suite invocation used the pre-existing fleet Python environment, where the async pytest plugin is not active even though the repository config expects it. It therefore reported 202 failures, mainly unsupported async tests, plus 6623 passes and 41 skips. This environment result is not attributed to the four-path candidate. The exact two reported repository failures were separately run against both candidate and pinned base, and failed identically:

- Notification interaction memory has `['notification']` rather than a `conversation` tag.
- Dashboard root lacks the `/control-plane/now` navigation link.

The hosted Python 3.11 and 3.12 unit suites passed on the exact candidate.

## Hashes and rollback

- Aggregate binary full-index diff SHA256: `4fbb9f375a8fcd55966c39f96b161848e1ed30baaa11f286c94000a35e624d24`
- Repair-only binary full-index patch SHA256: `d681b589ce5c1fe8bd4b98ebcd2b44dd42361f5e5c870406535cac6c2ea86b0b`
- Four-path manifest SHA256: `d69867940a435408ea720bd6204f7ef70dbcd4ea9baf32d25ce0b3eb045ee362`

Candidate file identities:

| Path | Git blob | SHA256 |
| --- | --- | --- |
| `CHANGELOG.md` | `9214ac12b5eef4c8ce62cd106449ecf04b0b3bcf` | `35feffbea610e4b5b95863c91a55d571e570ef53a041344e489655aa589dee7e` |
| `scripts/fleet/blocker-referent-sweep.py` | `65e566daaf8b62dd177df4dac749c89a8cee89db` | `865ba3d6650df2da7e2bf0380659b618c7b472780227f6d38949d167a0305c0f` |
| `src/skcapstone/blocker_referent.py` | `4dca5551fc2b0b55d7c3dbe38029b45b4dc2cbac` | `01bf7ebf92c2980712e500d795f5ee81fb4c716cec1f282fcbca247aa001c75d` |
| `tests/test_blocker_referent.py` | `2d8c4e52a43353de640afbbbdebd7701531df9d0` | `27cd6249c48808f06d9b3916ba9fbdd3703ecc8fba2a3ab99cfab5aee8ccfce7` |

An independent reverse application of the repair-only patch produced tree `32af6b0cc1bbad3151563ef7485e529271d613b8`, exactly the candidate parent tree. Rollback is one normal revert of `14ad1faa55f650c5c53853e94ab35f027f5ec77a` on the PR branch.

No repair, PR 290 mutation, merge, install, deployment, runtime mutation, service action, timer action, worker action, credential access, provider mutation, protected-data access, or cleanup was performed.
