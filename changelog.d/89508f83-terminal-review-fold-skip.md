- Card `89508f83`: the fleet rotation no longer re-folds structurally labelled
  review cards that an authoritative read already proves terminal. Before, every
  card whose raw `initial_labels` named `review` was added to the bounded POOL_V2
  input set before legacy selection, so `_shadow_pool_v2` folded each terminal
  review card a second time. On the measured production board this dominated the
  population (terminal_cardstore 1518 of 1752) while only 21 cards were ready.
  The skip fires only when the authoritative lifecycle fold reaches complete or
  void, the ITIL projection is terminal, or a hashed review outcome records
  PASS/FAIL with no newer reopen; reopened, active, claimed, and dependency-held
  review cards are still folded, and the eligible card set is unchanged because
  each terminal fact already forces POOL_V2 ineligibility. A
  `POOL_V2_TERMINAL_SKIP` line reports the bounded skip count per cycle.
