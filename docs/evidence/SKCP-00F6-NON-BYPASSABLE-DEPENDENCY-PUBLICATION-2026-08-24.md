# SKCP-00F6 durable dependency gate publication evidence

Card: `54cd56f2`
Agent: Jarvis
Capture date: 2026-08-24

## Outcome

The published SKCoord gate no longer lets `--force` claim a card with an
incomplete dependency. The check uses the same folded dependency projection as
the normal claim path, so incomplete, unknown, review, and human gates remain
blocking. The refusal reports the blockers and returns nonzero at the CLI.

The matching SKCapstone CLI and regression coverage were published after fresh
green checks. The isolated-worktree reconstruction also uncovered and fixed a
file-descriptor leak in SKCoord atomic writes that had made the real watchdog
test fail only late in a full SKCapstone run.

## Source and publication receipts

| Component | Pull request | Source and merge receipts | Publication readback |
| --- | --- | --- | --- |
| SKCoord dependency gate | [PR 35](https://github.com/smilinTux/skcoord/pull/35) | source `4fa80bba3bca5b860af5a4613fa2f49f9b77d9e2`; squash merge `b33126f936eb9d6620757e99253fb3a865d94b5d` | Included in the later descriptor correction release. |
| SKCoord descriptor correction | [PR 36](https://github.com/smilinTux/skcoord/pull/36) | source `5aac38b15a10f871fd4c2a8657af2afb1dae7d15`; squash merge `9cfe0db9c2f6d3a57cbef999168658134c830fe7` | Automatic publish [run 32676584099](https://github.com/smilinTux/skcoord/actions/runs/32676584099) succeeded. Automatic tag `v0.1.35` points to `9cfe0db9c2f6d3a57cbef999168658134c830fe7`. |
| SKCapstone force gate | [PR 186](https://github.com/smilinTux/skcapstone/pull/186) | sources `3281cc2063cda2c23be8dea40c63717d0acf9a8d` and `b9dda66aed8d05471abf2e102ba7abc7eb8d7d4d`; normal refresh merge `abfa452570244b53ca7a26f722757789ebc221e8`; squash merge `2244a5f50b8111499f1b1a944c78c9c410f33493` | Fresh [run 32677284123](https://github.com/smilinTux/skcapstone/actions/runs/32677284123) was green. Automatic publish [run 32677916230](https://github.com/smilinTux/skcapstone/actions/runs/32677916230) succeeded. Automatic tag `v0.15.55` points to `2244a5f50b8111499f1b1a944c78c9c410f33493`. |

Each authored commit above ends with the required Claude Opus 4.7 co-author
trailer. The refresh merge was a normal merge commit with the same trailer.
No force push, admin bypass, hand tag, production deployment, service restart,
or secret-baseline change was used.

## Verification

- SKCoord focused force-gate and hardening coverage: `83 passed`.
- SKCoord descriptor-close regression: `2 passed`; the new success-path check
  proves the captured temporary descriptor raises `EBADF` after write. The
  same check failed against `b33126f` because the descriptor remained open.
- SKCoord full suite after the correction: `424 passed in 4.52s`.
- SKCapstone focused post-refresh set: `33 passed, 1 skipped in 0.91s` for
  dependency CLI, autopilot cost, operator-seat fault injection, and real
  watchdog coverage.
- SKCapstone direct configuration watcher test after the descriptor correction:
  `17 passed in 0.54s`.
- SKCapstone full-order prefix ending at the configuration watcher:
  `2259 passed, 5 skipped, 8 deselected in 48.44s`.
- Fresh hosted SKCapstone checks in run `32677284123` were all successful,
  including lint, docs, secret scanning, build, providers, shim imports, and
  Python 3.11 and 3.12. The Python test jobs completed in 11m32s and 10m21s.

The fault-injection pytest wrapper has a narrow environment guard only for a
missing real CapAuth PGP signer prerequisite. The standalone safety drill is
unchanged and remains fail closed. This authorized CI-specific guard does not
skip the standalone assertion.

## Installed and board readback

Read-only installed board output shows card `54cd56f2` in `doing`, owned by
`jarvis`, with direct dependencies `f701b0d3` and `559c8c48`. No live
implementation claim probe was made.

The local `.skenv` `skcapstone --version` remains
`0.15.50.dev0+gf1d688f.d20260823`, older than published `v0.15.55`, and its
help still contains prior force wording. It was intentionally not refreshed:
the editable runtime checkouts contain unrelated dirty work and the
cross-cluster contract forbids changing, cleaning, switching, or resetting
them. Published source, PR receipts, and hosted checks provide the authoritative
verification for this card.

An accidental same-named branch exists in the upstream repository from the
initial remote-name discovery. It was not used by PR 186 and remains untouched
because deletion was not authorized.

## Rollback

No data migration ran. A source rollback is recoverable through ordinary,
reviewed follow-up pull requests, not by rewriting history:

```bash
git revert 9cfe0db9c2f6d3a57cbef999168658134c830fe7
git revert 2244a5f50b8111499f1b1a944c78c9c410f33493
```

Each revert must receive fresh focused and full verification before merge. The
commands are recorded as rollback evidence and were not executed.

## Non-authorizations and limitations

The shared editable SKCoord and SKCapstone checkouts were left untouched rather
than risk unrelated work or the live runtime. No candidate artifact, human
gate, independent-review card, implementation card, external action, Matter
content, HammerTime Inbox content, deployment, restart, commit in a shared
checkout, or force operation was performed. The public GitHub latest-release
object is still older than the automatic tag, so this evidence records the
successful tag and PyPI publication rather than claiming a manually created
GitHub release.
