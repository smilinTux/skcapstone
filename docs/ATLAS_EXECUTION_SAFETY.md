# Atlas execution safety contract

Atlas remains report-only unless execution is explicitly enabled. Physical
honor additionally requires every proposal to carry an exact `app`, `condition`,
`object`, and `action` binding. The app must be a registered `Operatorapp`, the
condition must belong to that app, and a human must have ratified the action on
that same object. A model response cannot grant itself eligibility.

## Two gates, and escalations are report-only (card f8dd9441)

ATLAS acts on exactly two gates:

1. **The freeze.** While the fleet is frozen nothing actuates.
2. **One-time signed ratification, or auto-normal.** An action ATLAS may run on its own is
   either a standard action ratified once for its app (`skoperator apps ratify <app>
   <action>`, signed, in the ratified catalog) or a change with the auto-normal shape that the
   ITIL fold approves with no human.

Anything else that escalates is REPORT-ONLY, always, whatever its ratification or actuator
status. There is no per-incident CAB vote for ATLAS, no approvable ATLAS decision, and no
decision queue that can actuate:

* the loop writes no ITIL change and no ledger intent for an escalation; it parks one
  acknowledge-only decision record (`report_only: true`, with the reason and
  `observed_on`, the node that saw it, for information);
* `skoperator decide --approve` refuses before any write and before asking for a grant;
  `decide --reject` acknowledges the record. The refusal, the record and the Telegram card
  carry a hint that follows the proposal's class: a major or emergency change (irreversible
  or high risk) can never be automated, so a human handles it by hand; a reversible
  non-standard one becomes auto-normal once ratified (`skoperator apps ratify <app>
  <action>`);
* the dispatcher refuses, before the approval is spent, any intent whose approval would come
  from a human CAB vote (an escalate-lane intent an older loop left behind, or any intent whose
  change carries a human vote). Only standard and auto-normal approvals dispatch;
* `skoperator honor-pending` prints what it verified and how many intents were report-only,
  and exits 3 when an intent can never actuate.

To let ATLAS fix something it escalated, ratify the action once. Otherwise a human fixes it.
The signed human CAB vote still gates the governed CMDB apply (`cmdb_act` / `_apply_gate`) and
the trustee rotate guard; those are not ATLAS actuation and are unchanged.

One record per episode: while a decision is pending, or resolved but its condition has not yet
read cleared, the same escalation reuses it. Once it was resolved and its condition read
definitely cleared, the next escalation of the same condition is a new episode with its own
record (`<id>-e<n>`). Only the node named in the record's `observed_on` ends its episode: the
decisions store is replicated, and a healthy node's view must not end another node's episode
(which would re-park and re-notify on every flap). `Unknown` never ends an episode. Loop-drafted changes are authored by `operator`, so auto-normal changes
auto-approve at the fold as designed.

## CMDB conditions and which action each makes eligible (card ff97f567)

`cmdb_adapter.CONDITIONS` is the cmdb condition table, like every other adapter's. The loop
builds its condition schema from it (`loop.CONDITION_SCHEMAS`), and the schema decides which
observed conditions reach the brief at all. Before this card the table did not exist, the cmdb
schema was empty, and every cmdb observation was dropped, so `CmdbReconcileFresh` going False
never fired anything. All three are health-type (they fire when False); `PROBLEM_WHEN_TRUE`
is empty.

| Condition | Makes eligible | Notes |
|---|---|---|
| `CmdbReconcileFresh` | `run-cmdb-shadow` | newest verified artifact older than 4 h, or none (Unknown) |
| `CmdbLastScanComplete` | `run-cmdb-shadow` | newest verified artifact incomplete |
| `CmdbAuditClean` | none | relationship audit findings are store damage a human repairs |
| (any) | `apply-cmdb-reconcile`: none | non-standard and irreversible, never eligible for an autonomous lane |

The declared mapping is `cmdb_adapter.ACTION_CONDITIONS`. Enforcement is the existing gate,
unchanged: a proposal passes only if its condition is in the app's OperatorApp `conditions`
(derived from `CONDITIONS`) and its action is in `ratifiedStandardActions`. A condition outside
the table is refused (`binding_denied`, escalated and report-only). `run-cmdb-shadow` is a
standard, reversible, low blast radius action, so once a human ratifies it a firing
`CmdbReconcileFresh` plans as auto; whether it actually runs still depends on the freeze, the
honor adapter and the evidence age, as for any app. `apply-cmdb-reconcile` stays
non-standard: it always plans as an escalation (report-only), and the manual apply goes through
`cmdb_act` and `_apply_gate` with a signed human vote, which this table does not touch.

## Execution path

The execution path now follows this fail-closed sequence:

1. Acquire the nonblocking single-flight lock.
2. Observe and build the polarity-aware brief.
3. Validate the proposal against an exact firing-condition identity.
4. Validate target existence and app-scoped human ratification.
5. Re-read the freeze at this proposal's decision point, then require fresh
   structured evidence (see below). Either refusal happens before any ITIL
   change, ledger intent or actuator call.
6. Check the stable intent fingerprint's cooldown and circuit breaker.
7. Actuate and require explicit `performed: true` proof.
8. Re-observe the owning app and require the bound condition to clear.
9. Persist success or failure atomically.

The fingerprint is SHA-256 over app, condition, object, and action. The default
cooldown is 15 minutes. Three consecutive failures open a durable circuit and
require an operator to inspect or deliberately clear the state; ordinary loop
runs never reset it. Corrupt or unsupported state blocks execution.

Each pass has a five-minute cooperative runtime budget and all production probes
and actuators must retain their own bounded I/O timeouts. The budget is checked
between observation, planning, and each action. The lock prevents overlapping
timer/manual passes.

State is stored beneath the fleet root at `atlas/state/` with owner-only file
permissions. Physical action remains frozen independently by the fleet freeze
control; this implementation does not remove or relax that control.

An unsuccessful actuator response, a missing proof, a failed postcondition, an
unknown observer, a missing condition, a cooldown, or an open circuit is a
failure, not an applied action. Reports use the brief's already normalized
`firing` and `stale` fields so health-condition polarity cannot be inverted by
the formatter.

## Evidence freshness, freeze at the decision point, outcome words

Weak spots the gate 5 fault-injection drill documented, and what the independent
review of the first fix then found, are enforced and are PASS criteria of the drill
(`scripts/atlas_p32_fault_injection_drill.py`, scenarios 1, 9, 12, 22, 23, 24).

**Evidence is structured, comes from the source, and is aged on the real clock.** A
proposal that wants to actuate carries `evidence: {source, observed_at}`
(`operator_seat/evidence.py`), where `observed_at` is the time the SOURCE data was
true, as an ISO 8601 timestamp with a timezone. It is never the pass clock: adapters
report `source_observed_at` (a live probe reports the real clock when it probed, the
cmdb adapter the reconcile artifact's end time, the skos backlog the digest's own
window end, the fleet adapter the `updatedAt` its node wrote on the status file the condition came from),
the envelope keeps it only if it is valid and never invents one, and the brief and
the proposer carry it through. An observation with no source time carries no
evidence and cannot actuate. The proposer stamps evidence from the observation,
never from the model's reply.

Age is measured against the REAL wall clock read at each decision point, not against
the pass's own timestamp, so a wrong pass clock or a slow model call cannot make old
evidence look fresh. Missing, malformed, naive (no timezone), future-dated (beyond 60
seconds of skew) or older than `skoperator run --max-evidence-age` (default 600
seconds, at most 3600) is refused with `refused: evidence ...`. A maximum age that is
non-numeric, non-positive, non-finite or above the ceiling also refuses, so the check
cannot be switched off by passing a huge number; the ceiling is an hour because a pass
has a five minute runtime budget. The legacy free-text `evidence_ref` is still
recorded on the ledger after the structured stamp, but is never freshness evidence.

**Refuse before writing, settle what was written, and make the dispatcher check
again.** Any closed actuation gate (frozen or unprovisioned) refuses an auto action
before an ITIL change or a ledger intent is written. If an intent was written and is
not actuated in the same pass (a gate closed after the write, the deadline fired, the
dispatcher refused, a cooldown suppressed it) it is voided: the ledger gains a
`PROPOSED -> ESCALATED` edge for this, with `voided` on the event, because the
dispatcher enumerates PROPOSED intents and a leftover one used to be acted on later,
unattended.

Only the auto lane gets an ITIL change and a ledger intent. A proposal the planner
escalated (an unresolvable target, a denied action binding, a major change) is report-only
(see "Two gates" above). The loop records the lane and the evidence stamp on the intent's
PROPOSED event.

The loop never trusts a change id on a proposal (a proposal is model output): it
always drafts its own change, tagged `op-bind:<app>|<action>|<target>`, the one intent
it is for. The dispatcher itself, for any intent however it was left, checks in this
order, and a refusal never writes AUTHORIZED because nothing was authorized:

* the change is bound to THIS intent (exactly one matching `op-bind:` tag; the three parts
  are percent-escaped and a raw `|` in any of them is rejected, so no part can forge the
  separator). A change approved for one action cannot run another, and a change with no
  binding authorizes nothing;
* **no human approval dispatches.** An escalate-lane intent, or an intent whose change
  carries a qualifying human vote, is refused as `refused: report-only` and left PROPOSED,
  before anything is spent (the loop closes it once its condition reads cleared);
* the approval is derived positively: the change is `standard`, or has the auto-normal shape
  (normal, tagged `auto-normal`, created by `operator`, not high risk, a rollback plan, no
  rejection). Anything else is left pending. An intent with no recorded `auto` lane is
  refused and left alone;
* a stale catalog generation, or a classification hardened since the proposal, refuses
  and leaves the intent PROPOSED (a proposal under the new catalog is a new intent);
* the target (`target_known`) and the ratified action binding (`action_allowed`) are
  re-checked when the caller supplies them (`skoperator honor-pending` does);
* the intent must carry fresh evidence stored on it, against the real clock, and is voided
  if that fails (stored evidence only ages);
* cooldown and an open circuit are suppression, not failure: a refusal with nothing
  recorded against the retry budget and nothing rolled back;
* the owning app is re-observed, and only a DEFINITE firing status counts. `Unknown` is
  not "still firing" (it is for alerting, not for permission to act), a missing
  condition or observer is not, and the re-observation must itself carry a fresh source
  time. A definitely cleared condition closes the intent with the reason "condition cleared
  before dispatch" and retires its change, so a recurrence needs a new proposal.

An approval is spent by the run it authorizes, and taking it is a compare-and-swap. Under
an exclusive per-change-id `flock` (shared by every dispatcher process on the host; it does
not span nodes) the dispatcher re-reads the change, requires it still `approved`, moves it
to `implementing` with a note naming the intent (`dispatch of <intent_id>`), and requires
that the FIRST unconflicted `approved->implementing` entry on the timeline is its own. The
timeline check matters because the fold keeps a conflicted second `implementing` event in
the `implementing` status, so the status alone cannot tell the winner from the loser. Any
other outcome refuses before the actuator is called. The change then goes to `deployed`
after a verified run or `failed` after a failed one, so the ITIL record agrees with the
ledger.

One bad intent never crashes a dispatch pass:
a ledger error while recording an authorization is returned as an `error:` outcome and the
pass carries on. An intent a crash stranded in AUTHORIZED or EXECUTING (older than ten
minutes on the real clock) is escalated at the start of the next loop or dispatch pass: FAILED
then ESCALATED, its change marked `failed`, a report-only decision parked and an alert
emitted. It is never re-run, because the actuator may or may not have run.

An injected clock must be timezone-aware; a naive one is refused, never read as local
time. A discovered (manifest) observer's own `source_observed_at` is dropped in both
spellings: evidence time comes only from an in-process adapter, so a discovered
observer can report but cannot license an actuation.

**The freeze is reported where, and as, it happened.** The gate is re-read for every
proposal and again after every attempt. A pass result carries `frozen` and
`freeze_state` (the state at the END of the pass, as it is), `frozen_mid_pass` (a freeze
landed after the pass began, even if it was lifted again), `freeze_summary` (actions
taken before the freeze, actions it refused, conditions still firing) and, on each
outcome, `freeze_state` at its decision point and `freeze_after`. The report and the
published brief say what ran before a mid-pass freeze, not that Atlas "stood down";
a pass that STARTS frozen still says so. The brief carries a stable
`state: frozen=... mid_pass=... firing=... stale=... acted_before_freeze=...` line for
downstream readers. A freeze REFUSAL is not a fault of the action and never counts
against the retry budget; a real failure that merely coincides with a freeze still does.

**"verified" means verified.** An auto action that ran reads `verified` only when the
post-action re-observation ran and passed (a lifecycle ledger or
`require_verified_actions` makes it mandatory). When nothing re-observed the
condition the outcome is `performed, unverified`. The word `applied` is no longer
produced. Nothing in the ledger, the ITIL correlation, the dispatcher or
`skcapstone doctor` branches on the outcome string; the brief artifact and the tick
report print it verbatim.

## Fleet restarts (card 21b01d01)

The fleet honor path (`act_dispatch` plus `fleet_adapter.resolve_restart_target`) only
restarts a fleet Service when every one of these holds, and otherwise refuses with
`actuator.NotAttemptedError` before an ITIL change, an `operatorActions` annotation or a
systemctl call:

* the action is `restart_service` (the other fleet actions have no physical muscle);
* the unit is the object's `spec.unit`, a `.service` unit (a bare name is normalized to
  one; a target, timer, socket or other type is refused). The object name is never used
  as a unit and a proposal that names a different unit is refused;
* the object is placed, on THIS node. No reviewed remote actuation path exists, so an
  object placed elsewhere is refused, never restarted locally;
* the spec and placement verify with `converge.verify_desired` against the local trust
  roster, and the unit is allowed by the node's profile (`profiles.unit_allowed`). Unlike
  converge, neither check is gated by its rollout mode: the autonomous seat never
  restarts on an unsigned spec or outside the profile. Limit: the roster always trusts
  the seat's own key, and `SKFLEET_SIGNING=permissive` signs every local write with it,
  so a valid signature means "written on a trusted seat", not "authored by Chef".
  `profiles.unit_allowed` only denies `mustNot` units. Requiring a signer other than
  the seat is an open decision;
* the object is `systemd-user`, not paused and not deleted. Pause, deletion, unit and
  placement are checked again immediately before systemctl.

`fleet_target_known` uses the same resolver, so the planner escalates an unrestartable
target and the dispatcher refuses it in preflight, before the approved change is spent
or EXECUTING is written. A refusal that still reaches `apply_fn` is a preflight refusal
in both lanes: it never feeds the circuit breaker and never runs a rollback. A typed
fleet rollback is bound to the approved object and unit; one that names another object,
unit or adapter is refused.

After the restart the postcondition re-reads the fleet store every 5 seconds for up to
75 (never more than 120, and never past the pass deadline) and accepts only evidence
written strictly after the restart: a whole-second `updatedAt` must fall in a later
second than the restart, so the pre-action status can never verify it.

**Deliberate limitation: a steadily failed service reads as stale.** Status files are
write-on-change, so `updatedAt` is the last time the node saw the content CHANGE. A
service that has failed and stayed unchanged for longer than `--max-evidence-age` has
evidence older than the limit and its restart is refused as stale. That fails closed on
purpose: the seat cannot prove the failure is still current. The follow-up is on the
node daemon: sknoded stamps a periodic observed-at on each status it re-checks, so an
unchanged reading stays provably fresh. Until then, such a restart is a human's call.

## Multi-node: one actuator, node-local locks, nothing stranded alive

The ledger, the ITIL records and the decisions store live under the fleet root, which
Syncthing replicates, and `skoperator.timer` runs on several node profiles. An `flock` only
orders processes on one kernel, so none of the locks below protects against a second node.

**Exactly one node actuates.** Actuation (the loop's inline and ITIL lanes, `dispatch_intent`,
`run_dispatch_pass`, `skoperator honor-pending`) refuses unless this node is the DESIGNATED
actuator (`operator_seat/node_local.py`). The designation is node-local configuration, the
environment variable `SKOPERATOR_ACTUATOR_NODE` (a systemd drop-in) or the first line of
`~/.config/skcapstone/atlas-actuator-node`, compared with this node's name, and it DEFAULTS
TO NONE: until an operator designates a node, nothing actuates anywhere. Other nodes still
observe, propose and park decisions, and that is ALL they do: the loop on a node that is not the
designated actuator does not sweep (stranded, stale auto-lane, cleared-episode closing) and does
not draft changes or ledger intents, because the ledger and ITIL records are replicated and its
clock or its local view of a condition could escalate the actuator's live intent. The manual act verbs (`skcapstone cmdb operator act`,
the per-adapter act functions) are separate entry points and are not covered by this rule.

**Locks are node-local.** The per-change lock and the single-flight lock live in a node-local
state directory (`$SKCAPSTONE_LOCAL_STATE_DIR`, else `$XDG_STATE_HOME/skcapstone`, else
`~/.local/state/skcapstone`), created 0700. EVERY directory from that state directory down to
the lock directory is verified on every acquisition to be a real directory (never a symlink)
owned by this user with no group or world access, otherwise the dispatch is refused; the lock
files are opened `O_NOFOLLOW`. Directories above the state directory are the operator's own
layout and are not judged.

**A live dispatch is never swept as stranded.** Every lifecycle event is stamped from the
clock at write time, never the pass start, and a dispatch holds its per-change lock from the
moment it commits to the change until it has closed the change out. The stranded sweep takes
that lock non-blocking and skips a change it cannot take. `run_dispatch_pass` and
`honor-pending` also take the same single-flight lock as the loop (they refuse with "another
Atlas operator pass is active" rather than overlap it). An intent a crash left in AUTHORIZED
whose change is still only `approved` has that change retired (rejected, then closed), since
`approved -> failed` is not a legal transition.

**Nothing is rolled back after an actuation that ran.** Once the postcondition has verified,
a bookkeeping error (the ledger, the ITIL record) is recorded as an `error:` outcome with an
alert and the fix is left in place, in both the loop and the dispatcher.

**Episodes close in the loop.** The loop closes every leftover escalate-lane intent (and
retires its change) when its condition reads definitely cleared in this pass, and marks a
resolved decision's episode ended, so the next escalation gets a new record. This runs on
every node that keeps its wiring, not only an armed actuator, so a report-only deployment
raises later episodes too, but each node ends only the episodes it observed itself. A clear-and-refire that falls entirely between two passes is
indistinguishable from one episode, because the loop only knows what it observed.
The next loop or dispatch pass also voids auto-lane intents older than one pass (twice the
runtime budget), the leftovers of a killed loop, before doing anything else.

**The rule is decided once per pass, and the inline lane is closed in production.** The loop
evaluates the actuator rule a single time at the start of a pass and uses that one answer
everywhere in the pass: asking again mid-pass could flip a refusal into an approval (a
designation file written meanwhile, or one failed read) after the ledger and ITIL wiring had
already been dropped for a non-actuator, and the proposal would then fall into the inline
lane. The inline lane (actuation with no ITIL change, no `dispatch_intent`, no change lock and
no re-observation) also never actuates in a pass whose wiring was dropped, and production
`skoperator run --execute --honor` refuses it outright (`allow_inline_actuation=False`), so
every physical action goes through the ITIL lane. The inline lane remains available for the
annotation-only deployment (`--execute` without `--honor`, which actuates nothing), for tests
and for the drill.

**Decisions are the same on every node.** Every node parks the same report-only record for
an escalation (ids are content-based and nothing in it is bound to a change), so a replicated
copy from another node is the same record, not a rival. The decisions store is replicated by
Syncthing, so a rejection made at the same moment as another node's park can conflict and the
record can reappear as pending. Approving it still refuses, so the worst case is a record the
human acknowledges again.

## Human CAB approvals: pinned approver keys and the arming runbook (card bada5c86)

**Scope.** ATLAS never actuates on a human vote (see "Two gates"); the ATLAS dispatcher only
uses this to REFUSE an intent whose change carries one. What follows gates the governed CMDB
apply and the trustee rotate guard.

**What counts as a human approval.** A CAB vote authorizes an actuation only when it stores
the signed CapAuth grant it was cast under (`skcapstone itil cab authorize`, then
`itil cab vote --authorization`) and that grant verifies against a key in the pinned approver
file `~/.config/skcapstone/cab-approvers.json`. The check runs at the governed CMDB apply gate
and the trustee rotate guard (through `skcapstone.cab_approval`) and again in the skcoord fold
(`skcoord.cab_authorization`). It
never reads `CAPAUTH_HOME` or the caller's own CapAuth profile. Votes by the change's drafter,
creator or managed-by owner, by the operator seat (`operator`, `atlas`, device seats) or by a
placeholder (`human`, `unknown`, empty) never count. The approval age limit is measured from the
grant's signed `issued_at`. Auto approval is derived from the change itself (standard, or the
auto-normal shape), never from the fold's status. **No pin file means no human approval
anywhere**, which is the state until Chef runs the steps below.

Historical votes from before signed grants were stored keep their meaning in the skcoord fold
through a pinned list (skcoord ADR 0002). They are history only: no actuation gate counts them.

### Arming runbook

Nothing here lifts the freeze. Arming generic actuation is a separate decision.

1. **Chef writes the pin file himself**, as `~/.config/skcapstone/cab-approvers.json`, mode
   0644, owned by the operator account. Put the key inline as `public_key` (the ASCII armor of
   his CapAuth human identity's public key, normally `<capauth home>/identity/public.asc`, as one
   JSON string with `\n` line breaks). Inline is recommended: the file is then self-contained
   and the same bytes work on every node, while a `public_key_file` path differs between
   noroc2027 and the chi nodes and silently pins nobody where it does not resolve.
   `public_key_file` is still accepted.

   ```json
   {
     "approvers": [
       {
         "identity": "chef",
         "fingerprint": "ADAD14CCAC8D6D0BF5A4209DB994E78200BF6422",
         "roles": ["owner", "approver"],
         "backend": "pgpy",
         "public_key": "<ASCII ARMOR OF CHEF'S CAPAUTH identity/public.asc>"
       }
     ]
   }
   ```

2. **Read it back** before trusting it. The fingerprint must come from the key file, not from
   the JSON:

   ```bash
   ~/.skenv/bin/python - <<'PY'
   from capauth.crypto import get_backend
   from capauth.models import CryptoBackendType
   from skcapstone import cab_approval
   pins = cab_approval.load_approvers()
   assert pins, "pin file missing or unreadable"
   for fpr, entry in pins.items():
       derived = get_backend(CryptoBackendType(entry["backend"])).fingerprint_from_armor(entry["public_key"])
       print(entry["identity"], fpr, sorted(entry["roles"]), "key matches:", derived.replace(" ", "").upper() == fpr)
   PY
   ```

   Expect one line: `chef ADAD14CCAC8D6D0BF5A4209DB994E78200BF6422 ['approver', 'owner'] key matches: True`.
   Copy the same file to every node whose skcoord fold or ATLAS gate should accept Chef's
   approvals, and read it back there too.

3. **Re-vote open changes.** No existing vote carries a stored grant (the envelope behind
   Chef's chg-a543c87b vote was not retained), so every open change that needs a human
   approval must be re-approved through the signed path: `skcapstone itil cab authorize <change>
   --decision approved --target <target> --scope <scope> --output <file>`, then `skcapstone itil
   cab vote <change> --authorization <file> --target <target> --scope <scope>`. Confirm with
   `cab_approval.approval_basis` that the change now reads `human`.

   ATLAS decisions cannot be approved at all; ratify the action instead.

   A human approval is usable for a day from the grant's signed `issued_at` at both gates, so
   vote close to when the change should run.

4. **Rollback:** move the pin file away. Every gate then fails closed (no human approvals);
   auto approvals are unaffected.

### Trust limit (stated plainly)

Everything here runs as the `cbrd21` account, which has passwordless sudo. What the pin does
stop is a vote that no pinned key signed: a vote cast through the sanctioned surfaces (the MCP
tool, the CLI) without a grant, a hand-written or edited vote file, and a
vote synced in from another node. A genuine signed vote is bound to the change and voter it was
cast for, so a copy of its file under another change's or another voter's name counts for
nothing, and at the gates it expires a day after its `issued_at`.

What the pin does not stop is anything running as `cbrd21` (or as root through sudo). Such a
process can rewrite the pin file to add its own key, change the code that reads the pin, or
sign with any key this account can read. Nothing records the pin file's hash or checks it today,
so such a change would go unnoticed. The pin raises the bar from "write one JSON file" to
"rewrite operator config or code on this node". It is not a boundary against the operator
account.

Recommended follow-ups to anchor the pin (not done; Chef decides):

- Run ATLAS and the agents as a separate unprivileged user that cannot write the pin file or
  the installed code, with no passwordless sudo.
- Ship the pin inside a manifest signed by Chef (as the module manifests are), and verify that
  signature against Chef's fingerprint before trusting the pin's contents.
- Record the pin file's sha256 in the CMDB as a configuration item, so reconcile drift reports a
  changed pin.
