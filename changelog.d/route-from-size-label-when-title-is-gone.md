### Fixed

A card's routing identity no longer depends on its title surviving edits.

`_size_class_for` consulted the canonical `sk-s`/`sk-m`/`sk-l`/`sk-xl` label only
when the folded title was EMPTY. A title that was present but carried no size
marker took the fail-closed branch, `_logical_route_for` returned None, and the
candidate scan filtered the card out before any lane was consulted, with no log
line naming the drop.

Measured on chi 2026-09-19: an `mcp` writer appended `describe` events carrying
literal argv fragments as the title (`x`, `--description`) to live cards, ~80
times since 2026-09-08. CardStore folds the latest describe, so
`[SKLEGAL-R33-ACTIVITY][S] Project packet and receipt Matter Activity` folded to
`x` while the card still carried `sk-s`. Every chi host then reported
`SELECTION_EMPTY reason=no-compatible-lane` against a non-empty pool with every
seat free, and the fleet ran two workers against ~30 configured seats.

The title marker still wins when there is exactly one, and routing still fails
closed with neither a marker nor a single size label.

Two diagnostics came with it, because the drop was invisible and the reason
string lied. `UNROUTED_CANDIDATES` now names every owned candidate dropped for a
missing or ambiguous size, and `_selection_diagnostic` reports
`reason=unroutable-size` instead of the trailing `no-compatible-lane` when the
whole owned slice never reached lane selection. That catch-all sent three
consecutive diagnoses after lane health, lane targets and claim ceilings.
