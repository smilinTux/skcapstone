# A wedged worker can hold a card for six hours and nothing notices

Measured on the chi estate, 2026-09-19. Card `139ec63d` was held by
`pi-glm-chiap03-139ec63d` for **6 hours 18 minutes** while the worker did
nothing at all.

| signal | what it said | why it said it |
|---|---|---|
| workspace files written in 4h | **0** | the worker produced nothing |
| `pi` process | alive, 0.0% CPU, state `Sl` | waiting, forever |
| worker stdout log | 0 bytes | empty the whole run |
| wrapper beat | `disposition=RUNNING`, age 39s, **for the entire 6h18m** | it is a timer |
| systemd unit | active | the shell had not exited |
| card status | `DOING`, claim held | nothing released it |

Nothing caught it, and each of the three existing release paths was behaving
exactly as designed:

* `reap_dead_claims()` acts only when **every authoritative host reports the
  worker absent**. The unit was active, so `publish_live` listed the card as
  running and the reaper skipped it. That gate is deliberate: it is what stops
  the reaper killing a working seat.
* `_expire_idle_claims()` measures an absolute idle deadline from card events.
  It is default-off, and a long task legitimately writes no card events.
* the liveness reaper trusts beat freshness, and the beat was fresh.

**The beat lies, and cannot stop lying.** It is a
`while :; do echo ...; sleep N; done` loop inside the worker's bash shell, a
*sibling* of `pi` rather than a signal from it. `"disposition":"RUNNING"` is a
hardcoded literal. It stays fresh on a wedged worker by construction.

## The one signal that separates working from wedged

From the 2026-09-18 learnings, section 18, measured on two workers that were
genuinely productive while looking dead to the board:

| signal | live-but-working A | live-but-working B | separates? |
|---|---|---|---|
| wrapper beat age | 10s | 26s | no: a shell timer, fresh even when wedged |
| worker stdout log | 0 bytes / 4.9h | 0 bytes / 4.2h | no: empty all run |
| last card event | 2.2h | 4.0h | no: task boundaries only |
| **workspace newest write** | **269s** | **191s** | **yes** |

`classify_progress` has measured this since 2026-09-18 and was wired
report-only pending a measurement day.

## The measurement

158 `WORKER_PROGRESS` records, 12 distinct owners, 5 chi hosts,
`20260918T193007Z` to `20260919T052500Z` (the entire life of the report-only
pass). Fixture: `tests/fixtures/worker-progress-chi-20260918.log`.

`chiap04` emitted zero records and that is correct, not a gap: it had no live
workers, and the pass emits one line per live local worker.

| state | n | share |
|---|---|---|
| `progress-missing` | 110 | 69.6% |
| `progress-fresh` | 38 | 24.1% |
| `progress-stale` | 10 | 6.3% |

No `progress-claim-mismatch`, `progress-exited`, `progress-terminal-evidence`,
`progress-invalid-identity`, `progress-malformed` or `progress-clock-skew` was
observed at all. There were zero `WORKER_PROGRESS_UNAVAILABLE` lines.

`progress_age_s` distribution:

| state | n | min | p50 | p95 | max |
|---|---|---|---|---|---|
| `progress-fresh` | 38 | 2s | 14s | 132s | **225s** |
| `progress-stale` | 10 | **53,545s** | 55,795s | 57,144s | 57,445s |

**The populations are bimodal.** Not one observation in the entire window
landed anywhere between 225s and 53,545s. The threshold is chosen from that
gap, not from a percentile.

## The threshold, and its margin

`DEFAULT_WEDGE_TIMEOUT_S = 14400` (4 hours).

| bound | value | margin |
|---|---|---|
| worst gap on a worker known to be working (2026-09-18 s18) | 628s | **23x below** the threshold |
| worst `progress-fresh` observation in this window | 225s | 64x below |
| lowest `progress-stale` observation ever seen | 53,545s | **3.7x above** the threshold |
| the incident | 22,680s | caught, with 1.6x margin |

`DEFAULT_PROGRESS_TIMEOUT_S` (900s) stays exactly where it is. 900s is where a
worker stops looking *fresh*; it is a reporting threshold and killing at it
would destroy real work.

### How many genuinely-working workers would have been killed: zero

Replaying all 158 records through `classify_wedge` (the test
`test_no_genuinely_working_worker_would_have_been_killed`), **exactly two of
the twelve owners are ever actuated**, and independent evidence gathered
outside this signal shows neither was working:

* `pi-glm-chiap03-139ec63d` — the incident. 0 workspace files, `pi` at 0.0%
  CPU, 0-byte stdout log.
* `pi-qwen-chiap01-34115541` — its unit was **crashlooping**: every relaunch
  exited `status=1` within 2.5 minutes, every worker log was 0 bytes, and
  `scanned=1587` never changed across the whole 16-hour stale run, so not one
  file was added or touched. Its later return to `progress-fresh` was a *new
  generation's launch* touching the workspace, not the old process resuming.

The other ten owners are never actuated once.

### Why the absent case is deadlined rather than acted on immediately

`pi-glm-chiap03-ea911b09` reported `progress-missing` three times and then
`progress-fresh` nine times **under the same claim revision**: one real worker
that took about fifteen minutes to populate its workspace before producing
anything. **An empty workspace is a normal startup state.** A rule that acted
on `progress-missing` when first seen would have destroyed it. The 4-hour
deadline is what makes the absent case safe.

### Stale and absent are different evidence

* A **stale mtime** is a fact about this worker whatever path it was read from,
  so it needs no receipt.
* An **absent workspace** is a fact only when the path came from the exact
  generation's admission receipt. Otherwise the reporter *inferred* the path
  from the owner name, and a miss is a measurement failure. A measurement
  failure must never read as a kill signal, so it classifies
  `wedge-unmeasured`.

This is not hypothetical: card `139ec63d` had its lane renamed glm to codex
mid-flight and carries a Syncthing sync-conflict file in the admission
directory, so its receipt does not always resolve.

## What actuates, and how

`_reap_wedged_workers` in `scripts/fleet/skfleet-rotate.py`, a **third**
release path, deliberately independent of the other two. It is the inverse of
the absence path: it acts only on workers that are **present**.

1. `SKFLEET_WEDGE_MODE` gates it: `off` (default, nothing is read or acted on),
   `report` (logs `WEDGE_WOULD_STOP`, releases nothing), `enforce`. `DRY` still
   gates the mutation; a dry run remains safe to run at any time.
2. The exact generation is re-read from disk immediately before acting. A
   reclaim between the scan and the act refuses.
3. The fence is re-evaluated against the fresh claim.
4. The unit is stopped, its name validated against the worker pattern first.
5. **Only then** is `WORKER_WEDGED` recorded on the card, with the evidence:
   `progress_age_s`, `last_write_at`, `claim_age_s`, the verdict and the exact
   owner and claim revision. A stop that fails leaves no verdict and no
   release.
6. The claim is released under a CAS on the exact claim revision, and the
   release is confirmed against a **fold re-read**, because a zero exit code is
   not proof the claim moved.

Stopping the unit usually runs the wrapper's own exact-generation release. The
sanctioned owner returning its own claim is the best outcome and is counted as
such, not followed by a second release.

## What this will NOT catch

Stated explicitly, because the measurement exposed each of these:

1. **A crashlooping worker.** `pi-qwen-chiap01-34115541` relaunched every ~5
   minutes, and **each relaunch touches the workspace**, so it reads
   `progress-fresh` forever. `progress-fresh` is not proof of progress. This
   change caught that worker only because it happened to sit silent for 16
   hours between crash cycles; a tighter loop would read fresh indefinitely.
   Catching it needs unit restart counting, which is a separate signal.
2. **A worker that writes but produces nothing useful.** Any mtime counts,
   including a log line or a `.git` index write. This measures *output*, not
   *value*.
3. **A worker wedged for less than four hours.** By design. The margin is the
   safety, and the cost of it is up to four hours of a held card.
4. **A worker with no systemd unit** (migration-era tmux). There is nothing to
   stop, so the claim is left held.
5. **A truncated workspace scan.** Above `_PROGRESS_SCAN_CAP` (20,000 entries)
   the scan finds the newest of a bounded prefix and can under-report
   freshness. Harmless in a report, fatal in a kill, so a truncated scan is
   refused outright.
6. **A stale claim whose owner is not a fleet worker** (bare `jarvis`, `codex`,
   `seraph`). That is `_expire_idle_claims`'s job.
7. **Card `139ec63d`'s receipt problem.** Where a Syncthing sync-conflict or a
   mid-flight lane rename makes the receipt unresolvable, `progress-missing`
   classifies `wedge-unmeasured` and nothing happens. Fixing receipt
   resolution is separate work.

## Deployment

`~/.local/bin/skfleet-rotate.py` and `skfleet-worker-wrapper.py` are **per-host
artifacts that a git pull does not update**. This branch changes both. Landing
it changes nothing on the fleet until they are redeployed to chiap01, chiap02,
chiap03, chiap04 and chiap08.

Suggested rollout, mirroring the claim TTL rollout that this one follows:

1. Deploy with `SKFLEET_WEDGE_MODE` unset. Behaviour is identical to today
   apart from the extra `wedge=` field on the `WORKER_PROGRESS` line.
2. Set `SKFLEET_WEDGE_MODE=report` and read `WEDGE_WOULD_STOP` for a cycle. A
   known-live worker appearing there blocks enforcement rather than being
   stopped.
3. Only then `SKFLEET_WEDGE_MODE=enforce`.

---

# Superseded: the oracle above could never fire (2026-09-19, later the same day)

Everything above is kept because the incident and the actuation contract are
real. The **progress signal** it was built on is not fit, and the threshold
derived from it was measuring an artifact.

## What was actually happening on the fleet

`SKFLEET_WEDGE_MODE=report` was armed on all five chi hosts. It found nothing,
on every host, every cycle:

```
WORKER_PROGRESS|chiap01|codex-auto-9968f114|9968f114|...|state=progress-fresh|
  progress_age_s=26|...|scanned=12|wedge=wedge-progressing|wedge_timeout_s=14400
WEDGE|chiap01|mode=report candidates=0 released=0
```

`9968f114` had been running six hours at that point, with a 157 MB session
transcript, 6,882 tool calls, zero `edit`/`write` tool calls, zero commits and
zero dirty files. It read as 26 seconds fresh.

**`scanned=12` is the tell.** `_workspace_progress_at` timestamped every
`os.scandir` entry including DIRECTORIES, and early-exited the moment it found
one inside `_PROGRESS_FRESH_EXIT_S`. An ordinary `git status` writes no file
but still bumps the `.git` directory's mtime, so the walk hit `.git` within a
dozen entries and returned "fresh". Measured across all six live workers:

| workspace | `.git` dir | `.git/index` | newest real file |
|---|---|---|---|
| pi-codex-chiap03-bb0a06ef | 34s | 9,460s | 9,473s |
| pi-codex-chiap01-9968f114 | 60s | 21,600s | 21,611s |

The same held for tool caches. `1960b107`, the worker that looked healthiest,
had `.tools/uv-cache/...` then `.pytest_cache/v/cache/nodeids` then
`.ruff_cache` as its three newest entries. It was re-running pytest and ruff,
not producing.

The reading was also nondeterministic: whether the walk early-exited depended
on whether a git read had happened in the preceding 60 seconds, so the same
workspace reported `6s (scan=14)` and `140s (scan=1454)` twenty minutes apart.

## Why no threshold could have fixed it

`progress_age_s` was 11-26s for every worker on the fleet, permanently. The
14400 in the table above was never reached and never would be.

The bimodality that justified it (`progress-fresh` max 225s,
`progress-stale` min 53,545s, nothing between) was partly the artifact: the
fresh population was measuring git reads, not writes.

## And it must never actuate even when correct

**37.7% of cards are required to write nothing.** 2,601 of 6,901 folded
cards, and 20 of the 41 in `DOING`, carry the `source-only` label. Their
acceptance criteria say it outright: *"Read-only audit. No edits, commit,
push"*, *"Publish hash-linked card evidence and SKMail; repository remains
clean"*. Their deliverable is card evidence and mail, not a diff.

Under a workspace-mtime deadline every one of them is a kill candidate from
the moment it starts. Arming that signal would have reaped about half of all
active work for complying with its own card. The six workers examined on
2026-09-19 were all `source-only` audits, all alive, all doing what they were
asked.

## The replacement: the agent's own transcript

`pi` appends a record per assistant turn and per tool result to
`~/.pi/agent/sessions/<slug>/<stamp>_<uuid>.jsonl` while it runs, so the
file's mtime is the last moment the agent did anything at all. That separates
"reading and reasoning" from "stopped", which workspace mtime cannot.

`_session_progress_at` reads it and is preferred whenever present. Only it may
actuate. A workspace-mtime reading is reported with `source=workspace-mtime`
and classified `wedge-unmeasured`. Total absence (no workspace and no
transcript) keeps the existing absent-reaper deadline.

## The re-derived threshold

`DEFAULT_WEDGE_TIMEOUT_S = 7200` (2 hours), down from 14400.

Measured over **2,754 fleet sessions** with >=20 events, all four chi worker
hosts. Longest silence *inside* a session whose worker went on to keep
working:

| p50 | p90 | p95 | p99 | p99.9 | max |
|---|---|---|---|---|---|
| 45s | 184s | 300s | 513s | 1,350s | **2,558s** |

Sessions with a max gap above one hour: **0 of 2,754**.

| bound | value | relation to 7200 |
|---|---|---|
| worst observed healthy silence | 2,558s | **2.8x below** |
| largest bash tool timeout ever issued (a full `pytest` run, and the longest a single legal tool call can hold the transcript silent) | 3,600s | **2.0x below** |
| the `139ec63d` incident | 22,680s | **3.2x above** |

The empty band is 2,558s..22,680s and 7200 sits inside it. 3600 also kills
zero working workers on replay, but it equals the maximum legal single tool
call, so it is not left as the margin.

**Genuinely-working workers this threshold would kill: zero**, by both
measures. Replaying the 158-record fixture moves 25 records from
`wedge-within-margin` to `wedge-absent-confirmed`; every one belongs to
`pi-glm-chiap03-139ec63d`, the incident worker. The actuated owner *set* is
unchanged, and the slow-starting `pi-glm-chiap03-ea911b09` is untouched.

## Also checked, and not the problem

`pi` **does** bound a lost gateway response. `dist/core/http-dispatcher.js`
sets `DEFAULT_HTTP_IDLE_TIMEOUT_MS = 300_000` as both `bodyTimeout` and
`headersTimeout` on the undici global dispatcher, and `dist/core/sdk.js`
passes the same 300s as the per-request `timeoutMs`. Neither is overridden on
the chi hosts (`~/.pi/agent/settings.json` does not set `httpIdleTimeoutMs`),
and `retry.maxRetries` defaults to 3. A lost response therefore fails in about
300s and stalls a worker for at most ~20 minutes across retries, well inside
this deadline. The residual gap is that `bodyTimeout` is an *idle* timeout
between chunks, not a cap on total request duration, so a backend that
trickles bytes indefinitely would not trip it.
