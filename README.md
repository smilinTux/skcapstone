# skdashboard

The SKWorld operator dashboard (coord board + ITIL + kanban + CMDB), extracted
from `skcapstone` (CR-4.3). Serves the `:7778` web UI + JSON API.

## Dependency direction

Coordination access goes through **skcoord** directly (`skcoord.card`,
`skcoord.card_store`, `skcoord.coordination`, `skcoord.itil`, `skcoord.cmdb`) —
there are no `skcapstone.coordination` / `skcapstone.card_store` imports (CI grep
gate). The richer agent / runtime / doctor / trust / model panels reach back into
`skcapstone` at runtime via lazy imports, so `skdashboard` depends on both but has
no import-time cycle (the dashboard is launched on demand, after skcapstone is up).

## Launch

The deployed `:7778` unit launches via `skcapstone dashboard --port 7778`; that
CLI resolves `skcapstone.dashboard` -> this package through a transparent alias
shim, so routes are byte-identical to the pre-split dashboard.

## Test

```bash
~/.skenv/bin/python -m pytest tests/ -q
```
