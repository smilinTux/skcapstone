# Projection Read Inventory (card A5.1)

Status: inventory + verdicts. No behaviour changes in this document's PR.
Card: `ebc927c3` (A5.1), parent `b17ebd32`. Follow-ups: A5.2 applies the fixes and
markers, A5.3 adds the lint.

## Why this exists

A projection disagreed with its source and nearly caused the corruption it appeared
to report. A view showed 37 destroyed records. Acting on that view would have
written 37 fabricated completion events into an unversioned store that syncs to four
machines.

The rule this epic adopts:

> A decision-making read goes to the event store, or it carries a written
> justification for using the view.

This document lists every place in `skcapstone` (and the `skcoord` package it now
delegates to) that reads or writes the agent/board projection, with a `fix` or
`justify` verdict per site.

## What the two stores actually are

- **Projection**: `~/.skcapstone/coordination/agents/<agent>.json`. A mutable,
  whole-file-rewritten `AgentFile` carrying `claimed_tasks`, `completed_tasks`,
  `current_task`, `last_seen`, `host`.
- **Event store**: per-card append-only logs, folded by `CardStore.fold()`. The true
  input set is `cards/<id>/events/*.jsonl` union `coordination/card_events/*.jsonl`
  union `coordination/archive/<host>.jsonl`.

The asymmetry that decides most verdicts: the event store is per-writer append-only
and conflict-free. The projection is a mutable file rewritten in full by
`Board.save_agent()`, so a concurrent write or a Syncthing conflict silently drops
claims and completions.

## Correction to the parent card's premise

The parent card `b17ebd32` states that `agents/<agent>.json` reads 0 while
`cards/<id>/events/` holds them all. **That direction is wrong**, and this document
does not repeat it. Measured on 2026-08-16:

| Path | Contents |
| --- | --- |
| `coordination/cards/` | 11 card directories, 11 event files, every one `writer: archive`, `action: archive` |
| `coordination/tasks/` | 4,877 card files (this is the live board) |
| `coordination/card_events/noroc2027.jsonl` | 2,072 events over 1,119 distinct cards |
| `coordination/agents/` | 120 files (114 live + 6 Syncthing conflict copies) |

So `cards/` is nearly empty, not authoritative-and-full. The live event overlay is
`card_events/`, and `tasks/*.json` carries no status key at all: card status exists
only as events.

A second correction, to this card's own starting-point list: **`state_diff.py:398`
does not read the agent projection.** It reads `done_ids` out of a snapshot built by
`_snapshot_tasks()` (`state_diff.py:289-297`), which calls `board.get_task_views()`.
It is a projection read only indirectly, through the fallback described below.

A third correction, to this card's description of `skcoord`: `card_store.py:915` is
not inside `reconcile_from_legacy`. It is inside **`export_to_legacy()`**
(`card_store.py:809`). `reconcile_from_legacy()` is a different function
(`card_store.py:740`) that flows the opposite direction, legacy into the store, and
does not touch agent files. The two are easy to conflate and only one writes the
projection.

## The single most important structural finding

`Board.get_task_views()` (`skcoord/coordination.py:739`) is **not** unconditionally
event-sourced. It falls back to `_legacy_task_views()`, which builds its entire
notion of status from `ag.completed_tasks` / `ag.claimed_tasks` / `ag.current_task`,
in three cases:

1. `SKCOORD_CARD_STORE` disabled or set to `dual`.
2. The store returns zero views while legacy task files exist (the "catastrophe
   guard", `coordination.py:762-772`).
3. **Any exception at all**, caught bare and logged at warning level
   (`coordination.py:770-771`).

Every site in this repo that reads `v.status == DONE` is therefore a *conditional*
projection read. Under the fallback, a broken event-store read and a healthy one
produce the same shaped output and differ only by a warning line in a log nobody
tails. That is the failure mode this epic exists to remove, so the fallback itself is
listed as a site below.

## Measured damage (evidence, not assertion)

All figures produced on 2026-08-16 on this box against the live
`~/.skcapstone/coordination/` tree.

### 1. A bulk rebuild stamped the rebuilder's identity onto every agent

`last_seen`, grouped by whole second:

```
     74  2026-08-16T07:59:34
     32  2026-08-16T07:59:33
      2  2026-07-03T09:07:28
      2  2026-07-03T08:59:33
      (8 further singletons)
```

106 of 120 agent files share a two-second window. `host`:

```
    100  cbrd21-laptop12thgenintelcore
     18  noroc2027
      1  xai-cloud
      1  norap2027
```

The card's stated timestamp (`2026-08-16T07:59:33`) is the smaller of the two
buckets; the actual bulk is `07:59:34`. The conclusion is unchanged and slightly
stronger: **`host` identifies whoever last rebuilt the projection, not whoever did
the work**, and `last_seen` is a rebuild stamp, not a heartbeat.

The mechanism is exact and traceable. `export_to_legacy()` iterates
`owners = claimed | completed | in_progress | existing_agents`
(`card_store.py:909`) and calls `board.save_agent(af)` for every one
(`card_store.py:921`). `save_agent()` unconditionally overwrites
`agent.last_seen = now` (`coordination.py:295`) and `AgentFile.host` defaults to
`socket.gethostname()` (`coordination.py:143`). One export therefore restamps every
agent file with the exporting host's clock and name.

There is a backup on disk named for the incident:
`agents.post-export-damage-20260816T040103.tar.gz`.

### 2. Syncthing conflict copies disagree with the live files

Six conflict copies exist. Every one claims completions the live file has lost:

| Live file | `completed_tasks` live | in conflict copy |
| --- | --- | --- |
| `agents/autopilot.json` | 0 | 4 |
| `agents/opus-swarm.json` | 0 | 5 |
| `agents/opus-swarm.json` | 0 | 11 |
| `agents/opus-swarm.json` | 0 | 22 |
| `agents/opus-swarm.json` | 0 | 5 |
| `agents/opus-swarm.json` | 0 | 11 |

The divergence is one-directional: the live file has *fewer* completions in all six
cases, never more. That is the signature of whole-file overwrite, and it confirms
`save_agent()`'s rewrite is lossy under concurrency.

### 3. The projection's completion claims are mostly unbacked

Across the 114 live agent files, 90 report zero completions and 24 report some.
One synthetic agent, `legacy-export`, holds 1,621 of them. That name is
`_EXPORT_OWNER` (`card_store.py:807`), the fallback owner `export_to_legacy()`
assigns to any card with no owner.

Reconciling the 1,681 claimed completions against every store on disk:

| Backed by | Count |
| --- | --- |
| `archived_by: archive-done` in the archive manifest | 1,398 |
| either the archive manifest or a `move -> done` event | 1,495 |
| **neither store** | **186** |
| `move -> done` events total (all shards) | 101 |

Excluding `legacy-export`, the real agents claim 62 completions of which 36 have no
`move -> done` event.

Note the third store this exposes. `archive/*.jsonl` is not a lifecycle event log; it
is an archive manifest with the schema `{id, archived_at, archived_by}`. 1,418 rows
carry `archived_by: archive-done`, meaning those cards were archived *because* they
were done, and their `move -> done` events were pruned at archival. **Completion
history for most of the board's history exists only in the archive manifest.**

This materially changes A5.2's scope: a naive "convert to an event-store read" would
report 101 completions where the defensible figure is roughly 1,495, an under-report
of about 93 percent. Any site converted to the event store must union the archive
manifest, or it will regress.

### 4. The two stores disagree right now, on this box, with the flag on

`SKCOORD_CARD_STORE=1` is exported in `~/.bashrc:201`, and
`card_store_read_enabled()` returns `True`, so the store path is live. Running both
read paths against the same tree in the same process:

```
task_views_from_store  -> 1358 views, 230 done
_legacy_task_views     -> 1358 views, 229 done
get_task_views         -> 1358   (served by STORE)
```

Same total, different truth. **33 cards have a different status depending on which
store you ask**, including one `done` disagreement:

- `gtd-765c0782fbfd` ("Email Casey the updated invoice for the file server work") is
  `done` in the event store and not done in the projection. Its owner is
  `legacy-export`.
- The other 32 are `review` in the store and `claimed` in the projection.

That second group is not staleness, it is a **structural expressiveness gap**. The
projection has no field that can represent the `review` column; `_legacy_task_views()`
derives status purely from the claimed/current/completed sets, so `review` collapses
to `claimed` on every read. The projection cannot represent the board it projects,
and no amount of rebuilding will fix that.

## The inventory

Verdict key: **fix** = convert to an event-store read in A5.2. **justify** = keep,
and carry a written `PROJECTION-OK` marker.

### skcapstone (this repo)

| # | File:line | Reads | Decision that depends on it | Verdict | Reason |
| --- | --- | --- | --- | --- | --- |
| 1 | `src/skcapstone/changelog.py:98` | `a.completed_tasks` over all agents | Attribution only: who is credited for each completed task. The *set* of completed tasks comes from `views` where `status == DONE` (line 94), and there is a `v.claimed_by` fallback (line 106). | **fix** | Not a completion decision, but the attribution is measurably wrong: `legacy-export` holds 1,621 entries, so the changelog credits a synthetic agent for most of the board's history. Attribute from the event's `writer`/`owner` instead. |
| 2 | `src/skcapstone/mcp_tools/coord_tools.py:150` | `len(a.completed_tasks)` | `coord_status` reports `completed_count` per agent to operators and to agents in-context, who use it to judge workload and progress. | **fix** | This is the exact shape of number that triggered the incident. It currently reports 1,621 for `legacy-export` and 0 for 90 agents that have done work. Derive the count from folded cards. |
| 3 | `src/skcapstone/mcp_tools/coord_tools.py:221` | `agent.completed_tasks` | Response body of `coord_complete`, echoed to the caller. | **justify** | This is a read-back of the write the same call just performed (`board.complete_task()` at line 208), not an independent decision input. Keep, but bound it: for `legacy-export` this serialises 1,621 ids into a tool response. |
| 4 | `src/skcapstone/state_diff.py:398` | `newly_done` from snapshot `done_ids` | `state_diff` reports "Completed N task(s)" between snapshots; drives session summaries and the `state_diff` MCP tool. | **fix** | Indirect. `_snapshot_tasks()` (line 295) calls `get_task_views()`, so it inherits the silent fallback. It should snapshot from the store explicitly so a fallback shows up as an error, not as a plausible smaller number. |
| 5 | `src/skcapstone/warmth_anchor.py:321` + `:326` | `board.load_agents()`, `views` `done` count | Sets `cal.connection` to 8.5 or 7.0 from thresholds at 20 and 5 completed tasks, feeding the emotional-continuity anchor. | **fix** | Threshold decision on a count that both stores disagree about (230 vs 229 live, and far more historically). `agents` is loaded at 321 and, on the code path read here, not used for the threshold, so the load itself is dead weight worth removing. |
| 6 | `src/skcapstone/context_loader.py:104,126` | `a.state.value`, `a.current_task` | Builds the coordination block injected into agent context at session start. | **justify** | `current_task` and `state` are liveness signals whose only home is the projection; the event store has no "what is this agent doing right now" concept. Justified, but the block should be labelled as projection-derived so a model reading it does not treat it as ground truth. |
| 7 | `src/skcapstone/cli/coord.py:38,103` | `board.load_agents()`, `ag.current_task` | Human-facing `skcapstone coord` roster display. | **justify** | Display of live agent state to a human at a terminal, adjacent to counts derived from `views`. No automated decision. |
| 8 | `src/skcapstone/shell.py:320,337` | `board.load_agents()`, `a.current_task`, `a.state` | Interactive shell `coord status` roster. | **justify** | Same as #7. |
| 9 | `src/skcapstone/auction.py:104` | `len(agent_file.claimed_tasks)` | **Bid weighting.** Feeds `AuctionBid.claimed_tasks_count`, which decides which agent wins a task. | **fix** | A load-bearing scheduling decision on a field that Syncthing conflicts demonstrably truncate (evidence 2). An agent whose file lost claims bids as if idle and wins work it cannot take. |
| 10 | `src/skcapstone/team_comms.py:464` | `board.load_agent(name)` then `save_agent()` | Appends a note to the agent's `notes`. | **fix** | Read-modify-write of the **whole** `AgentFile`. If the load races another writer, saving back clobbers `completed_tasks` and `claimed_tasks` as collateral, and refreshes `last_seen`. This is a projection *writer* hazard, listed here because A5.2 should narrow it to a notes-only append. |
| 11 | `src/skcapstone/coordination.py` (whole file) | re-export shim | `sys.modules[__name__] = skcoord.coordination` | **justify** | Not a read. Recorded so the lint in A5.3 does not flag it and so readers know skcapstone has no second implementation. |

### skcoord (reported, not edited by this card)

These are in `/home/cbrd21/clawd/skcapstone-repos/skcoord`. They are listed because
skcapstone's behaviour is defined by them, and because A5.2 cannot fix sites 1-10
without them.

| # | File:line | Reads / writes | Decision | Verdict | Reason |
| --- | --- | --- | --- | --- | --- |
| 12 | `coordination.py:739-773` | fallback from store to `_legacy_task_views()` | Which store answers every `get_task_views()` caller in both repos. | **fix** | The bare `except Exception` at 770-771 converts a broken event-store read into a plausible projection answer with only a warning. This is the root enabler of sites 1, 4, 5. It should fail loudly, or set an explicit flag on the returned views that callers can assert on. |
| 13 | `coordination.py:791-808` | `_legacy_task_views()` builds status from agent files | The entire legacy notion of task status. | **fix** | Cannot represent the `review` column, which is 32 of the 33 live disagreements. Structurally lossy, not merely stale. |
| 14 | `coordination.py:698-699` | union of `ag.completed_tasks` | **Dependency gating.** `unblocked_task_ids()` treats a task as unblocked iff `set(t.dependencies).issubset(completed)`. | **fix** | Highest severity in the inventory. A completion lost to a Syncthing conflict deadlocks every dependent task permanently, and the failure is silent: the task simply never becomes eligible. |
| 15 | `coordination.py:718-727` | `af.last_seen` | **Staleness verdict** in `release_stale_claims()`, which force-releases another agent's claims. | **fix** | `last_seen` is refreshed by every `save_agent()`, including the bulk `export_to_legacy()` pass. Evidence 1 shows 106 files sharing one two-second stamp, so this check currently measures the last rebuild, not agent liveness. Two silent bail-outs return `[]` (agent missing, unparseable timestamp), which look identical to "nothing is stale". |
| 16 | `coordination.py:845` (`archive_done_tasks`) | derived `DONE` via `_legacy_task_views()` | **Irreversible archival.** | **justify** | Deliberate and currently correct. The in-code comment at 843-844 is the justification: a completion recorded only in the legacy agent file must never be missed by a destructive sweep, and the store mirror is best-effort. Keep the projection read while legacy is still written. **This justification inverts the moment legacy stops being written**, and that inversion is a one-way door. It must be re-checked at Phase 4e-retire, not inherited. |
| 17 | `coordination.py:888` (`age_stale_open`) | derived `OPEN` via `_legacy_task_views()` | **Irreversible backlog aging.** | **justify** | Same reasoning and same expiry condition as #16. |
| 18 | `coordination.py:917-931` | derived status via `get_task_views()` | **Claim admission control**: raises if a task is already claimed. | **fix** | Inherits #12's fallback. Under fallback, the 32 `review`-vs-`claimed` disagreements change who is allowed to claim. |
| 19 | `coordination.py:936-938` | `agent.current_task`, `agent.claimed_tasks` | Bump/demote decision on claim; emits a `demote` event when bumping. | **justify** | Operates on the agent's own in-flight state, which only the projection models, and it mirrors the outcome into the store immediately (943-944). |
| 20 | `coordination.py:958-963` | `agent.claimed_tasks` | Successor selection: `current_task = claimed_tasks[0] if claimed_tasks else None`. | **justify** | Same as #19: in-flight state with no event-store equivalent. |
| 21 | `coordination.py:1302-1306` | `ag.claimed_tasks`, `ag.completed_tasks`, `ag.state`, `ag.current_task` | `get_briefing_json()` serialises the raw projection into the agent-facing briefing. | **fix** | This is where the projection escapes into model context as ground truth. An agent handed `completed_tasks` will reason from it and act on it. Highest blast radius after #14. |
| 22 | `coordination.py:1017-1022`, `1241-1243` | `ag.state`, `ag.current_task`, `ag.host` | `generate_board_md()` and `get_briefing_text()` roster lines. | **justify** | Human-readable roster. Note that `ag.host` is misleading per evidence 1 and should be dropped or relabelled rather than justified as-is. |
| 23 | `card_store.py:914-921` (in `export_to_legacy`, defined at 809) | **writes** `claimed_tasks`, `completed_tasks`, `current_task` for every agent | Bulk rebuild of the projection from folded cards. | **writer, fix** | Belongs in this inventory as a *writer*, not a reader. It is the proximate cause of evidence 1 and 3. Three specific defects: (a) it iterates over `existing_agents` too, so an agent absent from the store has `completed_tasks` overwritten with `[]` rather than left alone; (b) ownerless cards are all attributed to the synthetic `legacy-export`, producing the 1,621 figure; (c) `save_agent()` restamps `last_seen` and `host` on all ~120 files, destroying the staleness signal #15 depends on. |
| 24 | `card_store.py:684-685` (`parity_check`) | forces `_forced_legacy_read()` | Parity numbers between the two stores. | **justify** | Reading the projection is the entire point: it is the thing being compared. Justified by definition, and worth a marker so A5.3's lint does not flag the epic's own measuring instrument. |

### Sites I could not fully resolve

- **`tests/test_dashboard_trust.py:37-41`** builds `agents/*.json` with
  `completed_tasks` and asserts on a trust graph. The module it imports,
  `skcapstone.dashboard._trust_graph_dict`, **no longer exists in this repo**; it was
  extracted to `skdashboard` (`skdashboard/src/skdashboard/dashboard.py:666`). The
  real implementation delegates to `capauth.trust.graph.build_trust_graph()`, in a
  third repo, and swallows every exception into an empty graph. I could not determine
  from `skcapstone` alone what decision the trust graph's coord edges drive, or
  whether `completed_tasks` affects the graph at all beyond node presence. **Flagged
  for A5.2**: either the test is orphaned and should be deleted, or `capauth`'s trust
  graph is an unaudited projection consumer and needs its own inventory row.

Nothing else in this repo reads the projection. See the method note below for why
that negative result is trustworthy.

## Recommended marker format for A5.2

```
# PROJECTION-OK: <reason>
```

Placed on the line immediately above the read.

Why this exact token:

- **One token, greppable with no false positives.** `command grep -rn 'PROJECTION-OK'`
  is exact. The string does not otherwise occur in either repo (verified), so A5.3's
  lint keys on something with a zero baseline.
- **Uppercase and hyphenated** matches the existing convention in these files
  (`# Reason:` at `coordination.py:296`, `# noqa: BLE001`), so it reads as a
  machine-checked pragma rather than prose.
- **`# ` comment prefix** means it works unchanged in Python, and identically in YAML
  and shell if the lint later covers config or scripts.
- **The reason is mandatory and free-text after the colon.** The lint should require a
  non-empty reason, because the whole point of the epic is that the justification is
  *written down*, not that the site is merely annotated. A bare `# PROJECTION-OK`
  should fail the lint.
- **It states the verdict, not the mechanism.** A marker like `# LEGACY-READ` would
  describe what the code does; `PROJECTION-OK` records that a human decided it was
  acceptable. The lint enforces a decision, so the token should name the decision.

Suggested lint rule for A5.3: flag any read of `completed_tasks`, `claimed_tasks`,
`current_task`, `last_seen`, or `host` off an `AgentFile`, and any call to
`load_agent`/`load_agents`/`_legacy_task_views`, unless the preceding line matches
`^\s*#\s*PROJECTION-OK:\s*\S`. Sites 16, 17 and 24 additionally warrant an expiry
note in the reason text, since their justifications are valid only until legacy
writes stop.

## Method note

Per the card's requirement, and because it materially changed the results:

- All filesystem greps used **`command grep`**, because `grep` in this shell obeys
  `.gitignore` and silently hides files.
- Every empty result was positive-controlled before being believed. Three empty
  results turned out to be measurement bugs, not findings:
  1. `find cards/ -exec command grep ...` returned nothing, because `command` is a
     shell builtin and does not exist as an executable for `find -exec`. The files did
     contain the pattern. Re-run as `command grep ... $(find ...)`.
  2. `"action": "` matched only 13 of 2,072 lines in `card_events/noroc2027.jsonl`.
     The log contains **two different JSON spacings**; 2,058 lines use `"action":"`
     with no space. Any future tooling over these logs must be spacing-agnostic.
  3. `_trust_graph_dict` returned nothing from `src/`. Positive-controlling with
     `def _home` (2 hits) confirmed the grep worked, and the symbol had genuinely
     moved to another repo.
- The live two-store comparison in evidence 4 was run in-process against the real
  `~/.skcapstone` tree, calling `task_views_from_store()` and `_legacy_task_views()`
  directly so neither could mask the other via the fallback.

## Consequences for A5.2

1. Converting a site to "the event store" is **not** a one-line substitution. The
   event store alone knows 101 completions; the defensible figure is about 1,495. Any
   conversion must union `archive/*.jsonl` (`archived_by: archive-done`) or it
   regresses the number it was meant to fix.
2. Site 12 (the silent fallback) should land **first**. Until it fails loudly, every
   other fix can be silently undone at runtime and still look correct.
3. Sites 16, 17 and 24 are justified today on a condition that expires. Their markers
   must say so.
4. `export_to_legacy()` (site 23) should not run again unguarded. It has already
   zeroed `completed_tasks` for 90 agents and restamped `host` on 100 files.
