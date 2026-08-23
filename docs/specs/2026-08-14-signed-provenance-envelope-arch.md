# SKWorld Signed Provenance Envelope (SPE)

**Status:** proposed
**Date:** 2026-08-14
**Author:** Fable (claude-fable-5)
**Extends:** `2026-08-13-unified-consent-plane-arch.md`, `2026-08-13-change-management-cab-ai-arch.md`, `sk-standards/standards/PROVENANCE_AND_MUTATION_STANDARD.md`

---

## 1. What Chef asked for

> "there should always be a good provenance trail of identity ownership so we can map back quickly, that's the whole point of our keys"

> "it's not to name n shame but to empower"

> "we run fast, fast, break, iterate"

The design shape approved verbatim: **one envelope, defined once in
sk-standards, adopted by every store.** Fast requires reversible. Reversible
requires attributable. Undo is what makes "run fast and break things"
survivable. Provenance is the empowerment mechanism (an agent that can map any
mutation back to an identity, a session, and a prior state can self-correct in
seconds), not an accountability theater.

## 2. The two incidents (2026-08-13)

**Incident A, GTD.** A sibling agent session was told to close out all GTD
items for a completed errand. It ran a topic filter of the shape
`skcapstone gtd inbox | grep -iE "<topic>|<ambiguous-token>"`, and one
ambiguous token matched BOTH the intended errand item AND an unrelated item
in a completely different domain, where the same word was a proper noun.
It closed both, 541ms apart. It self-corrected 4 seconds later and could not
undo: `gtd done` is a one-way door. Recovery required hand-reading
`archive.json` and recapturing under a NEW id, breaking the id chain. The item
survived only by luck: had it carried a `source_ref`, the archive-side dedupe
would have swallowed the recapture while the archive-blind lookup could never
find the original (section 4.3).

**Incident B, skoperator.** The operator seat crash-looped 16 times in one
day: `ValueError: unknown service object 'skgateway'` at
`skcapstone/src/skcapstone/operator_seat/fleet_adapter.py:172-173`. Three root
causes: (a) the live fleet tree had no `objects/service/` at all (manifests
authored at `deploy/fleet-objects/bulletproof/` but `skfleet apply` never
run); (b) the LLM proposer picks the APP LABEL as the object name
(`proposer.py:79-83` feeds `c.get('app')` into the prompt; skgateway's real
objects are `upstreams`/`connection-pool` per `skgateway_adapter.py:80-93`)
and `plan.plan_actions` (`plan.py:26-46`) validates the ACTION against the
catalog but never the OBJECT; (c) zero try/except in `loop.py` or
`act_dispatch.py`, so the raise at `loop.py:123` aborted the whole pass,
skipping `decisions.park(...)`: escalations the human was meant to rule on
were SILENTLY NEVER WRITTEN, plus no report, no brief publish, no notify.

### 2.1 Shared root causes

Two different subsystems, one failure shape. Every row is a hole SPE closes:

| Hole | GTD incident | skoperator incident |
|---|---|---|
| **Loose identifier matching** | one ambiguous substring matched two unrelated domains | LLM guesses the app label as the object name (`proposer.py:79-83`) |
| **No target validation before mutating** | `done` accepts any id the grep produced, no per-item confirm | action validated against the catalog, object never (`plan.py:26-46`); guess reaches `fleet_act` raw |
| **No blast-radius isolation** | one query fanned into two destructive calls | one bad proposal aborted the entire pass (`loop.py:123`), taking parks, report, brief, and notify down with it |
| **No attribution** | GTD carries zero actor fields; nothing says which session closed what | `operatorActions` hardcodes `"by": "atlas"` (`fleet_adapter.py:178`); `writer.signature` is `null` on disk |
| **No undo** | `gtd done` is one-way; recovery broke the id chain | parked escalations silently lost, nothing to replay them from |

## 3. The finding that reframes the work

**This epic is NOT "invent provenance."** The three stores already do the same
job at three maturity levels. The work is: bring GTD up to the pattern
CardStore already proves, and populate the signature slot the fleet store
already declares.

| Capability | coord CardStore | fleet object store | unified GTD |
|---|---|---|---|
| Append-only event log | YES: per-writer JSONL, flock, per-writer seq (`skcoord/src/skcoord/card_store.py:225-249`) | PARTIAL: `operatorActions` appended within the spec (`fleet_adapter.py:175-186`); spec versioned by generation | **none** |
| Write-once creation | YES: `O_CREAT\|O_EXCL` core (`card_store.py:204-223`) | n/a (declared state) | none |
| State = pure fold | YES: status lives ONLY in the folded stream (`card_store.py:291-413`); fold actions incl. `move, claim, complete, assign, unassign, priority, swimlane, add_label, remove_label, link, note, archive, reopen` | n/a: the spec IS desired state | **none**: `status` is a stored mutable field |
| Inverse verbs | YES: `reopen` already folds (`card_store.py:407`) | catalog knows `reversible` per action; no reversing-event convention | **none** |
| Actor envelope | PARTIAL: every event carries `{event_id, ts, writer, node, seq, action}` (`card_store.py:236-243`), but `writer` is an unresolved agent string | PARTIAL: `Writer(role, node, identity, agent_seat)` (`fleet/store.py:24-42`) serialized as a `writer` block on every spec | **none** |
| Signature | none | SLOT declared + FULL signing machinery shipped, mode off (section 5.1) | none |
| Lock discipline | YES (per-writer flock) | YES (`atomic_write_text`) | PARTIAL: shared `.gtd.lock` on the main paths, two unlocked writers |
| Schema | pydantic `Card` (`skcoord/src/skcoord/card.py:50-72`) | dataclass + conventions | **none** |

`SKCOORD_CARD_STORE` defaults ON: the most mature pattern is already the live
default for coordination. Maturity order: **coord > fleet > GTD**, and GTD is
the store agents mutate most casually.

## 4. Ground truth (verified with file:line)

### 4.1 coord / CardStore (the pattern to copy)

Immutable `cards/<id>/core.json` write-once via `O_CREAT|O_EXCL`
(`card_store.py:204-223`). Append-only `cards/<id>/events/<agent>@<host>.jsonl`
per writer, flock-guarded, per-writer `seq` (`:225-249`). Deterministic fold
in `(ts, writer, seq)` order (`:263-279`, `:291-413`); status is never a
stored field. `reopen` is already a fold action (`:407`). Per-writer files
make concurrent appends on different Syncthing nodes merge instead of
conflict. What it lacks: the writer is a bare agent string (never resolved via
capauth), and there is no signature.

### 4.2 fleet (envelope declared, signature null, machinery parked)

`fleet/store.py` `Writer(role, node, identity, agent_seat)` (`:24-42`) with a
`writer_identity()` helper that already calls
`capauth.resolve_agent_identity()` (`:45-52`). Every spec write stamps a
`writer` block with a `signature` slot (`_writer_block`, `:72-78`) and runs it
through `_maybe_sign` (`:81-91`). Verified live on
`~/.skcapstone/fleet/objects/service/skgateway.json`:

```json
"writer": {"identity": "operator", "node": "node-noroc2027", "role": "operator", "signature": null}
```

`fleet_act` (`operator_seat/fleet_adapter.py:153-189`) appends
`{action, ts, by, changeClass, rationale}` to `spec.operatorActions` with
`"by": "atlas"` hardcoded (`:178`), and its docstring claims the entry is
"SIGNED" (`:156-157`). It is not. That overclaim is itself an sk-standards
violation (the honest-claim gate).

### 4.3 GTD (nothing, plus five structural defects)

- **Zero actor provenance.** grep for `SKAGENT|resolve_agent_identity` over
  `skcapstone/src/skcapstone/mcp_tools/gtd_tools.py`, `cli/gtd.py`, and
  `skos/src/skos/gtd_ingest.py`: zero hits. `source` records the CHANNEL
  (manual/email/itil/cron), never the WHO. `delegate_to` means "waiting on
  them", not "they did it".
- **Destructive two-file move.** `gtd done` (`gtd_tools.py:720-754`) removes
  the item from the source list first (`_remove_item_from_list` saves at
  `:200`, invoked at `:735`), THEN appends to archive (`:743`). Each file
  write is individually atomic, but a crash between the two loses the item.
  The sibling sink in the SAME store deliberately does write-then-delete so a
  crash duplicates instead of losing (`gtd_ingest.py::_upsert_locked`,
  `:345-354`). Two code paths, one store, opposite safety choices.
- **Archive in the dedupe universe, not the lookup universe.**
  `_find_item_across_lists` (`gtd_tools.py:184-191`) iterates `_GTD_LISTS`
  (`:20-26`), which excludes archive; `_seen_refs` (`:149-166`) scans
  `_ALL_STORE_FILES` (`:146`), which includes it. Net: an archived item is
  simultaneously un-findable AND (if it has a `source_ref`) un-recapturable.
  `_DESTINATION_MAP` (`:34-41`) lets `move` PUSH into archive but nothing can
  find or reopen there; no un-archive path exists anywhere. Three modules
  disagree on the store's own file set: `agent_run.py:62-71` and
  `skos/src/skos/adapters/order.py:78-88` include archive in theirs.
- **Two unlocked writers.** `skcapstone gtd capture` (`cli/gtd.py:46-61`)
  inlines `_load_list`/`_make_item`/`_save_list`, bypassing
  `_handle_gtd_capture`: no store lock, no dedupe, unserialized
  read-append-write. skos `mail.py::_save` (`:169-170`) is a plain
  `write_text`, unlocked AND non-atomic, and it archives items itself
  (`:453-460`). The compliant paths share an advisory
  `fcntl.flock(LOCK_EX)` on `.gtd.lock` (`gtd_tools.py:92-113`,
  non-reentrant) with crash-safe atomic writes (`:115-143`).
- **No schema.** No pydantic model, no JSON schema, no dataclass. Two
  divergent item constructors: `gtd_tools.py::_make_item` (`:220-238`) vs
  `gtd_ingest.py::_capture_locked` (`:251-266`, which flattens arbitrary
  `meta` keys onto the item). Enums are bare module constants
  (`gtd_tools.py:28-49`). Three naming axes for the same five files (skos
  `_LIST_FILE` keyed by status vs skcapstone `_GTD_LISTS` keyed by filename
  stem).
- **The store is already incoherent.** Of 5188 archived items, 3867 carry
  `archived_at` but only 1321 carry `completed_at`: the majority was written
  by ad-hoc scripts, not `gtd done`. Nobody can say by whom.

## 5. What already exists. DO NOT REBUILD.

1. **The CardStore event machinery** (section 4.1) including `reopen`. SPE
   adoption there is additive keys on the existing event dict, nothing more.
2. **The complete fleet signing stack.** `fleet/signing.py` already ships:
   canonical bytes over the record with the signature slot blanked
   (`canonical_bytes`, `signing.py:31-42`, replay-safe because generation and
   `updatedAt` are covered), a three-class verifier
   (`verified`/`unsigned`/`invalid`, `verify_payload`, `:45-56`), a
   capauth-backed signer (`capauth_signer`, `:76-102`), a LOCAL trust roster
   that is never read from the synced tree (`load_roster`, `:112-131`), a
   roster verifier (`:134-156`), and the `SKFLEET_SIGNING`
   off/permissive/enforce rollout modes (`:21-28`, default off). P3's fleet
   work is a **key ceremony + suite-id field + mode flip**, not a build.
3. **The identity resolver.** `capauth.resolve_agent_identity()` is the one
   canonical resolver (capauth epic `2b264064`); `fleet/store.py:45-52`
   already wraps it. P2 imports it into the GTD write paths; it invents no
   identity code.
4. **The locked atomic GTD sink.** `_store_lock` + `_atomic_write_json`
   (`gtd_tools.py:92-143`, delegating to `skos.gtd_ingest` when present) and
   the write-then-delete move (`gtd_ingest.py:345-354`). P1 routes the two
   bypass writers through this; it does not write a new sink.
5. **The suite registry.** `skcomms.crypto_suites` + the wire-tag discipline
   (CRYPTO_AGILITY_STANDARD sections 1-3). SPE's `sig.suite_id` uses existing
   registry ids (`ed25519-v1`, `mldsa65-ed25519-v2`); no new crypto.
6. **Per-proposal isolation in the operator seat. FIXED 2026-08-13, do not
   re-plan.** try/except around `apply_fn` in `operator_seat/loop.py`, outcome
   becomes `failed: <err>`, 3 new tests in
   `tests/operator_seat/test_loop.py`; all 252 operator_seat tests pass. All
   26 bulletproof fleet objects applied (14 services, 12 cronjobs). Confirmed
   `--execute` WITHOUT `--honor` is annotation-only
   (`operator_seat/cli.py:166-179`): it writes `operatorActions` onto the
   spec and does not physically actuate; `--honor` (CR-9.1) remains off.
7. **The consent plane's event convention.** The unified-consent-plane spec
   already decided `consent.granted` events live in the object's own
   append-only store. SPE is the envelope those events (and every other
   mutation event) carry. One envelope, not two.

## 6. The model

> **One envelope, defined once in sk-standards, embedded in every store's
> native record. Undo is a reversing event.**

The normative schema lives in
`sk-standards/standards/PROVENANCE_AND_MUTATION_STANDARD.md` section 1. In
brief: `spe` version tag, `actor.{id, role, node, session}` (id resolved via
`capauth.resolve_agent_identity()`, never caller-supplied), `ts`, `action`
(from the store's registered verb set), `target.{store, kind, id}` (validated
before the event is emitted), `prior` (the state ref the writer observed),
and `sig.{suite_id, value}` (detached signature over blanked-slot canonical
bytes, suite from the registry so it inherits CRYPTO_AGILITY end to end).

Per store, adoption means:

- **GTD** gains an append-only event journal beside the flat lists
  (per-writer `events/<agent>@<host>.jsonl`, copying the CardStore shape so
  Syncthing merges instead of conflicts). The flat lists remain the read
  model every existing consumer already parses; the journal is the provenance
  and undo record. `done` becomes: append the SPE event, then move the item
  archive-ward with write-then-delete ordering. `reopen` is the inverse event
  plus the reverse move, restoring the SAME id.
- **CardStore** events gain the SPE keys additively (the event dict at
  `card_store.py:236-243` simply grows; old events classify `pre-spe`).
- **Fleet** populates the `writer.signature` slot via the existing machinery,
  adds `suite_id` beside it, resolves `identity` through the resolver instead
  of the literal `"operator"`, and either makes `fleet_act`'s "SIGNED" claim
  true or deletes the word.

Reversal falls out of append-only: an undo is one more enveloped event whose
`prior` points at the event it reverses. History is never edited; the
timeline reads "did X, undid X", both attributed. That is exactly how
CardStore's `reopen` already works; SPE generalizes it to every store.

## 7. Phasing + blast radius (1:1 with child cards)

Each phase ships value standalone and is reversible on its own.

**P0, Standard.** Define SPE in sk-standards: envelope schema, signature
suite rules, verification classes and modes, the reversal rule, the named
anti-patterns, and the adoption checklist per store. Everything else
references it. *Blast radius: zero runtime; docs only.* (The draft standard
lands with this spec; the card is review + ratify + merge.)

**P1, GTD safety.** The incident fix, shippable alone with no crypto and no
identity dependency: (a) per-writer append-only event journal on every GTD
mutation; (b) `gtd reopen <id>` as the inverse of `done`, restoring the same
id; (c) `done` reordered to write-then-delete (adopt the `_upsert_locked`
ordering); (d) archive joins the lookup universe (`_find_item_across_lists`
iterates one shared file-set constant that includes `archive.json`; the three
divergent file-set definitions collapse to one); (e) the two unlocked writers
(`cli/gtd.py:46-61`, skos `mail.py:169-170` + `:453-460`) routed through the
locked atomic sink. *Blast radius: the GTD store only; the journal is
additive; list formats unchanged, so every existing reader keeps working.
Worst case is a malformed journal line, which no reader depends on yet.*

**P2, Identity.** Populate the envelope's `actor.*` from
`capauth.resolve_agent_identity()` in every GTD write path, sign events via
the capauth backend (permissive: sign when a key is present, count when not),
and add a verify command (`skcapstone gtd verify` reporting
verified/unsigned/invalid/pre-spe counts per the doctor convention). *Blast
radius: metadata only; permissive mode never refuses a write; a resolver
failure degrades to an unsigned envelope with the raw `SKAGENT` noted, never
a blocked capture.*

**P3, Coord + fleet adoption.** CardStore events carry SPE (additive keys);
fleet runs the key ceremony, adds `sig.suite_id` to the writer block, flips
`SKFLEET_SIGNING` to permissive (enforce stays a Chef-hand flip gated on
doctor-green, per the fleet signing module's own rollout design); `fleet_act`
resolves `by` via the resolver and its docstring stops overclaiming (or the
claim becomes true because the verify path enforces it at actuation). *Blast
radius: event/spec shape is additive; permissive signing changes no
behavior; enforce is a separate, reversible flag flip that refuses actuation
on unsigned specs but never stops running services.*

**P4, Enforcement.** The CI completeness gate: enumerate each store's write
entry points and fail the build on any writer that does not produce a valid
envelope, following the SKWORLD_AUTHORIZATION_STANDARD section 3 pattern
(shadow soak is necessary but NOT sufficient; it structurally cannot see
unmapped paths, only enumeration can). Plus a `skcapstone doctor` check
reporting envelope coverage per store. *Blast radius: CI and doctor only; no
runtime enforcement beyond what P3's mode flags already gate.*

Dependencies: P1 depends on P0 (event schema shape). P2 depends on P1 (the
journal is what identity attaches to). P3 depends on P0 and P2 (signing
conventions proven on GTD first). P4 depends on P1-P3 (something to
enumerate).

## 8. Separate track (in scope, not an epic phase)

`skoperator.timer` and the other systemd user timers BYPASS the
`sk-cron-run.sh` wrapper every crontab entry uses, so they get no run-ledger,
no failure-to-GTD, no `sk-alert`. That is why skoperator failed 16 times and
told nobody, a direct violation of
`sk-standards/standards/OBSERVABILITY_AND_SCHEDULING_STANDARD.md` ("nothing
scheduled fails silently"). Fix: wrap the timer `ExecStart`s in the same
wrapper. This is an observability card, not a provenance card; it rides
beside the epic, not inside it.

## 9. Risks

| Risk | Mitigation |
|---|---|
| **Journal/list divergence in GTD.** The flat lists stay the read model; a bug could let them drift from the journal | The journal is provenance and undo, not a second source of list truth; P4's doctor check compares journal tail against list state and reports drift; the fold never silently "corrects" a list |
| **Syncthing conflicts on the journal** | Per-writer per-node JSONL files, the exact CardStore shape that already survives the same sync fabric; appends on different nodes merge by construction |
| **Resolver unavailable at write time bricks capture** | Permissive posture in P2: capture never blocks on identity; a failed resolve produces an unsigned envelope and a counted degradation, enforcement arrives only via P4 CI + the store's own mode flag |
| **The 5188-item archive tempts a backfill** | Forbidden by the standard: `pre-spe` records are grandfathered read-only; fabricating attribution is worse than admitting its absence |
| **Signature enforcement breaks a writer nobody enumerated** | That is precisely why P4 is enumeration-based CI, not soak-based; the authorization standard's incident already proved soak cannot see unmapped paths |
| **Scope creep into a GTD rewrite** | P1 touches exactly the five listed defects; the schema/pydantic unification is noted as follow-up, not smuggled in |
| **Provenance read as surveillance** | The envelope carries what undo needs (identity, session, prior state) and nothing else; no dashboards rank agents by mistakes; the standard states the empowerment framing normatively |

## 10. Not doing

- **No new store, service, or database.** The envelope embeds in each store's
  native records. A parallel provenance store is the anti-pattern.
- **No backfill of historical records.** Pre-SPE history stays as it is,
  classified `pre-spe`.
- **No GTD rewrite.** Lists stay lists; readers are untouched; the pydantic
  schema unification (two constructors, three naming axes) is follow-up work
  the P1 card links but does not include.
- **No LLM anywhere in the envelope, signing, or verification path.**
- **No blocking of interactive human shell edits.** Shell is root-equivalent
  here; the honest boundary is the tooling paths plus signed records plus
  doctor visibility (same posture as the consent plane spec, section 9).
- **No quorum signing or multi-party ceremonies** for a one-operator fleet.
- **No blame tooling.** No per-agent error leaderboards, no shame reports.
  The verify surfaces count envelope coverage, not actor mistakes.

## 11. Open questions

1. **Session id source.** Claude Code sessions have a stable session URL;
   Hermes and cron runs need an equivalent convention (`run_id`?
   `SKAGENT+pid+boot-id`?). P0 should pick one shape; the field is SHOULD,
   not MUST, until then.
2. **GTD journal retention.** The archive holds 5188 items; the journal will
   grow unbounded. GFS-style rotation per BACKUP_AND_RETENTION_STANDARD, or
   append forever and let the store's own backup policy handle it?
3. **ITIL CAB vote files.** The change-management spec already binds vote
   identity to the authenticated subject. Should `cab-decisions/*.json` adopt
   the full SPE in P3, or is that the consent plane's Phase 2 delivering the
   same envelope? (Answer should be "same envelope, whichever lands first";
   flagging to prevent a double-build.)
4. **Suite choice at the fleet key ceremony.** capauth PGP backend today; do
   we register `mldsa65-ed25519-v2` as the fleet writer suite from day one
   (dual-stack per CRYPTO_AGILITY section 3) or start `ed25519-v1` and roll?
5. **Does `skcoord` gain a `gtd`-style verify, or does one
   `skcapstone spe verify` command walk all three stores?** One command is
   friendlier to doctor integration; per-store commands are friendlier to
   repo ownership boundaries (MCP_TOOL_OWNERSHIP_STANDARD).
