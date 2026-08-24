# Criteria-fold uptake landing evidence

Date: 2026-08-24

Card: `da1e302d`

Local verdict: PASS

Remote verdict: pending normal pull request checks and protected merge

## Reviewed custody

- Landing base: `3a50b69bfe8f0190ba9241e8abbdca165d75f072`
- Landing base tree: `d6e99c6ad3569f6e9b37afb59d8c31b5b4cf99cf`
- Reviewed source: `fbc1ec3e11e832bc7942992d3d3394f0bfa9875b`
- Reviewed source tree: `40b4bd0a2b5948d5ad79fa48707fd8b596caeaef`
- Producer evidence: `e28b185c1b46b018eec554183093fb68ea79a394`
- Independent PASS evidence: `ba5578ded0389c52c82fee3e9c9e1474203cadac`
- Landing TDD: `07a1681bb22e73dba08f4a254b558c295c4f0f54`
- Qualified landing source: `086bec6d36822dea481f7e860892e715adb6662a`
- Qualified landing source tree: `e8ea9e342e27d64c55956f992964cd0b803d94e5`
- Worktree: `/tmp/skcapstone-da1e302d-landing-20260824`
- Artifact root: `/tmp/skcapstone-da1e302d-artifacts-20260824`

Startup, coordination status, the complete briefing, and the required full Git
fetch ran before claim. Review `a6c7fd2f` read back DONE with PASS before
`da1e302d` was claimed.

## Exact reconciliation

Current `origin/main` was also the exact merge base of the reviewed source.
The six reviewed commits cherry-picked without conflict. No manual source or
changelog resolution was needed.

The stable patch ID of the full reviewed semantic diff and the landed semantic
diff is exactly:

`003a6898670fdebc025dccc4b7f6922a9317138d`

All 11 reviewed path blobs are byte-identical to the independent review source:

- `.github/workflows/pytest.yml`
- `CHANGELOG.md`
- `SOP.md`
- `docs/RELEASING.md`
- both reviewed TDDs
- `pyproject.toml`
- `scripts/build_reproducible.py`
- `src/skcapstone/coord_amendments.py`
- `tests/test_coord_amend.py`
- `tests/test_reproducible_build.py`

The only additional pre-evidence path is the landing TDD. Every concurrent
Unreleased changelog entry from the landing base remains present, together
with the reviewed SKCoord uptake and reproducible-build entries. Every landing
commit has the required co-author trailer. The gitleaks baseline blob stayed
exactly `83b2b20990e8c3230e309e83aa4da04f9302c76a`.

## Deterministic artifacts

Two separate clean local clones were pinned to qualified landing source
`086bec6d36822dea481f7e860892e715adb6662a` and built at different wall-clock
times through the reviewed driver.

- Derived `SOURCE_DATE_EPOCH`: `1787592796`
- Exact package version: `0.15.63.dev7+g086bec6`
- Exact dependency metadata: `skcoord>=0.1.39`

| Artifact | SHA-256 in both builds | Size | Members | Normalized payload SHA-256 |
| --- | --- | ---: | ---: | --- |
| wheel | `0b171c507e5a24370f7f2d2d02c5b1c1bf52a63a93cf802a7b541f2ae2068449` | 1,456,832 | 454 | `5bb86fa50f7182fcb972aacf9c44512fe21631e730a57af12bacde6b2c65a39c` |
| sdist | `c576d2b419a86a9f6c0dcd8821c3fbb7ef352877bb250f69819c74ce527097bf` | 2,786,548 | 1,221 total, 1,131 files | `278040746382fc33df63f5b765958688936752cf3dd5a0f99f5851932a32311c` |

`cmp` passed for both pairs. All wheel members used one source-derived ZIP
timestamp. Every sdist member and its gzip header used epoch `1787592796`,
numeric owner `0:0`, and empty owner names. The four candidate files were made
read-only after qualification.

Two ordinary clean-clone controls omitted the deterministic procedure and
proved both formats still detect the original defect:

| Artifact | Ordinary A SHA-256 | Ordinary B SHA-256 |
| --- | --- | --- |
| wheel | `047b0b9f4b5d21fe9e301de4deded6bb852e0325d34f06945ff84c32525cf1f9` | `56e1ef2cb3e9fb0f34a370c88ac783f187cef2ef1d0b9aa2e940c87bd9966dd9` |
| sdist | `bfb8c1aa1e59ba245a6adc9cd437a12dad8c8ab859d8f277002db3f30a5fb765` | `afaeed8a5117780a5f341d235295fc8c32374856a91ff9240d88295557133179` |

## Noneditable qualification

Each independently built wheel was installed in its own fresh environment
with declared `[all,dev]` extras and these exact published inputs:

- SKCoord 0.1.39 wheel SHA-256:
  `28c01960a1bac630aecb4c4327c6f029031420844ba08ce91edf7b4775a3b288`
- SKHarness 0.3.44 wheel SHA-256:
  `45de86d754dcc438a6bb5b1e5ddcaa97341dfee4ab20c1cafe095e491f83c943`

Both environments resolved SKCapstone, SKCoord, SKHarness, and SKChat from
their own `site-packages`. Both `direct_url.json` records identified archive
installs, neither was editable, and both passed `pip check`.

Both installed CLIs returned the exact ordered nine current criteria for:

`unset`, `1`, `dual`, `0`, `off`, `false`, `no`

After an empty malformed amendment event, every selector denied both kanban
read and claim. No stale birth criteria were emitted, no agent claim file was
created, and task, core, and event bytes stayed unchanged.

One preliminary disposable harness invocation stopped before its malformed
checks because it guessed the event-log path incorrectly. No product file or
state was changed. The corrected harness discovered the attributed event file
and both complete matrices then passed.

## Local gates

| Gate | Result |
| --- | --- |
| focused build plus criteria tests | 34 passed in 2.34s |
| full declared environment suite | 6,558 passed, 41 skipped, 0 failed in 366.27s |
| Black 26.5.1 | four changed Python files unchanged |
| Ruff 0.15.4 | all checks passed |
| retired shim scan | passed |
| docs-check tiers 1 through 3 | passed with changed-file context |
| Twine 6.2.0 | both wheels and both sdists passed |
| Gitleaks 8.28.0 | 789 commits, about 14.33 MB, no leaks |
| `git diff --check` | passed |
| clean-clone rollback | exact landing base tree restored |

The full suite retained only its normal environment-gated skips. No test was
weakened, masked, or newly skipped.

## Rollback and boundaries

A separate clone reverted all seven pre-evidence landing commits. Its clean
final tree was exactly `d6e99c6ad3569f6e9b37afb59d8c31b5b4cf99cf`,
the fetched landing base tree. Any later remote rollback must use a new normal
pull request. Published history will not be rewritten.

No shared checkout file, service, runtime, baseline, ATLAS or operator state,
credential, protected data, provider, remote branch, tag, or external action
was changed during local qualification.

## Remaining remote gate

The evidence commit is the immediate successor of the qualified source and is
linked on the landing card. Before push, the branch must be refetched and
reconciled if main moved. The card remains incomplete until every required pull
request check passes, normal protected merge succeeds, post-merge main is
verified, and any automatic patch release and artifacts are read back.
