- Card `9ccc42ec` (port of #203 and #237): `coord amend-criteria` and its MCP twin now
  verify the amendment by reading the authoritative fold back and fail loudly
  (`did not persist`) when the writer silently discarded the event, instead of
  reporting success. `coord parity` no longer advises the unsafe automatic
  `coord migrate` plus `coord reconcile --apply` remediation; it tells the operator
  to inspect the named fields and choose the authoritative direction. Regression
  tests pin that status and kanban expose the same folded dependency gates the
  claim path enforces.
