# Writing a card the fleet can actually dispatch

Card creation and fleet dispatch use different gates:

- `skcoord/card_store.py` governs creation.
- `scripts/fleet/skfleet-rotate.py` governs fleet selection and preclaim.

The selector's `authoritative_claimability()` function is the dispatch
authority. If this document and that function disagree, the function wins and
this document must be corrected.

Human approval is separate from machine state. A complete dependency, a label,
an elapsed deadline, or a successful CLI write never manufactures human
authority. Record the exact human decision and its evidence first, then make
only the state changes that decision authorizes.

`--agent` records attribution; it does not authenticate a human. Never type a
human writer identity on a person's behalf. The human or trusted decision
recorder must actually make or attest the recorded decision.

Never bypass a selector gate with manual execution. If a fixed authorization
window arrives while the card is still blocked, stop and repair the card or
obtain a fresh authorization. Urgency does not change authority.

## Birth facts and folded state

`core.json` is write-once, so its original title, description,
`initial_labels`, and dependencies remain immutable birth facts. The effective
card is not frozen. Attributed append-only events can fold a new title or
description, add or remove labels and dependencies, and change lifecycle state.

Use a removable label for a temporary hold:

```text
human-gate
do-not-claim
```

Use `[HUMAN]` in a title only for a standing human-only card. The title can be
changed with `coord describe --title`, but changing it is an attributed contract
edit, not proof that a human approved execution. Do not remove `[HUMAN]` merely
because another machine condition became true.

Removing one hold does not remove another. A card carrying both `human-gate`
and `do-not-claim` stays blocked until both are removed by an authorized
decision.

## Creation governor

The creation class is case-insensitive:

```python
_GOVERNED_CARD_CLASS = re.compile(
    r"\[(REVIEW|REREVIEW|REPAIR)\]", re.IGNORECASE
)
```

A new `[REVIEW]`, `[REREVIEW]`, or `[REPAIR]` card normally requires exactly
one `parent-<card_id>` initial label. The parent must exist. `REREVIEW` folds to
the review class, a live sibling of the same governed class blocks a duplicate,
and a third review level is refused.

`human-override` skips the creation governor. It is a human-authority escape
hatch, not a convenient way around a malformed card. Use it only when an
explicit human decision authorizes that exact creation. CardStore does not
authenticate that label by itself, so the surrounding operator gate remains
mandatory.

Creation governance reads birth facts. Later title and label events do not
rerun the creation governor.

## Card-authoring gates used by dispatch

`authoritative_claimability()` folds the card and returns one reason. Relevant
reasons, in evaluation order, are:

1. `non-task`, `void`, `archive`, `done`, and `owned-*`: the card kind or
   lifecycle is not available for a new claim.
2. `human-gate`: `non_implementation()` found `[HUMAN]` in the folded title or
   labels, or one of these folded labels:

   ```text
   planning-only-container
   do-not-claim-as-implementation
   human-gate
   human-decision-recorded-no-action
   no-action-authorized
   ```

3. `foreign-project`: the folded labels contain `foreign-project`.
4. `not-claimable`: folded labels, or legacy core tags, contain one of:

   ```python
   _NOT_CLAIMABLE = {"not-claimable", "sprint-container", "do-not-claim"}
   ```

5. `sensitive-category`: the folded title matches this expression and the
   folded labels do not contain `dispatch-approved`:

   ```python
   _SENSITIVE_CATEGORY = re.compile(
       r"(capauth|credential|custody|issuer|secret|\bkey\b|rollback|"
       r"deploy|production|release|migrat)", re.I
   )
   _CATEGORY_OPT_IN = "dispatch-approved"
   ```

   The expression searches the folded title only. Labels do not trigger it.
   `dispatch-approved` is an explicit category opt-in, not human approval. The
   `\bkey\b` alternative matches the word `key`, not the plural `keys`.

6. `dependency`: at least one folded dependency is not complete with a
   non-BLOCKED outcome.
7. `host-pin:<host>`: the folded title and labels name exactly one rotation host
   other than the current host.

The fleet also applies lifecycle reassessment, ITIL, prior-launch, review, and
BLOCKED-backoff filters before or around this function. Passing the card-authoring
checks alone does not guarantee dispatch.

## BLOCKED backoff

BLOCKED is an outcome, not a label. Outcomes fold through the controlled keys
`verdict`, `result`, `disposition`, and `review_decision`. Missing or ambiguous
blocker metadata fails closed.

A newer explicit `reopen` event clears a recorded BLOCKED backoff. Otherwise the
allowed wake depends on `blocked_on`:

- `dependency`: the exact dependency edge is removed, or that exact dependency
  becomes satisfied after the verdict. Adding or changing that exact edge after
  the verdict can also wake it when the referenced dependency is already
  satisfied.
- `human`: every named referent receives a matching approval or void event after
  the verdict from `chef`, `human`, or a `human-decision-recorder` writer. The
  event must be an authorized void, or a matching approval or void link or label.
- `capability`: the card first routes to the stronger lane. After that route also
  blocks, its referents must be `ac:<number>` or `free`, and an attributed
  contract change is required before another attempt.
- `card`: every exact referenced card resolves after the verdict. A referenced
  non-human-gated card must complete with a satisfied outcome. A referenced
  human-gated card also requires a matching human-resolution event. If the
  blocker names only `ac:<number>` referents, an attributed title, criteria,
  dependency, or material-label change can create one new retry generation.
  Mixed card and acceptance-criterion referents fail closed.

Each blocker generation funds at most one claim-fenced retry. Separately, three
reported launches with no outcome park a card until an attributed material
change or a folded dependency completion occurs. PASS and PASS_FOR_REVIEW
outcomes remain parked for review rather than being rerun.

A human-resolution event only wakes BLOCKED backoff. It does not remove
`human-gate`, `do-not-claim`, or any other dispatch gate.

## Where the authoritative fold reads

Dispatch does not use `folded_labels()` by itself. The authoritative path is:

```text
cards/<id>/core.json                    write-once birth facts
cards/<id>/events/*.jsonl               native lifecycle and mutation events
coordination/card_events/*.jsonl        legacy overlay events
```

`_authoritative_card_state()` reads native events and overlay events, then
`_fold_claimability()` orders the combined stream by timestamp, writer, and
sequence before folding title, labels, dependencies, ownership, and lifecycle.
The legacy `coordination/tasks/<id>-*.json` projection is not selector input.

The separate `folded_labels()` helper reads birth labels plus overlay label
events for other fleet features. It is not the complete claimability fold.

## Worked example

This example creates a repair card whose folded title is sensitive because it
contains both `production` and `credential`. Set every variable explicitly:

```bash
: "${COORD_HOME:?set COORD_HOME to the SKCapstone home}"
: "${CARD_ID:?set CARD_ID to the new card ID}"
: "${PARENT_CARD_ID:?set PARENT_CARD_ID to the existing parent ID}"
: "${CARD_WRITER:?set CARD_WRITER to the attributed author}"

skcapstone coord create \
  --home "$COORD_HOME" \
  --id "$CARD_ID" \
  --title "[SKGW-AUTHZ-06A6-LC][S][REPAIR] Repair production credential lifecycle" \
  --by "$CARD_WRITER" \
  --priority high \
  --tag "parent-$PARENT_CARD_ID" \
  --tag human-gate \
  --tag do-not-claim \
  --tag skgateway \
  --tag authz \
  --desc "Prepare the authorized replacement lifecycle." \
  --criteria "Record exact human authorization before releasing either hold." \
  --criteria "Preserve immutable evidence and stop if authorization cannot be verified."
```

After the human decides, the authorized human or trusted decision recorder runs
the release. Writer attribution is explicit on every mutation:

```bash
: "${HUMAN_DECISION_WRITER:?set to human or a trusted human-decision-recorder}"
: "${HUMAN_AUTHORIZATION_FILE:?set the verified authorization artifact path}"
: "${HUMAN_AUTHORIZATION_SHA256:?set its verified SHA256}"

printf '%s  %s\n' \
  "$HUMAN_AUTHORIZATION_SHA256" \
  "$HUMAN_AUTHORIZATION_FILE" \
  | sha256sum --check --status

approval_referent="approval:$CARD_ID-execution"
approval_value="APPROVED $approval_referent artifact=$HUMAN_AUTHORIZATION_FILE sha256=$HUMAN_AUTHORIZATION_SHA256"

skcapstone coord link \
  "$CARD_ID" human_approval "$approval_value" \
  --home "$COORD_HOME" \
  --agent "$HUMAN_DECISION_WRITER"

skcapstone coord label \
  "$CARD_ID" human-gate --remove \
  --home "$COORD_HOME" \
  --agent "$HUMAN_DECISION_WRITER"

skcapstone coord label \
  "$CARD_ID" do-not-claim --remove \
  --home "$COORD_HOME" \
  --agent "$HUMAN_DECISION_WRITER"

skcapstone coord label \
  "$CARD_ID" dispatch-approved \
  --home "$COORD_HOME" \
  --agent "$HUMAN_DECISION_WRITER"
```

Read the folded card through a different path than the writes:

```bash
skcapstone coord kanban --home "$COORD_HOME" --json \
  | python3 -c '
import json, sys
card_id = sys.argv[1]
grid = json.load(sys.stdin)
cards = [card for lane in grid.values() for column in lane.values() for card in column]
print(json.dumps(next(card for card in cards if card["id"] == card_id), indent=2, sort_keys=True))
' "$CARD_ID"
```

This confirms the two-store CardStore fold. It is necessary but not sufficient
for dispatch. There is currently no public read-only CLI for one card's
`authoritative_claimability()` decision, so do not paste another implementation
of the selector into an operational command. In a source checkout, run the
selector contract tests instead:

```bash
python3 -m pytest -q \
  tests/test_skfleet_claimability.py \
  tests/test_skfleet_backoff_wake.py \
  tests/test_cli_coord_describe.py
```

At dispatch time the fleet calls `authoritative_claimability(cid, fresh=True)`
again immediately before claim. That fresh selector result, followed by the
claim command's own gate, is authoritative.

## Checklist

1. Use temporary labels for temporary holds.
2. Give every governed card one real parent unless an exact human creation
   override applies.
3. Check sensitive-category matching against the folded title only.
4. Keep human approval evidence separate from label and lifecycle state.
5. Remove every authorized hold, not just the first one encountered.
6. Inspect the folded card through Kanban, then rely on the fresh selector and
   claim gates for dispatch.
7. If a deadline arrives while a gate remains, stop. Never substitute manual
   execution for authority.
