# Automatic crews and support requests

An owner can delegate useful independent work without releasing the parent card.
An immutable crew manifest defines direct dispatch, technical, verification and
support roles, exact source/claim/contract, reserved paths and a finite allowance.
Only roles with useful work get helpers. Persistent role records cost no idle
model calls. The existing node loop consumes requests; the existing fleet remains
responsible for placement, claim and launch on qualified routes and nodes.

## Owner and worker procedure

1. Read the parent's assigned contract and current claim. Preserve existing
   commits, dirty workspaces and receipts. Register on the designated coordinator
   host using `skfleet crew register --agent OWNER --packet crew.json`.
2. Put immediate independent tasks in `initial` slots. Use a read-only
   `stale-progress` slot for diagnosis and `support-request` slots for materials,
   tests or a specific missing tool. No minimum task age applies to explicit help.
3. Stop parent edits in delegated paths. Each helper uses an isolated checkout;
   its completion is input to the parent's integration, never parent acceptance.
4. A parent or its currently claimed helper can run `skfleet crew request --agent
   OWNER --packet request.json`. The packet selects an existing slot, not new
   instructions. Ask the parent/coordinator to amend scope through a new exact
   contract when no existing slot fits; do not spawn uncontrolled descendants.
5. Return exact artifacts and reported test outcomes using `skfleet crew receipt`.
   A verified delivery still needs the existing integration and acceptance path.
   Inspect persistent state with `skfleet crew status CREW_ID`.

The command group also exists as `skcapstone fleet crew`. `--home PATH` binds
both coordination and fleet to that explicit root, useful for isolated tests.
CLI packet input is bounded to 32 KiB. Registration validates the final helper
objective including delivery instructions before persisting its mandate.

Minimal manifest (replace fixture identities with the exact authorized ones):

```json
{
  "schema": "skfleet.crew/v1",
  "crew_id": "goal-generation-1",
  "parent_id": "1234abcd",
  "parent_claim_revision": "EXACT_CURRENT_CLAIM",
  "coordinator_node": "chiap08",
  "owner_paths": ["src/integration.py"],
  "max_active_helpers": 3,
  "max_helpers": 8,
  "slots": [{
    "slot_id": "boundary-test",
    "role": "verification",
    "trigger": "initial",
    "packet": {
      "request_id": "boundary-template",
      "title": "[S] Verify the isolated boundary",
      "objective": "Produce the assigned boundary tests and exact results",
      "criteria": ["Evidence binds the exact source and observed outcomes"],
      "allowed_paths": ["tests/test_boundary.py"],
      "verification_commands": ["pytest -q tests/test_boundary.py"]
    }
  }]
}
```

A support slot can also have `resource` containing `resource_id`, `version`,
`policy_sha256` and `context_sha256`. Repeated requests for the same slot/resource
coalesce even when request IDs or diagnostic evidence differ. Different scope or
objectives never merge. Resource workers first reuse already authorized matching
artifacts, then build only within the assigned source scope or report a specific
blocker. No package installation, download execution, new credentials or access
expansion is authorized by a support request.

Request fields: `schema=skfleet.crew-support/v1`, `crew_id`, `request_id`, `slot_id`,
`requester_card_id`, `requester_claim_revision`, `evidence_sha256`, `reason`.
The reason is recorded as context; it is not interpolated into helper instructions.

Receipt fields: `schema=skfleet.crew-receipt/v1`, `crew_id`, `request_id`,
`helper_id`, `claim_revision`, `cwd`, plus `artifacts=[{path,sha256}]` and
`tests=[{command,outcome}]`, or an explicit `blocker`. The current helper owner
submits it. Source ancestry, artifact bytes and claim generation are checked;
the receipt stores reported test outcomes, not independently rerun tests.

## Recovery, boundaries and operating limits

Creation intent is durable before the Board API call. Replay creates the same
child; it never releases the parent or starts another scheduler. Local and remote
admission recheck helper-parent generation, source and restrictions. Source
changes between mandate validation and creation are refused under the existing
Board helper lock. Admission checks are not a distributed atomic parent/child
claim transaction and do not make parent filesystem writes impossible.

One coordinator host owns the ledger. It uses local locking, atomic replacement
and bounded reads; Syncthing is not a distributed lock. Immutable requests and
receipts reject conflicting replay. A corrupt crew request holds that crew's
uncertain custody while other crews remain eligible. Unknown helper state still
occupies its slot. There is no blind retry of failed or delivered work. Changed
evidence can request a new bounded helper up to the declared allowance.

Automatic diagnostics consume fresh exact-owner/claim/session observations from
the existing worker progress reporter. Session inactivity requests read-only
diagnosis; it does not prove a worker is dead or making no useful progress. A
worker that continually writes a transcript while looping is not diagnosed by
this silence signal alone. Explicit worker support requests are immediate.

Fleet freeze prevents automatic intake/creation and all existing dispatch paths
remain in force. Registration is explicit owner delegation under the existing
seat capability boundary, not a newly signed CapAuth delegation credential.
Review-role labels do not grant formal reviewer authority.

Helpers may follow the existing approved remote dispatcher. This ledger's direct
intake and artifact verification are coordinator-local; automatic cross-host
receipt transport and arbitrary remote artifact access are not implemented here.
Remote helpers must use an existing authorized evidence/custody route. Herdr's
already installed exact handoff can be used with properly claimed helpers and
existing qualified panes; this controller does not start or claim Herdr workers.

W7 stays paused. On an authorized resumption, inventory its exact existing work
and complete the requested product-versus-design audit before splitting verified
remaining gaps. Never restart W7 or discard its work as a crew initialization step.

Installation, running-process adoption, live worker delivery and remote rollout
are separate evidence states. This source change does not itself assert any of
those operational results.
