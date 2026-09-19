- **An unsized card no longer disappears from the rotation without a reason.**
  A card whose title carries no single `[S]`/`[M]`/`[L]`/`[XL]` marker resolves
  to no gateway bucket, and the candidate scan drops it before lane selection.
  That drop is correct and stays: a size names a capability bucket, and guessing
  one silently downgrades work onto a weaker model than its author chose. The
  defect was that the drop was SILENT. The card never reached
  `select_compatible_lane()`, so it recorded no lane reason, and
  `_selection_diagnostic()` fell through to its catch-all `no-compatible-lane`
   - a reason naming a subsystem that had never seen the card. On 2026-09-19
  five cards sat unclaimed across the chi fleet against 13 free seats on one
  host and 9 on another, and the reported reason sent debugging into
  `lane_compatibility()` for hours; the real fix was a size marker in five
  titles. The rotation now emits `UNSIZED_SKIPPED|<host>|count=N ids=... `
  every cycle it drops one, `SELECTION_EMPTY` carries an `unsized=N` count, and
  the reason reads `unsized-cards` when every owned candidate was dropped for
  that and nothing else. Routing behaviour is unchanged: the same cards are
  dropped, they are just no longer dropped anonymously.

- **`docs/fleet/card-authoring-dispatch-gates.md` documents the size marker.**
  It was the one mandatory dispatch gate the card-authoring guide never stated,
  and it is invisible to `coord gates <id>`, which reports `eligible: true` for
  a card that can never be dispatched. The new section gives the rule, the
  three ways to fail it, the fact that a `sk-m` LABEL does not substitute for
  the marker (`_size_class_for()` reads size labels only when the folded title
  is empty, which a real card's never is), and how to read the refusal.
