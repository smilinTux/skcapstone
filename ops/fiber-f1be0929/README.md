# Fiber deployment review snapshot

Parent card: f1be0929. Producer: jarvis (Codex family).
Human authorization: local-only snapshot commit for independent review,
with no push, merge, or application deployment, received 2026-09-29.

This directory is an exact review snapshot of staged operational artifacts,
not a change to the SKCapstone product modules. Base revision:
c6031047328348aca53dc3611cf94ceb56a9006c.
All new files for this snapshot are under ops/fiber-f1be0929/.
The local repository has no remote configured, preventing accidental pushes.

The Python files are candidate operational sources for ~/.local/lib/skfleet-fiber/.
The revised controller is staged, not installed. The worker startup PATH fix
is installed on chiap08 and chiap03 to permit independent review execution;
chiap02 and chiap04 still need the reviewed update before canary execution.
The admission file also matches chiap02/03/04. Shell candidates correspond
to the installed llm-orch wrappers. Reports are chronological and contain
earlier hashes; READINESS-AUDIT-20260929T2219.md has the later installed hashes.
MAINTENANCE-PAUSE-20260929.md supersedes the earlier natural-drain-only posture.
Credentials, live agent catalogs, CardStore data, Matter content, and private
session transcripts are deliberately excluded. approved-aliases.json contains
nonsecret model descriptors only, not credential values.

## Independent review contract

Review the exact snapshot commit and tree, compare SHA256SUMS against files,
and inspect safety behavior rather than accepting the producer's reports.
The independent reviewer must use a qualified different provider family and
its own governed claim. No edits to production files, live services, claims
belonging to others, or deployment flags are permitted by this snapshot.
Use scratch resources for adversarial tests. Return exact commands/results,
actionable findings with paths/lines, and acceptance gaps. Do not write PASS
merely because component tests pass. Do not fabricate hosted or native CI.

Reproduce component checks from this directory:

```sh
PYTHONPATH="$PWD" python -m unittest discover -s tests -p 'test_fiber*.py'
PYTHONPATH="$PWD" python -m unittest discover -s tests -p 'test_verify_card.py'
```

These are focused component checks, not the six full SKCapstone review gates.
For all snapshot tests, including synthetic catalog migration and legacy
rollback, run with BASH_ENV=/dev/null, FIBER_CANDIDATE_DIR set to this directory,
FIBER_FILL_NAME=fill-slots.candidate.sh and
FIBER_PROMPT_NAME=make-prompt.candidate.sh, then use discovery pattern test_*.py.
The native cgroup assertion skips outside its explicitly bounded remote unit.
The two unsafe originals in backup/ are rollback test inputs only. Never run
them or restore the destructive launcher into service.
The Python environment must contain the existing SKCapstone runtime. Record
which interpreter and import paths were used. Also examine catalog migration,
worktree refusal, claim failures, unavailable hosts, duplicate reservations,
provider accounting, launch spacing, transport ambiguity, and native receipts.

## Known open issues and mandatory holds

- No distributed implementation/review/test canary has run. No useful-work
  throughput improvement has been demonstrated.
- The revised qualification helper rejects a served model different from the
  native selected bucket member, or a different provider/request attribution.
  A live sk-zai-m response selected glm-4.6 but served glm-5.3-flash. GLM
  admission remains held; transport success is not exact-model qualification.
- Live review startup exposed missing CLI and Node directories in systemd
  and child PATH. The candidate sets one shared explicit PATH and runs Pi
  --version during preflight. Failed attempts and workspaces were preserved.
- The gateway omitted the deepseek-flash alias from per-model context limits.
  A 120 KB fallback discarded hundreds of messages, causing repeated review
  inspection. Bounded gateway repair 309ae923 added only this alias, passed
  native rollback/reload checks and 30 regressions, and preserved a middle
  marker in an actual 190 KB, 183-message DeepSeek completion. This does not
  itself qualify the full rollout or its interrupted review.
- Several native CLI processes are counted conservatively, including possibly
  idle terminals. Do not erase them from occupancy without positive evidence.
  Codex family accounting currently prevents adding another Codex worker.
- Review job requests need a qualified seat identity and explicit producer
  provider. The generic describe helper's default owner alone is not sufficient
  for governed review. Validate the actual request and native admission.
- A failed launch acknowledgment retains capacity and claim ownership. There
  is no automatic blind retry; review safe recovery before treating it as ready.
- Remote claims require local replica visibility as well as the authority's
  exact generation. Sync delay can refuse a start; do not bypass that fence.
- The old babysitter resumed workers after a pause. It is stopped and must
  not be restarted unchanged. Auto-merge/nightly publishing remain paused.
- Kimi remains fenced. No entitlement or qualification override is authorized.

New dispatcher admission remains held and its timer disabled. Independent
review, required checks, source-bound canary stages and external acceptance
are still required before enabling regular dispatch. This snapshot is not
a provisional PASS for the entire deployment card.
