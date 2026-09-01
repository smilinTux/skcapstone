# Card Authoring Dispatch Gates

This document explains how the SKCapstone fleet selector decides whether a card
is eligible for automatic dispatch, and how to author cards that are correctly
configured for fleet execution.

## Authority

The canonical authority for dispatch decisions is the fleet selector script at
`scripts/fleet/skfleet-rotate.py`. The `coord_dispatch_gates` module in this
repository mirrors that logic to provide early feedback at card creation time.

If the two ever disagree, the selector wins and the module here is stale.

## Dispatch Gates

A **gate** is a condition that can block a card from being automatically dispatched
to a fleet worker. Gates fall into two categories:

### Removable Gates

These are workflow steps. You can clear them by modifying card labels:

- **not-claimable**: Labels `not-claimable`, `sprint-container`, or `do-not-claim`
  keep the card out of the pool. Remove the label to release it.

- **human-gate** (label): A `human-gate` label blocks dispatch until a human
  decision is recorded. Remove it when the human decides to proceed.

- **non-implementation**: Labels like `planning-only-container` or `no-action-authorized`
  mark this as not a unit of implementation work.

- **sensitive-category**: Cards matching keywords like `credential`, `secret`,
  `deploy`, or `release` are considered sensitive. Add the `dispatch-approved`
  label to opt in to automatic dispatch for sensitive cards.

- **governed-class**: Cards with `[REVIEW]`, `[REREVIEW]`, or `[REPAIR]` in the
  title require exactly one `parent-<cardid>` label to establish dependency.

### Permanent Gates

These gates are baked into the card title and can never be removed:

- **human-gate** (title): If you put `[HUMAN]` in the card title, the card can
  never be auto-dispatched. Titles and initial labels are immutable, so this
  gate is permanent.

**Use a `human-gate` LABEL instead of a title marker.** The label is removable;
the title marker is not.

## Checking Gates

Use the `coord gates` command to see why a card is or is not dispatchable:

```bash
skcapstone coord gates <card_id>
```

This folds labels the way the selector does: `core.json` initial_labels plus
`add_label`/`remove_label` events from `coordination/card_events`. It does not
read the structural lifecycle from `cards/<id>/events/`, and it does not read
the legacy `coordination/tasks` JSON, which shows original labels forever.

## Common Mistakes

### Mistake 1: Using `[HUMAN]` in the title

Wrong:
```
[SKGW-06A6][S][HUMAN] Authorize a service-token lifecycle
```

Right:
```
[SKGW-06A6][S] Authorize a service-token lifecycle
```
(Then add the `human-gate` label if needed)

### Mistake 2: Missing parent label on governed cards

Wrong:
```
[SKX-01][REPAIR] Fix the dispatch logic
```

Right:
```
[SKX-01][REPAIR] Fix the dispatch logic
```
(With label `parent-abc123def456`)

### Mistake 3: Not opting in to sensitive work

Wrong:
```
[SKX-01][S] Rotate the production credential
```

Right:
```
[SKX-01][S] Rotate the production credential
```
(With label `dispatch-approved`)

## Real-World Example

Card 983336c1 was authored with `[HUMAN]` in the title and carried a signed
human authorization with a fixed 30-minute issue window. Because the gate was
in the title, it could not be cleared, and the card sat unclaimed until the
authorization expired. The fix was simple: void the card and recreate it without
the title marker, using a `human-gate` label instead.

This feature exists to prevent that exact failure from happening again. The
`coord create` command now warns you immediately if a card is born undispatchable.
