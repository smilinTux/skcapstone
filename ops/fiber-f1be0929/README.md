# Fiber deployment review snapshot

Parent card: f1be0929. Producer: jarvis (Codex family).
Human authorization: local-only snapshot commit for independent review,
with no push, merge, or application deployment, received 2026-09-29.

This directory is an exact review snapshot of staged operational artifacts,
not a change to the SKCapstone product modules. Base revision:
c6031047328348aca53dc3611cf94ceb56a9006c.
All new files for this snapshot are under ops/fiber-f1be0929/.
The local repository has no remote configured, preventing accidental pushes.

The Python files correspond to ~/.local/lib/skfleet-fiber/ on chiap08.
Admission and worker files also match chiap02/03/04. Shell candidates correspond
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
- The installed qualification helper accepts any nonempty glm-prefixed served
  model. A live sk-zai-m response reported glm-5.3-flash, currently class S.
  That transport success is not proof of M-class qualification. GLM admission
  must remain held until exact requested/resolved/served qualification agrees.
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
