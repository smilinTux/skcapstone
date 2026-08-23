# ITIL Persistence Refactor — Conflict-Free Syncthing Replication

**Design doc — implementation-ready. prb-7810b08e / chg-11d0e1c7.**
Target file: `src/skcapstone/itil.py` (+ `service_health.py`, `scheduled_tasks.py`, `coord_federation.py`, one migration script, tests).
Model to emulate: `src/skcapstone/coordination.py` (immutable Task + per-agent claim files + status folded on read; 1673 tasks, near-zero conflicts).

---

## 1. Executive summary

`itil.py` stores every record as a single slug-named JSON (`_write_record`, L288-297) that every writer rewrites in a read-modify-write cycle (`update_incident` L389-448, `update_problem` L520-573, `update_change` L637-679, all ending in `_update_record` L331-337). That file is Syncthing-synced and has **many concurrent writers on many nodes**: each node's `service_health` daemon (`_create_incident_for_down_service` L431-472 / `_auto_resolve_recovered_service` L475-507), the built-in scheduler tasks `service_health_check` / `itil_escalation_check` / `itil_auto_close` (registered with **no node affinity** in `build_scheduler`), the `_evaluate_cab` auto-transition (L730-760), and any agent via MCP/CLI. Three pathologies result, all confirmed on disk:

1. **RMW bloat** — `inc-4071064e-skvector-qdrant-down.json` carries **4892 timeline entries** (4891 identical "still down" notes); ~5226 timeline entries total across 52 incidents.
2. **Random-id duplication** — ids are `inc-{uuid4().hex[:8]}` (L137), so two nodes auto-detecting one outage mint two incidents; `find_open_incident_for_service` (L468-474) dedup is racy across Syncthing lag. Observed: 6× syncthing-down, 6× skchat-daemon-down, 6× skvector-qdrant-down, 3× skgraph-falkordb-down, plus 2× clusters — **20 excess records** across 7 clusters.
3. **Same-file conflict** — one live `.sync-conflict` on `inc-509606aa-skgateway-down`; the slug-in-filename mechanism (`_update_record` unlinks old, writes new on any title change, L335-336) is a latent duplicate source.

The fix copies the coord model, and generalizes the **one already-correct pattern inside this file** — the CAB per-agent vote files (`submit_cab_vote` L690-715, `<change_id>-<agent>.json`, single-writer, folded by `get_cab_votes` L717-728):

- **Immutable core** written once per record (`core.json`), keyed by a **stable id** directory — no slug in any path.
- **Append-only per-writer event log** (`events/<agent>@<node>.jsonl`) — single-writer per file ⇒ disjoint write-sets ⇒ Syncthing has nothing to conflict.
- **State folded deterministically on read** (status, severity, timestamps, resolution, timeline) via the existing `_INCIDENT/_PROBLEM/_CHANGE_TRANSITIONS` tables (L95-121) applied at fold-time.
- **Deterministic dedup ids** for auto-detected incidents so two nodes converge on one `core.json` (create-if-absent via `O_EXCL`).

Public API (`create_*`, `update_*`, `list_*`, `get_status`, `submit_cab_vote`) and the MCP tool / CLI surface stay **byte-identical**; only `ITILManager` internals change. KEDB (`create_kedb_entry` L763-788) is already write-once — it only drops its filename slug. CAB votes stay as-is.

---

## 2. On-disk layout

Root unchanged: `~/.skcapstone/coordination/itil/`. Each mutable record becomes a **directory keyed only by id**:

```
itil/
├── incidents/
│   └── <id>/                         # e.g. inc-3f9a2c17/  (STABLE — never renamed)
│       ├── core.json                 # immutable, write-once (O_CREAT|O_EXCL)
│       └── events/
│           ├── lumina@noroc2027.jsonl        # one file per (agent, node) writer
│           ├── service_health@noroc2027.jsonl
│           ├── service_health@fw41.jsonl
│           └── auto-close@noroc2027.jsonl
├── problems/<id>/{core.json, events/<writer>.jsonl}
├── changes/<id>/{core.json, events/<writer>.jsonl}
├── cab-decisions/<change_id>-<agent>.json    # UNCHANGED (already conflict-free)
├── kedb/<id>.json                            # write-once; slug dropped from filename
├── dedup/incidents/<hash>.json               # LOCAL-ONLY open-incident pointer (see §4)
└── ITIL-BOARD.md                             # regenerated on one pinned node
```

**Stable-`<id>` rule.** The record's directory name is exactly its id, forever. The human title/slug lives **only inside `core.json`** as a `label` field. A title edit is an event, never a rename. This deletes the entire `<id>-oldslug.json` + `<id>-newslug.json` class and makes lookup an exact `incidents/<id>/` `is_dir()` check instead of the nondeterministic first-glob in `_find_record_path` (L312-318).

**Writer-file naming rule (the anti-conflict invariant).** Event files are **always** `events/<agent>@<node>.jsonl`, where `<node>` = `socket.gethostname()` (already `service_health._HOSTNAME`, L42). A bare `<agent>.jsonl` is **forbidden** — that is exactly the heartbeat-v1 collision documented in `~/.skcapstone/.stignore`. Two nodes running the same daemon write different files ⇒ never conflict. Within a node, a `fcntl.flock` on the file guards the append (local, no sync lag). A process only ever appends to its **own** writer file.

**core.json** (immutable, write-once):
```json
{
  "id": "inc-3f9a2c17", "type": "incident",
  "title": "skvector (Qdrant) down", "label": "skvector-qdrant-down",
  "source": "service_health", "affected_services": ["skvector (Qdrant)"],
  "impact": "Service unreachable: ...", "created_by": "service_health",
  "detected_at": "2026-07-13T17:03:01.123456+00:00",
  "severity_at_creation": "sev3", "tags": ["auto-detected","service-health"],
  "dedup_key": "skvector (Qdrant):unreachable"
}
```
Changes' core additionally carries the immutable-at-birth fields `change_type`, `risk`, `rollback_plan`, `test_plan`, `implementer`, `cab_required`, `related_problem_id`. **No** `status`, `timeline`, `*_at`, or current `severity` — all derived.

`.stignore` additions (do **not** sync local-only bookkeeping; **do** sync `core.json` and `events/*.jsonl`):
```
**/itil/**/*.sync-conflict-*
**/itil/dedup
**/itil/_legacy
**/itil/**/migration.state.json
```

---

## 3. Event schema + fold algorithm

### 3.1 Event line (append-only, one JSON object per line, never modified)

```json
{"event_id":"<uuid4hex>", "ts":"2026-07-13T17:03:01.234567+00:00",
 "writer":"service_health", "node":"noroc2027", "seq":42,
 "kind":"status", "to":"resolved", "note":"...", "resolution_summary":null}
```

`seq` is a per-writer monotonic counter (append index within that writer's own file) — it only tie-breaks equal `ts` **from the same writer**. An event encodes a **delta/intent**, never absolute current state.

**kinds:** `created` · `status` (`to`) · `severity` (`to`) · `note` (`note`) · `ack` · `resolution` (`resolution_summary`) · `link_problem` (`id`) · `link_change` (`id`) · `title` (`text`) · `tags` (`add:[…]`) · `gtd_link` (`id`) · `gtd_complete` · `reopen` · `recovery` (`note`).

### 3.2 Fold (pure function, mirrors `get_task_views` L431-473)

```
fold(core, event_files):
  state = { status: initial(type),          # incident:'detected' prob:'identified' chg:'proposed'
            severity: core.severity_at_creation,
            timeline: [], acknowledged_at:None, resolved_at:None, closed_at:None,
            resolution_summary:None, related_problem_id: core.get(...), tags:set(core.tags),
            gtd_item_ids:[] }
  events = [ e for f in sorted(event_files) for e in read_lines(f) ]
  events.sort(key=(e.ts, e.node, e.writer, e.seq))   # TOTAL ORDER identical on every replica
  for e in events:
      timeline.append(render_row(e))                 # timeline IS the ordered log — cannot balloon
      if e.kind == 'status':
          if e.to in TRANSITIONS[type][state.status]:     # existing _*_TRANSITIONS tables, L95-121
              state.status = e.to
              if e.to=='acknowledged' and not acknowledged_at: acknowledged_at = e.ts
              if e.to=='resolved'     and not resolved_at:     resolved_at = e.ts
              if e.to=='closed'       and not closed_at:       closed_at = e.ts
              if e.resolution_summary: state.resolution_summary = e.resolution_summary
          else:
              timeline[-1].conflicted = True              # losing branch: audited, excluded from state
      elif e.kind=='reopen':   apply reopen transition (resolved->investigating) if valid
      elif e.kind=='severity': state.severity = max_sev(state.severity, e.to)   # monotone escalation
      elif e.kind=='resolution': state.resolution_summary = e.resolution_summary
      elif e.kind=='ack' and not acknowledged_at: acknowledged_at = e.ts
      elif e.kind=='link_problem': state.related_problem_id = e.id
      elif e.kind=='tags': state.tags |= set(e.add)
      elif e.kind in ('gtd_link','gtd_complete'): fold gtd_item_ids
      # note/recovery: timeline-only
  return build_model(core, state)   # returns a normal Incident/Problem/Change pydantic object
```

**Determinism / conflict resolution.** The state is a pure function of the eventually-consistent **set** of event files, sorted by keys `(ts, node, writer, seq)` that are all present in the data and identical on every replica once Syncthing converges — a CRDT-style op-log, no locking. Worked cases:

- **Both nodes ack** `detected→acknowledged`: earlier event acks; the later validates against the now-`acknowledged` state, is a no-op (idempotent). No double-ack.
- **Divergent transitions** from `detected` (A→resolved, B→escalated): earlier-in-total-order wins; the later, validated against the new folded status, is invalid ⇒ flagged `conflicted` in the timeline for audit, excluded from state. State stays monotone.
- **Legitimate correction** uses `reopen`, not a fight against the transition table.
- **Severity** takes the **max** (safer for alerting than last-writer); this also reproduces today's escalate-only behavior.

Fold is idempotent: `fold(fold(x)) == fold(x)`.

---

## 4. Deterministic dedup-id for auto-detected incidents

Replace `uuid4` (L137) **only for `source="service_health"`**; manual/MCP/CLI incidents keep random ids (no dedup needed).

```python
def _auto_incident_id(service: str, failure_class: str, day_bucket: str) -> str:
    key = f"{service}|{failure_class}|{day_bucket}"   # e.g. "skvector (Qdrant)|unreachable|2026-07-13"
    return "inc-" + hashlib.blake2b(key.encode(), digest_size=4).hexdigest()
```

- `failure_class` = a coarse normalization of `service_result["error"]` (`"unreachable"` default; today's `error_info`, service_health L443).
- `day_bucket` = `datetime.now(timezone.utc).strftime("%Y-%m-%d")` (window `W≈24h`). Two nodes detecting the same outage the same day compute the **same id** ⇒ the **same** `core.json`.
- **core.json for a deterministic id is fully derived from (id, dedup_key, day)** ⇒ byte-identical across nodes ⇒ an `O_EXCL` create race yields identical bytes, harmless even if it ever surfaces as a `.sync-conflict`.

**Create-if-absent:** open `core.json` with `os.open(path, O_CREAT|O_EXCL|O_WRONLY)`; on `FileExistsError`, skip the create and just append the `created` event to the caller's own writer file (folds idempotently — the first `created` wins). This is race-free and **replaces** the racy `find_open_incident_for_service` pre-check, which stays only as a convenience read.

**Precision pointer (local-only, `dedup/incidents/<blake2b(dedup_key)>.json`):** records `{open_incident_id, opened_at}`. On detection: if the pointer's incident **folds to an open status**, reuse it and emit nothing (matches today's no-note policy, service_health L446-459); only mint a new id once the prior incident folds-closed. The id remains a pure function of `(dedup_key, day_bucket)`, so even a pointer race collapses to one id.

**Trade-off (documented):** an outage spanning a day boundary can mint a second incident, which reads naturally as "still down, new day" while the prior one auto-closes. Same-service re-outage within one day folds into the same incident — usually desired. If finer recurrence tracking is wanted later, add an `episode` counter derived from the count of prior `close` events (deferred, not P1).

---

## 5. `itil.py` refactor — exact functions

Public method signatures and return types are **unchanged**; every method still returns a fully-populated `Incident`/`Problem`/`Change` pydantic object built by the fold.

### 5.1 Delete
- `_update_record` (L331-337) — the slug-rename + RMW source. Gone entirely.

### 5.2 Replace / rewrite
| Function | Lines | Change |
|---|---|---|
| `_write_record` | 288-297 | Replace with `_write_core(dir, record_id, core: dict)` → `os.open(dir/record_id/"core.json", O_CREAT\|O_EXCL\|O_WRONLY)`; on `FileExistsError` return existing path (create-if-absent). No slug in path. |
| `_find_record_path` | 312-318 | Replace with exact `dir/record_id` `is_dir()` check (deterministic). |
| `_load_record` | 320-329 | Replace with `_fold_record(dir, record_id, model)` = load `core.json` + read all `events/*.jsonl`, run §3.2 fold, return model. |
| `_load_records` | 299-310 | Iterate `dir.iterdir()` dirs, `_fold_record` each; keep the skip-and-warn tolerance (like coord `load_tasks` L182-187) so old/new layouts coexist during migration. |

### 5.3 Add
```python
def _writer_id(self, agent: str) -> str:        # "<agent>@<hostname>"
def _append_event(self, dir, record_id, agent, kind, **payload) -> None:
    # flock-guarded append one JSON line to dir/record_id/events/<writer>.jsonl
    # assigns seq = current line count; ensures events/ dir exists
def _fold_record(self, dir, record_id, model) -> Optional[BaseModel]
def _fold_incident/_fold_problem/_fold_change(core, events) -> state   # applies §3.2
```

### 5.4 create_* — write core once, then append `created` (no second write)
- `create_incident` (L341-387): compute id — deterministic (§4) when `source=="service_health"`, else `Incident().id`. `_write_core(...)` (create-if-absent). `_append_event(kind="created", note=f"Incident detected: {title}")`. Keep `_publish_event("itil.incident.created", …)` (L372). GTD: keep `_create_gtd_item_for_incident` (L1035) but record the returned id via `_append_event(kind="gtd_link", id=gtd_id)` — **removes** the second whole-file write at L383-385. Return `_fold_record(...)`.
- `create_problem` (L478-518): same shape; `gtd_link` event instead of the L514-516 rewrite.
- `propose_change` (L584-635): write core with `cab_required` and (immutable) type/risk/plans; append `created` event. **Do not** store status. Standard-change auto-approval and `cab_required` become **pure derivations** at fold-time (`change_type==STANDARD`), not stored fields (removes L618-622 write intent).
- `create_kedb_entry` (L763-788): keep write-once; just call `_write_core` under `kedb/<id>/core.json` (or keep single-file, slug dropped).

### 5.5 update_* — stop loading-mutating-rewriting; append exactly one event each
- `update_incident` (L389-448): replace the whole body with appends — one `status`/`severity`/`note`/`resolution`/`link_problem` event per non-None arg to the caller's writer file, then `return _fold_record(...)`. **Move transition validation into the fold** (the L406-410 raise becomes a fold-time `conflicted` flag; the API no longer raises on a losing concurrent transition — document this behavior change). Keep the `itil.incident.escalated` publish (L433) fired by whoever appends the `severity` event; keep `_complete_gtd_items` on the `resolved` event.
- `update_problem` (L520-573): same; `root_cause`/`workaround` become `note`-carrying or dedicated events folded as last-writer-wins; `create_kedb` path (L559-568) appends a `link_kedb` event after `create_kedb_entry`.
- `update_change` (L637-679): same; keep the `approved`/`deployed` publishes and implementer-GTD (L661-671) fired by the appending writer.

### 5.6 CAB — derive, don't write
- `submit_cab_vote` (L690-715): **unchanged** (already per-agent files).
- `_evaluate_cab` (L730-760): **delete the `update_change` write.** Change status becomes a **pure derivation** folded from the vote files (`get_cab_votes` L717-728) at read time: any `REJECTED` ⇒ rejected; ≥1 `human` `APPROVED` and no rejection ⇒ approved (the exact L740-757 logic, moved into the change fold). This removes the last cross-node writer to the change record.

### 5.7 Readers — fold-on-read
- `list_incidents/list_problems/list_changes` (L450-466, 575-580, 681-686), `find_open_incident_for_service` (L468-474), `get_status` (L809-874), `generate_board_md`/`write_board_md` (L935-1002), `auto_close_resolved` (L878-898), `check_sla_breaches` (L900-931): all already consume folded records — they now read the folded views. `write_board_md` stays on **one pinned node** (like coord's `BOARD.md`).

---

## 6. External-writer changes

### 6.1 `service_health.py`
- `_create_incident_for_down_service` (L431-472): pass `failure_class` through so `create_incident` computes the deterministic id (§4); drop the racy `find_open_incident_for_service` **pre-check** in favor of create-if-absent + the local dedup pointer. `managed_by`/`created_by` stay `"service_health"`; the writer id becomes `service_health@<hostname>` automatically.
- `_auto_resolve_recovered_service` (L475-507): the `update_incident` calls (L488-493, L501-504) become single appends to **this node's own** writer file (`resolution` event for sev4, `recovery` note otherwise). **Delete** the hand-rolled last-3-notes host-tag guard (L497-500) — each host writes only its own file and the fold shows at most one low-volume recovery edge per host; the 4892-entry pathology is structurally impossible.

### 6.2 `scheduled_tasks.py`
- `make_itil_auto_close_task` (L566-583) → `auto_close_resolved` (L878-898): the close becomes an **append** of a `status:closed` event to `auto-close@<host>.jsonl`; concurrent closes fold idempotently (first valid close wins), so it no longer needs single-node pinning — but keeping it pinned (as today via jobs affinity) is harmless.
- `make_itil_escalation_task` (L586-607) → `check_sla_breaches`: already a **read + publish only**, no file write. Leave it, or (optional) node-gate to cut duplicate SLA-breach pubsub noise across nodes.
- `build_scheduler`: no structural change required (all writes are now appends), though gating `service_health_check`/`itil_auto_close` to one node via the existing `scheduler_jobs` host-alias mechanism remains available as belt-and-suspenders.

### 6.3 `coord_federation.py`
Extend `CoordFederationWatcher` (or a sibling) to also watch `coordination/itil/` and apply the existing `_CONFLICT_RE` mtime-newer-wins reconciliation (L197-244) as a **backstop** for the rare `core.json` collision. With globally-unique per-writer append files, true `.sync-conflict` on event files cannot occur; this only cleans up residual artifacts and announces `itil.*` sync events.

### 6.4 MCP / CLI — no change
`mcp_tools/itil_tools.py` and `cli/itil.py` are thin pass-throughs; each already passes an `agent`/`managed_by` string that is exactly the per-writer key the event model needs. No tool-schema or CLI-flag change.

---

## 7. Migration algorithm (idempotent, backed-up, lossless)

One-shot script `scripts/itil_migrate_events.py`, run **once on `noroc2027`** (single node) to avoid races. Folds **both** `~/.skcapstone/coordination/itil/` **and** `~/.skcapstone/coordination.backup-pre-cleanup/itil/` (an earlier snapshot with ids not all present in main).

1. **Backup:** `tar --zstd` the whole `itil/` tree → `itil.pre-refactor-<ts>.tar.zst`. Move legacy files to `itil/_legacy/<type>/` rather than `unlink`.
2. **Per legacy `<id>-<slug>.json`:** parse; write `<id>/core.json` from the immutable subset **if absent**; explode `timeline[]` into `<id>/events/<agent>.migrated.jsonl` — map `created`→`created`, `status:X->Y`→`status(to=Y)`, `severity:A->B`→`severity(to=B)`, `note`→`note`. Synthesize terminal `ack`/`resolution`/`status:closed` events from top-level `acknowledged_at`/`resolved_at`/`closed_at`/`status` when the timeline lacks them. `seq` = source index (preserves equal-`ts` order). The `.migrated` file is **deterministically regenerated** from the `_legacy` source on re-run (overwritten, never appended) ⇒ replay-idempotent.
3. **Sync-conflict fold:** for `inc-509606aa …sync-conflict-*.json`, union-dedup its timeline into the base record's events by `(ts,agent,action,note)` — recovers all 4 distinct entries — then retire the conflict file to `_legacy/`.
4. **Semantic merge (the 7 clusters, 20 excess):** group by `(affected_services, normalized_title)`; canonical id = `min(detected_at)` then `min(id)` (deterministic ⇒ every node converges). Fold each duplicate's events under the canonical id as `events/<agent>.from-<origid>.migrated.jsonl` (namespaced, collision-free), append a `note` event `merged_from:[…]`, and drop a redirect stub `incidents/<dupid>/redirect.json → canonical` so old-id lookups resolve. Result: incidents **52 → 32**.
5. **Re-key auto-detected records** to their deterministic ids (§4) using `detected_at`'s day bucket.
6. **State file:** `migration.state.json` (local-only, ignored) records completed ids + schema version; re-runs skip done ids unless `--force`.
7. **Verify then retire:** run §8(d) lossless check; only then leave `_legacy/` aside (ignored by `.stignore`).

---

## 8. Test plan

`~/.skenv/bin/python -m pytest tests/ -v`; ≥3 cases/feature (happy/edge/failure), per project convention.

- **(a) Concurrent-writer no-conflict:** N processes (simulated nodes) each append to their own `events/<w>@<node>.jsonl` for one incident concurrently → assert **zero** `.sync-conflict` artifacts and folded state contains **all** events. Two nodes creating the same deterministic-id incident simultaneously → one dir, **byte-identical** `core.json`, union of events.
- **(b) Fold-correctness:** crafted interleaved-ts per-writer logs → folded status == expected transition-table result; ordering stable under `(ts,node,writer,seq)`; `fold(fold(x)) == fold(x)`; a losing concurrent transition is flagged `conflicted` and excluded from state but kept in timeline.
- **(c) Dedup-convergence:** feed the 7 real clusters → every node computes the identical canonical/deterministic id; merged record holds the **union** of sub-incident timelines; count 52 → 32.
- **(d) Lossless migration:** for **every** legacy record (main + `coordination.backup-pre-cleanup` + the sync-conflict), assert each legacy timeline entry keyed `(ts,agent,action,note)` appears in the folded new state (superset); final status preserved (or most-advanced among merged dupes per the transition table); all 4891 `inc-4071064e` "still down" notes represented; re-run changes **zero bytes** (checksum no-op).
- **(e) Filename-stability:** create → append a `title` event → assert the directory name is unchanged, no `<id>-oldslug`/`<id>-newslug` files exist, and the folded `title` reflects the edit.
- **(f) CAB derivation:** votes-only (no `update_change` write) → folded change status matches the old `_evaluate_cab` outcome for reject / human-approve / pending.

---

## 9. Ordered execution checklist + risks

1. **Add fold primitives** (`_writer_id`, `_append_event`, `_fold_incident/problem/change`, `_write_core`, `_fold_record`) alongside the existing methods; unit-test folds in isolation (§8b). *Risk: fold/transition edge-cases — mitigate with the crafted-log tests before any writer flips.*
2. **Switch readers** (`_load_record(s)`, `list_*`, `get_status`, board) to fold-on-read while writers still RMW (dual-read tolerant). *Risk: mixed old/new on disk — keep skip-and-warn (coord L182).* 
3. **Flip `create_*`** to core-once + `created`/`gtd_link` events (removes the double-write). Add deterministic id for `service_health`. *Risk: `O_EXCL` race semantics — covered by §8a.*
4. **Flip `update_*`** to single-append; move transition validation into the fold; **delete `_update_record`**. *Risk: API no longer raises on losing transitions — document; callers (MCP/CLI) already ignore return-value details.*
5. **Derive CAB + standard-change status;** delete the `_evaluate_cab` write. *Risk: human-approval rule must match L740-757 exactly — §8f pins it.*
6. **Update `service_health`** (deterministic id, drop pre-check + note-guard) and **`scheduled_tasks`** (append close). *Risk: writer-id must be node-unique — assert `@<hostname>` in tests.*
7. **Extend `coord_federation`** to watch `itil/` as mtime backstop; add `.stignore` rules. *Risk: don't sync `dedup/`, `_legacy/`, `migration.state.json`.*
8. **Run migration** on `noroc2027`, verify §8d lossless, commit the new tree. *Risk: run on a single node only; `coordination.backup-pre-cleanup` ids may not exist in main — union both.*
9. **Deploy fleet-wide**, watch `.sync-conflict` count (expect → 0) and timeline growth (expect bounded). *Risk: a node on the old code still RMWs — roll all nodes together, or gate old writers off first.*

**Cross-cutting risks:** (i) clock skew across nodes perturbs `ts` ordering — acceptable, `(ts,node,writer,seq)` still totally orders and converges; escalation takes max-severity so skew can't de-escalate. (ii) A genuinely new outage inside one day folds into the existing incident (§4 trade-off) — documented, generally desired; the `episode` extension is the escape hatch if it bites. (iii) Board/auto-close stay pinned to one node like coord's `BOARD.md`/autopilot to avoid regenerated-artifact churn.