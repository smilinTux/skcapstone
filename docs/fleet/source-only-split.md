# source-only: one label, two unrelated meanings

## The defect

`source-only` meant two different things at the same time, and nothing recorded
which one a given card intended.

**To the dispatcher it was a routing flag.** `_source_workspace_spec`
(`scripts/fleet/skfleet-rotate.py`) returned `None` unless the card carried
`source-only`. The label was therefore the only thing that made a
`repository` / `base_ref` / `base_revision` binding get demanded, verified and
materialized. On a card without the label, a broken binding was inert: never
looked at, never checked.

**To a worker it was a safety constraint.** Card acceptance criteria say so in
their own words: "Source-only. No live database write, provider, Inbox,
mailing, deployment, push, or external action."

Two consequences followed. Triage could not fix a card's routing by dropping the
label without also stripping a no-external-action constraint, and triage could
not answer "is this card mislabelled?" without reading every description,
because the label did not say which meaning was intended.

This bit real work. Five read-only chiap08 host-ops cards (`ed3ad3c7`,
`9912e905`, `e26fc5ac`, `760240ca`, `7a4d9c11`) produce evidence rather than
commits and use only the safety sense. Dropping `source-only` to unjam their
routing would have removed a no-external-action constraint from precisely the
cards that touch hosts, so they were bound to a repository instead: a worker
gets a checkout it may ignore, which is the harmless direction to err.

## The measured split

Folded from the CardStore on chiap08 in a fresh process on 2026-09-18. The chi
and nor estates have disjoint card stores, so this census is only valid run on
chi. "Live" means not archived and not `done`.

| Population | Count |
| --- | --- |
| Cards folded | 7,212 |
| Carrying `source-only` (all statuses) | 2,612 |
| Carrying `source-only` and live | 631 |

Of the 631 live labelled cards:

| Shape | Count | Meaning in use |
| --- | --- | --- |
| Complete binding | 613 | routing, and often safety too |
| Partial or absent binding | 18 | safety only; silently `WORKSPACE_BLOCKED` today |
| State the safety constraint in their own words | 274 | safety |
| Both a binding and stated safety language | 265 | **both senses at once** |

And the population the label was hiding entirely:

| Shape (live, **no** `source-only` label) | Count | Status today |
| --- | --- | --- |
| Complete binding | 79 | binding never checked, workspace never materialized |
| Partial binding | 511 | binding never checked |

Method and confidence. Counts of cards, labels and binding fields are exact:
every card was folded through `CardStore.fold` in a fresh process and its
`links` and `meta` read directly, with no hand-rolled event replay. The 79
complete-binding cards were additionally run through the real
`_source_workspace_spec` extracted from `scripts/fleet/skfleet-rotate.py` by
AST, and all 79 validate cleanly, so widening the routing trigger to cover them
blocks nothing that dispatches today. That is a measurement, not an estimate.

The "states the safety constraint" figure is the one soft number. It comes from
a regular expression over each card's description and acceptance criteria
matching phrases such as "no external action", "no live database write",
"read-only" and "evidence only". It is a lower bound on the safety sense, not a
manual read of 631 descriptions, and a card can rely on the safety sense while
phrasing it in a way the pattern misses. It is reported to show that both senses
are genuinely in live use, which it does decisively at 265 cards carrying both.

## The design

Keep `source-only` as the safety constraint, which is its plain-English meaning
and the one already written into acceptance criteria. Move routing onto the
binding itself, because a card with a binding wants a workspace and a card
without one does not. That is a better trigger than any label: it cannot drift
from the thing it describes.

Routing now fires when **either** the legacy `source-only` label is present
**or** a complete binding is present on its own.

| Card | Before | After |
| --- | --- | --- |
| `source-only` + complete binding | workspace materialized | unchanged |
| `source-only` + partial or no binding | `WORKSPACE_BLOCKED` | unchanged |
| complete binding, no label | binding ignored | binding checked, workspace materialized |
| partial binding, no label | binding ignored | unchanged |
| `no-external-action` only | n/a | no workspace, safety rail emitted |

Two details are deliberate.

**The labelled path is not touched at all**, including its hard failure when the
binding is absent or partial. 2,612 cards carry the label and none are being
relabelled, so they must behave byte for byte as before.

**A partial binding with no label stays inert rather than raising.** Symmetry
would be tidier, but 511 live cards are in that state and raising would turn
every one into a `WORKSPACE_BLOCKED` skip. That trades a checking win for a
fleet-wide liveness regression, which is the wrong direction to fail in. A
partial binding is a triage defect to report, not a dispatch trigger.

### Why this fails safe

Nothing in the routing path reads a label to decide whether to check a binding
any more. So a card cannot lose its workspace by losing a label, and cannot lose
its safety constraint as a side effect of a routing fix. The two failure modes
that remain are a card getting a workspace it does not need, which is the
explicitly harmless direction, and a card whose partial binding goes unchecked,
which is exactly the status quo rather than a regression.

### The safety half gains a home

`no-external-action` is the constraint stated on its own, with no routing sense.
`source-only` remains a first-class synonym for it: the deprecated spelling of
the same constraint, not a different one.

Until now the constraint had no home in code at all. It lived only as prose in
acceptance criteria that a worker might or might not honour, while the same
label quietly drove workspace routing. Splitting the meanings is only safe if
the safety half is actually carried somewhere, so it now emits a rail into the
worker brief (`_worker_no_external_action_instructions`).

That rail deliberately does **not** rule on whether pushing a branch is
permitted. Some acceptance criteria list "push" among the forbidden actions
while the DEFINITION OF DONE rail makes pushing mandatory. That contradiction
predates this change and affects 265 live cards; resolving it by fiat here would
silently change what those cards mean. The rail defers to the card's own
criteria and leaves the question open. **It should be settled deliberately, as
its own piece of work.**

## Migration

Nothing has to be relabelled for this change to land, which is the point. The
compatibility shim is that `source-only` keeps both meanings indefinitely.

A later relabelling pass, if one is wanted, would need to:

1. **Bind, do not unlabel, for the 18 live labelled cards with a partial or
   absent binding.** They are jammed at `WORKSPACE_BLOCKED` today. Giving them a
   complete binding unjams them and preserves the safety sense. Removing the
   label would also unjam them, and would silently drop the constraint.
2. **Add `no-external-action` before removing `source-only`, never the reverse,
   and never in one step.** Both labels on a card is a valid, safe state; that
   is what makes the pass interruptible. A pass that removes first leaves a
   window where the card asserts nothing.
3. **Leave `source-only` on any card with a binding.** It costs nothing there,
   because routing no longer depends on it, and removing it is the only way to
   lose the safety sense.
4. **Treat the 511 partial bindings as a separate triage queue.** They are not a
   labelling problem. Each needs its binding completed or removed, and a removed
   binding that was load bearing is a worse outcome than a partial one, so each
   needs a look rather than a sweep.
5. **Never infer intent from the title.** The census above classifies by
   description and acceptance criteria because titles do not distinguish the two
   senses.

### Deployment

`~/.local/bin/skfleet-rotate.py` is a separately deployed artifact per host and
a `git pull` does not update it. This change is inert on the fleet until that
artifact is redeployed to each chi host. Until then the board behaves exactly as
it does today, which is itself the fail-safe property: a host running the old
copy simply keeps ignoring the 79 unlabelled bindings.
