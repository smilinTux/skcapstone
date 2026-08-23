# skharness execute bridge: architecture (R2 design, no implementation)

**Date:** 2026-08-13
**Status:** DESIGN. Follow-up to the R1 seam (`skcapstone.agent_run.set_execute_dispatcher`,
card 182b947f) and step 1 of `docs/runbooks/ai-runner-go-live.md`.
**Scope:** wire the fleet suggestion engine's EXECUTE runs into the sandboxed and
graded `skharness.autocode` engine, without disturbing the (disabled) autopilot
engine, skcode's ratify endpoint, or any existing caller.

Every claim below is grounded in code read on 2026-08-13:

- `skcapstone/src/skcapstone/agent_run.py` (the R1 seam, `process_one`, `claude_dispatcher`)
- `skharness/src/skharness/autocode/{types,executor,engineering,direct,ratify,config,harness,sandbox,orchestrator,journal,digest}.py`
- `skharness/src/skharness/autocode/adapters/{__init__,claude_code,base}.py`
- `skharness/src/skharness/{serve,daemon}.py` (the skcode dispatch surface)
- `skcoord/src/skcoord/{coordination,card_store}.py` (Board.claim_task, card fold)

---

## 1. Decision: which entrypoint

**Chosen: a bridge-local subclass of `DirectExecutor` for the sandboxed run and
draft PR, plus `ratify()` for the grade.** One WorkItem, one isolated worktree,
one sandboxed harness round, an independent twin-gate grade over the resulting
diff, then commit + push + a DRAFT PR. Structurally incapable of merging.

### Why not the alternatives

| Option | Verdict | Reason (from the real code) |
|---|---|---|
| `serve.py` / `daemon.py` dispatch API | **No** | `POST /api/v1/dispatch` (daemon.py:399) spawns an *interactive skcode session* via `harness.spawn(SessionDescriptor, prompt=...)`. It has no WorkItem, no grade, no twin gate, no PR pipeline. It is the RCE surface for a human-driven session, exactly what R1 exists to route around. Reusing it would be `claude -p` with extra steps. |
| `EngineeringExecutor.run` with automerge off | **No (for P1)** | The gated Ralph loop calls `self.board.score_task(item.ref, ...)` every round (engineering.py:332), and `Board._write_task_raw` raises `FileNotFoundError` for any id without a `tasks/<id>-*.json` file (skcoord coordination.py:326-346). Agent-run cards include GTD/ITIL shadow cards that have no coord task file, so the crown-jewel loop cannot run against them without either mutating the coord board or being modified. Modifying engineering.py is exactly what we must not do. Revisit in P3 for repos whose `min_quality` floor is GATED. |
| `ratify()` alone | **No (alone)** | `ratify(repo, worktree, acceptance, harness)` grades an *existing* diff and explicitly never commits, pushes, or merges (ratify.py:34-61). It does not create the diff. It is the right second half; it needs `DirectExecutor.run` as the first half. |
| `DirectExecutor.run` + `ratify` + `DirectExecutor.finalize` | **Yes** | `DirectExecutor` (direct.py) is documented and built as "one sandboxed run, branch + diff + PR, NEVER merges". Its `_merge` raises unconditionally (direct.py:63-69) and its `finalize` has no merge path and refuses gated results (direct.py:71-96). `ratify` reuses the pinned `twin_gate_passed` predicate so the grade cannot drift from the crown jewel (ratify.py:59, engineering.py:59-70). Composition gives the runbook's required "sandbox -> grade -> twin-gate -> draft PR" without touching either file. |

### The bridge executor subclass

The bridge defines (in skharness, see section 6) a small subclass, never
registered in `EXECUTORS`:

```python
class AgentRunDirectExecutor(DirectExecutor):
    kind = "agentrun-direct"          # never register()ed; constructed per dispatch

    def claim(self, item):            # agent_run already claimed the card under its
        # own lease (agent_run.claim_run). There is no coord task file to claim;
        # Board.claim_task would raise ValueError -> ClaimRaced for shadow cards.
        self.journal.record_claim(item.ref, claimed_at=_now_iso())

    def _settle_economics(self, item, sha):   # P1: no joule mint from ad-hoc runs
        return None

    def _open_pr(self, repo, pr_branch, item):
        # identical to EngineeringExecutor._open_pr but adds --draft and records
        # the url on self so the dispatcher can return it in links["pr"].
        ...
```

Three overrides, all subclass-local, all *narrowing* behavior. Everything else
(worktree creation with self-healing, `_stage_work`, `_diff`, `_commit_and_push`,
`_pr_base`, `prune_worktree`, the direct-mode `GateResult(mode="direct")`) is
inherited verbatim from direct.py / engineering.py.

### Exact call sequence

`execute_dispatch(context)` where `context = {card_id, kind, title, instruction,
agent, mode}` (agent_run.py:596-603):

```
 1. home = skcapstone.mcp_tools._helpers._shared_root()
    card = CardStore(home).fold(card_id)              # labels, description, meta
    -> refuse if card is None
 2. cfg = skharness.autocode.config.Config.load()     # fresh instance, no singleton
    -> refuse if cfg.repo_map is empty (missing autopilot.yaml loads a disabled
       default with an empty repo_map, config.py:72-75)
 3. repo resolution (section 2): exactly one `repo:<name>` label, name in
    cfg.repo_map -> repo: RepoSpec. Refuse otherwise.
 4. policy checks (all refuse, fail-closed):
    - not cfg.live_execution            (Sandbox.spawn would hard-raise anyway,
                                         sandbox.py:106-109; refuse early + clearly)
    - repo.automerge or repo.name in cfg.automerge_repos   (belt and braces; the
      executor cannot merge regardless)
    - coerce_quality(repo.min_quality) == QualityMode.GATED  -> refuse: this repo's
      floor demands the gated crown jewel; P1 does not provide it (see P3)
 5. harness = skharness.autocode.harness.build_harness(cfg)      # resolves
    cfg.harness ("claude-code" default) via the HARNESSES registry; unknown name
    raises (harness.py:92-100). NOTE: the autocode build_harness signature is
    (config, name=None); the top-level skharness/harness.py build_harness is
    (name, config) and serves the skcode session plane, not this path.
 6. item = WorkItem(kind="agentrun-direct",
                    ref=f"airun-{card_id}",           # namespaced, see section 5
                    source="agent-run", repo=repo.name,
                    payload={"title": context["title"],
                             "description": instruction (+ card.description),
                             "acceptance": card.meta.get("acceptance") or [instruction],
                             "tags": [f"repo:{repo.name}"],
                             "unblocked": True, "verdict": "valid"})
 7. handle = skharness.autocode.journal.handle(f"airun-{card_id}-<utcstamp>")
    ex = AgentRunDirectExecutor(cfg, board=None, journal=handle, digest=_shim)
    -> board=None is safe: claim is overridden and DirectExecutor.run/finalize
       touch the board nowhere else (direct.py:45-96); the shim captures
       queue_decision text into the returned activity instead of writing GTD.
 8. gr = ex.run(item, harness)
    = journal-claim -> make_worktree (git worktree add -b autopilot/airun-<id>,
      self-healing, engineering.py:177-197) -> harness.run_task(TaskBrief) ONE
      round in the Docker sandbox -> staged diff -> GateResult(score=None,
      passed=run_ok and diff nonempty, mode="direct")   (direct.py:45-61)
 9. wt = handle.worktree_for(item.ref)
    rr = ratify(repo, wt, acceptance, harness)         # grade only: stage, diff,
      external_ci_verdict, diff_coverage, harness.grade, twin_gate_passed.
      Never commits/pushes/merges (ratify.py:34-61).
10. if gr.passed: ex.finalize(item, gr)                # commit+push branch, open
      DRAFT PR, decision text captured by the shim (direct.py:71-96).
      MUST pass gr (mode="direct"), never rr: ratify returns the GateResult
      default mode="gated" (types.py:123), and DirectExecutor.finalize hard-raises
      on a gated result (direct.py:78-81). That guard working FOR us is by design.
    else: ex.prune_worktree(repo, wt); refuse-summary (no PR, no push).
11. return {"summary": "draft PR <url>; independent grade <rr.score>/5, "
                       "twin gate <PASS|not passed>; human review required",
            "activity": [... one entry per step, incl. harness tokens/cost
                         from HarnessResult, and rr.notes ...],
            "links": {"pr": pr_url, "branch": f"autopilot/airun-{card_id}"}}
```

**links keys are constrained by the card fold:** `agent_run_state` folds only
`("pr", "commit", "branch", "transcript")` into `run.links`
(skcoord card_store.py:394-402). Anything else is silently dropped, so the
bridge uses exactly `pr` and `branch`.

**Error contract:** policy refusals (steps 1-4, empty diff, PR-open failure)
return a well-formed `{"summary": "execute refused (bridge): <reason>",
"activity": [...], "links": {}}` so `process_one` records it and moves the card
to review with the reason visible. Only unexpected infrastructure exceptions
propagate, which `process_one` already converts to state FAILED with the error
recorded (agent_run.py:643-647). Either way nothing was pushed unless a draft
PR url is present.

**PR-open failure is not silent:** `_open_pr` returns `proc.stdout.strip()`,
which is empty when `gh pr create` fails (engineering.py:397-409, the failure is
printed but swallowed). The bridge treats an empty url after a successful
commit+push as a refusal-with-branch: summary says the branch was pushed but the
PR must be opened by hand, `links = {"branch": ...}`.

---

## 2. Repo resolution (the card has no repo field)

The context carries no repo. The resolution mirrors the engine's own
`resolve_repo` contract (engineering.py:135-143: exactly one `repo:<name>` tag,
and the name must be a `config.repo_map` key):

1. Re-fold the card: `CardStore(home).fold(card_id)`. Coord-task-derived cards
   carry the task's tags as labels (skcoord card_store.py:557), so a card whose
   coord task was tagged `repo:skchat` resolves exactly as autopilot would
   resolve it. GTD/ITIL shadow cards (ensure_card, agent_run.py:92-168) carry no
   repo label and therefore refuse.
2. Collect `repo:` labels. **Exactly one**, and it must be in `cfg.repo_map`.
3. Anything else is fail-closed, with the count in the refusal reason:
   - zero labels: "no target repo on the card; add a repo:<name> label"
   - more than one: "ambiguous target (repo:a, repo:b)"
   - unknown name: "repo:<name> is not in autopilot.yaml repo_map"

**No inference.** The bridge never parses the instruction text for a repo name
and never defaults to "the obvious repo". An EXECUTE run writes to a real
repository; the target must be an explicit, operator-visible label, same as the
autopilot contract.

---

## 3. Config / env prerequisites (each one fail-closed, each on the go-live checklist)

The bridge factory (`build_execute_dispatcher()`, section 6) checks the static
ones at wiring time and returns `None` when absent, which leaves the R1 seam in
its shipped fail-closed state. Per-run ones are re-checked per dispatch.

| # | Prerequisite | Where enforced if absent |
|---|---|---|
| 1 | `SKAI_RUNNER_LIVE=1` | agent_run.process_one: without it nothing dispatches (agent_run.py:605) |
| 2 | `SKAI_EXECUTE_BRIDGE=1` (new, bridge opt-in) | wiring helper never imports/wires the bridge |
| 3 | skharness importable in the runner venv | wiring helper catches ImportError, stays fail-closed |
| 4 | `~/.skcapstone/config/autopilot.yaml` (or `SKOS_AUTOPILOT_CONFIG`) exists with a non-empty `repo_map` | missing file loads the disabled default (config.py:72-75); factory returns None |
| 5 | `live_execution: true` in autopilot.yaml | bridge refuses per run; independently, `Sandbox.spawn` raises `HarnessUnavailable` (sandbox.py:106-109) |
| 6 | target repo in `repo_map` with valid `path`, `base_branch`, `test_cmd`, `ci` | refusal per run (section 2); `external_ci_verdict`/`diff_coverage` need `ci`/`coverage_cmd` for a meaningful grade |
| 7 | harness resolvable: `cfg.harness` in HARNESSES (claude-code default) | `build_harness` raises ValueError (harness.py:96-99); factory converts to None |
| 8 | docker present + sandbox image (`repo.sandbox_image` or `cfg.sandbox_image`, default `sandbox-claude:1`) + `sandbox-proxy:1` image | `Sandbox._ensure_capable` raises `HarnessUnavailable` (sandbox.py:96-103) |
| 9 | long-lived `CLAUDE_CODE_OAUTH_TOKEN` for headless runs | adapter falls back to the host access token and warns near expiry (adapters/claude_code.py:20-55); checklist item, not a hard gate |
| 10 | `gh` authenticated + `origin` remote on the target repo | push is best-effort (engineering.py:370-381); empty PR url is surfaced as refusal-with-branch (section 1) |
| 11 | `SKAI_AUTHZ=both` so queuing execute needs the `agentrun.execute` capability | existing runbook step 3, unchanged |

**Flag-coupling note (must be in the checklist):** `live_execution: true` is the
same flag the autopilot engine reads. Setting it for the bridge does NOT start
autopilot: the scheduled `skos autopilot run --once` is still governed by
`enabled: false` (kill switch, orchestrator.py:767/798/816) and `dry_run: true`,
and the live/canary CLI path requires an explicit human invocation
(orchestrator.py:889-894). But it does arm that manual CLI path, so the go-live
checklist must say so out loud.

---

## 4. Draft-only guarantee (no merge, no commit-to-main, no self-approval)

Layered, each layer independent:

1. **Structural (the chosen executor):** `DirectExecutor._merge` raises
   unconditionally (direct.py:63-69) and `finalize` contains no merge call and
   refuses any `mode == "gated"` result (direct.py:78-81). The bridge subclass
   only narrows this further. The `automerge`/`_github_checks_verdict`/`_gh_merge`
   machinery lives exclusively in `EngineeringExecutor.finalize`
   (engineering.py:424-517), which the bridge never calls.
2. **Grade path is side-effect-free:** `ratify` stages and diffs only; no
   commit, push, or merge (ratify.py:41-61).
3. **Branch hygiene:** all work happens on `autopilot/airun-<card_id>` in an
   isolated worktree under `<repo>-wt/` (engineering.py:173-197). The base
   branch is never checked out by any code the bridge calls.
4. **Where the PR opens and how the url reaches `links`:** `finalize` calls
   `_commit_and_push` then `_open_pr` (`gh pr create`, engineering.py:397-409,
   base chosen by `_pr_base`). The bridge subclass's `_open_pr` override adds
   `--draft` and stores the url; the dispatcher returns it as `links["pr"]`,
   which `set_state` attaches to the run and the card fold surfaces
   (agent_run.py:653, card_store.py:400-402).
5. **Config assertion:** the bridge refuses repos with `automerge: true` or
   listed in `automerge_repos`, even though the executor could not merge anyway.
6. **Upstream kind gate unchanged:** `gate()` still blocks execute on `change`
   cards (CAB approval) and clamps GTD to draft-only (agent_run.py:477-505); the
   bridge runs only after that gate allowed the run.
7. **No auto-approve / no auto-complete:** the bridge writes no board
   completions (board=None), answers no decisions, and `process_one` always
   lands the card in review (agent_run.py:653-654).

---

## 5. Blast radius: do-not-break analysis

Shared state the autocode engine touches, and how the bridge avoids each:

| Shared thing | Engine behavior | Bridge posture |
|---|---|---|
| `EXECUTORS` registry (executor.py:23-34) | `register()` is last-write-wins global; but `build_executors` already copies `dict(EXECUTORS)` per run (orchestrator.py:727-741) | bridge **never calls `register()`**; it constructs its executor instance directly. Zero registry mutation. |
| coord Board (`agents/*.json`, `tasks/*.json`) | claim/score/complete via `_BOARD_LOCK` | `claim` overridden to journal-only; `board=None` makes any accidental board call an immediate loud AttributeError instead of a silent write. `score_task` is EngineeringExecutor.run-only, never reached. |
| autopilot run journal dir (`coordination/autopilot/runs/`) | one JSON file per run_id (journal.py:22-30) | bridge uses run_id `airun-<card_id>-<stamp>`: its own file, never a resume-collision with autopilot's timestamp run ids. |
| joule P&L / wallet (joules.settle) | settled in `_settle_economics` on pass | overridden to no-op in P1: no mint, no spend records from ad-hoc runs. |
| GTD decisions (digest.queue_decision writes `source="autopilot"` items, digest.py:113-131) | finalize queues a merge decision | bridge passes a **digest shim** that captures the text into the returned `activity` instead. The kanban card in review IS the review surface; no autopilot-branded GTD item, so the autopilot decision resolver never sees bridge work. |
| git branches/worktrees (`autopilot/<ref>`, `<repo>-wt/<ref>`) | per-ref, self-healing | ref prefix `airun-` guarantees no collision with a real autopilot build of the same card. |
| `_GIT_LOCK` / `_BOARD_LOCK` (engineering.py:36-37) | process-local threading locks | the runner processes runs sequentially (`run_once`, agent_run.py:692-697), so intra-process is trivially safe. Cross-process git races with a live autopilot on the same repo are possible in principle; autopilot is disabled, and the P2 canary keeps it that way. Noted as risk R5. |
| skcode ratify endpoint | calls `ratify()` with a helper-only `EngineeringExecutor(config=None, board=None, journal=None)` (ratify.py:48) | bridge calls the same pure function the same way; `ratify` reads no globals. Untouched. |
| `Config` | `Config.load()` returns a fresh instance every call (config.py:71-107); no module singleton | bridge loads per dispatch; never writes the yaml. |
| Card claim/double-run | Board claim is the engine's cross-runtime guard | agent_run has its own claim/lease; folding `agent_run_claim` moves the run to `running` so `list_queued` never re-picks it (card_store.py:376-382, agent_run.py:217-218). Residual gap: a human could ALSO hand the same card to autopilot; the `airun-` namespace means that produces two branches/PRs, duplicated work but no corruption. Risk R4. |

Net: the bridge imports the engine as a library, constructs private instances,
and mutates nothing module-global. The disabled autopilot's behavior is
byte-identical before and after wiring.

---

## 6. Where the bridge lives + registration

**The bridge lives in skharness:** `skharness/src/skharness/autocode/agentrun_bridge.py`.

Rationale: the dependency arrow already points this way. skharness imports
skcapstone in several places (engineering.py:607, fleet_dispatch.py:57,
joules.py:164); skcapstone does NOT depend on skharness in pyproject and only
ever lazy-imports it behind fallbacks (fleet/capacity.py:79). Putting the bridge
in skcapstone would invert that and make skcapstone hard-import the engine's
internals. In skharness, the bridge sits next to the code whose invariants it
narrows (direct.py, ratify.py), so a change to those files breaks the bridge's
tests in the same repo and same CI run.

Module contents:

- `AgentRunDirectExecutor(DirectExecutor)` (section 1)
- `build_execute_dispatcher() -> Callable[[dict], dict] | None`: checks static
  prerequisites (section 3 rows 4-8's static subset), returns the dispatcher
  closure or None. Returning None is a first-class outcome, not an error.
- the digest shim and refusal helpers.

**Registration (skcapstone side), one small helper in agent_run.py:**

```python
def _maybe_wire_execute_bridge() -> None:
    """Wire the skharness execute bridge iff explicitly enabled and buildable.
    Inert by default; every failure path leaves execute fail-closed (R1)."""
    if os.environ.get("SKAI_EXECUTE_BRIDGE") != "1":
        return
    if execute_dispatch_available():
        return
    try:
        from skharness.autocode.agentrun_bridge import build_execute_dispatcher
    except ImportError:
        logger.info("SKAI_EXECUTE_BRIDGE=1 but skharness is not installed; "
                    "execute stays fail-closed (R1)")
        return
    fn = build_execute_dispatcher()
    if fn is None:
        logger.info("execute bridge prerequisites missing; execute stays fail-closed")
        return
    set_execute_dispatcher(fn)
```

called once at the top of `run_ai_runner_job()` (agent_run.py:764). Inertness is
double-keyed and ANDed:

- `SKAI_EXECUTE_BRIDGE` unset (default): the import never happens, `_execute_dispatcher`
  stays None, R1 fail-closed behavior is bit-identical to today.
- `SKAI_RUNNER_LIVE` unset: even a wired dispatcher is unreachable; `process_one`
  records a plan (agent_run.py:605).
- prerequisites missing: factory returns None, seam stays fail-closed with a log line.

No import of skharness at module import time, no wiring at import time, nothing
in `process_one` changes. The only skcapstone diff is this helper plus its call.

---

## 7. Test strategy (prove it with the engine mocked)

A live sandbox run is unverifiable here (docker image, oauth token, gh auth).
The wiring and every fail-closed edge are provable with mocks:

**skcapstone (tests/test_agent_run_bridge_wiring.py):**
1. `SKAI_EXECUTE_BRIDGE` unset -> `_maybe_wire_execute_bridge()` is a no-op,
   `execute_dispatch_available()` is False, and an execute run through
   `process_one` with `SKAI_RUNNER_LIVE=1` yields the existing R1 gated result
   (assert the exact reason string; test_agent_run_r1.py already pins this,
   extend rather than duplicate).
2. Flag set but import fails (monkeypatch `sys.modules["skharness..."]` out or
   patch the import to raise): seam stays fail-closed, log emitted.
3. Flag set, factory returns None: seam stays fail-closed.
4. Flag set, factory returns a fake dispatcher: `process_one` on an execute run
   calls it with the exact context dict, folds its `activity`, sets state
   NEEDS_REVIEW, attaches `links["pr"]`, and moves the card to review.
5. Teardown ALWAYS calls `set_execute_dispatcher(None)`: the seam is a module
   global; a leaked dispatcher would poison unrelated tests (autouse fixture).

**skharness (tests/autocode/test_agentrun_bridge.py):**
6. Fake harness (implements `run_task` writing a file into the worktree and
   returning `HarnessResult(ok=True, ...)`, and `grade` returning a canned
   `GateResult`); real tmp git repo with a `main` branch as RepoSpec.path;
   monkeypatch `_commit_and_push`/`_open_pr` (or provide a repo with a file://
   origin and a fake `gh`). Assert: worktree under `<repo>-wt/airun-...`,
   branch `autopilot/airun-...`, base branch untouched (rev-parse before ==
   after), draft flag in the gh argv, links == {"pr","branch"} only.
7. Refusal matrix, one test per rule: card missing, no repo label, two repo
   labels, unknown repo, empty repo_map, live_execution false, automerge repo,
   min_quality gated floor, empty diff (fake harness that writes nothing:
   assert worktree pruned, no push, refusal summary).
8. Isolation pins: `EXECUTORS` dict identical before/after building and running
   the bridge; no file created under `coordination/tasks/`; no GTD write
   (monkeypatch `skos.gtd_ingest` to raise if touched); journal file name
   starts with `airun-`.
9. Guard pin: passing ratify's (mode="gated") result into
   `AgentRunDirectExecutor.finalize` raises (proves the inherited G1/G2 guard
   still protects the bridge path).
10. Conformance: the bridge grade path calls the real `ratify` (spy), which
    itself pins `twin_gate_passed`; do not re-test the predicate, just that the
    bridge uses it and reports `rr.score`/`rr.passed` in the summary.

**What P2 (live) verifies that mocks cannot:** the sandbox image + proxy
actually run, the oauth token survives a full round, gh opens a real draft PR,
and end-to-end latency vs the runner cadence.

---

## 8. Phased plan

**P1: bridge, fail-closed, fully mock-tested (no live runs)**
- `skharness/autocode/agentrun_bridge.py` (executor subclass, factory, shim)
  + the skharness test file (items 6-10).
- skcapstone: `_maybe_wire_execute_bridge()` + call in `run_ai_runner_job`
  + wiring tests (items 1-5). No behavior change with the env flag unset.
- Update `docs/runbooks/ai-runner-go-live.md` step 1 to name the module, the
  `SKAI_EXECUTE_BRIDGE` flag, and the section-3 prerequisite table.
- Exit: both suites green; a queued execute run on a box without the flag
  behaves exactly as today (gated, plan recorded).

**P2: live canary (Chef-hand, per runbook)**
- Prereqs 4-11 satisfied on noroc2027 only; autopilot stays `enabled: false`.
- One low-risk card with a single `repo:` label on a sandbox-ready repo,
  `SKAI_EXECUTE_BRIDGE=1 SKAI_RUNNER_LIVE=1`, watch it produce a draft PR,
  review the PR AND the recorded grade, then widen card-by-card.
- Add during P2, informed by the canary: per-run cost line in activity is P1;
  a daily budget ledger (reuse `CapLedger`) and a lease-extension or explicit
  dispatch timeout below the 1800 s sandbox cap are P2 hardening.

**P3 (optional, separate design): gated-floor repos**
- Support `min_quality: gated` targets via an `EngineeringExecutor` variant
  whose board writes are shimmed, or by mirroring the card into a real coord
  task first. Out of scope now; P1 refuses those repos loudly.

---

## 9. Top risks

- **R1 residual: the seam is a process-global.** Any code in the runner process
  could call `set_execute_dispatcher` with something unsafe. Mitigation: the
  wiring helper is the only sanctioned caller; `claude_dispatcher` refuses
  execute even if mis-wired (agent_run.py:716-725); test 5 keeps tests hygienic.
- **R2 flag coupling: `live_execution: true` also arms the manual
  `skos autopilot run --canary/--live` path** (section 3 note). Mitigation:
  `enabled: false` + kill switch + checklist callout; the flag change is itself
  a Chef-hand step.
- **R3 grade quality on ad-hoc instructions:** acceptance defaults to the
  instruction text when the card has none, so `ratify`'s score is advisory-grade
  at best. Mitigation: the PR is a draft and the summary labels the twin-gate
  status honestly; never treat `rr.passed` as permission to do anything beyond
  opening the draft.
- **R4 duplicate work, not corruption:** the same card handed to both the bridge
  and (a future re-enabled) autopilot produces two branches (`airun-` vs bare
  ref) and two PRs. Acceptable for a canary; a cross-engine "in flight" check on
  `card.meta` is a P3 nicety.
- **R5 cross-process git races:** `_GIT_LOCK` is per-process; concurrent bridge
  + autopilot worktree ops on one repo could race on `.git` locks. Today
  autopilot is disabled and the runner is sequential; revisit before ever
  running both live on one node.
- **R6 long builds vs the 900 s agent_run lease:** the lease can expire mid-run.
  Nothing re-queues a `running` card (fold keeps it out of `list_queued`), so
  there is no double-run today, but a crashed bridge strands the card in
  `running`. P2 hardening: heartbeat/lease-renew or a stale-running sweep.
- **R7 swallowed `gh pr create` failure:** branch pushed, no PR, stdout empty
  (engineering.py:404-408). The bridge's empty-url check (section 1) turns this
  into an explicit refusal-with-branch instead of a silent "done".

---

## Appendix: signatures relied on (pin these in review)

- `set_execute_dispatcher(fn)` / `execute_dispatch_available()` / `process_one(home, item, worker, dispatcher=None)`: agent_run.py:528-697
- dispatcher contract `{"summary", "activity": [{"atype","text"}], "links": {...}}`: agent_run.py:648-653; folded link keys pr/commit/branch/transcript: skcoord card_store.py:400
- `WorkItem(kind, ref, source, repo, payload)` / `RepoSpec(...)` / `GateResult(score, passed, notes, artifact, mode="gated")`: types.py:49-124
- `DirectExecutor.run(item, harness) -> GateResult(mode="direct")`, `_merge` raises, `finalize` refuses gated: direct.py:45-96
- `EngineeringExecutor.__init__(config, board, journal, digest=None, *, agent_name=None)`: engineering.py:76
- `ratify(repo, worktree, acceptance, harness) -> GateResult` (grade-only): ratify.py:34-61
- `twin_gate_passed(gr, ci_status, cov, repo)` single pinned predicate: engineering.py:59-70
- `Config.load()` (missing file -> disabled default, empty repo_map): config.py:71-107
- `build_harness(config, name=None)` + HARNESSES registry: autocode/harness.py:84-103 (distinct from skharness/harness.py `build_harness(name, config)`)
- `Sandbox.spawn` raises `HarnessUnavailable` unless `live_execution`: sandbox.py:105-110
- `Board.claim_task` raises ValueError on unknown/foreign task: skcoord coordination.py:722-748; `_write_task_raw` raises FileNotFoundError on unknown id: skcoord coordination.py:326-346
- skcode dispatch surface spawns interactive sessions, not graded builds: daemon.py:399-466, serve.py:154-199
