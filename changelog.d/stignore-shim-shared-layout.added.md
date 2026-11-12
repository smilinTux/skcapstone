- Syncthing ignore rules now install as a synced `.stignore-shared` (package data, the
  noroc2027 reference plus the private-material classes `skcapstone sync audit` probes)
  and a 3-line `.stignore` shim that `#include`s it (sk-standards
  `SYNCTHING_IGNORE_STANDARD`). One module, `stignore_layout.install`, serves both
  `ensure_skeleton` and `sync setup`: shared file first (a shim over a missing include
  errors the Syncthing folder), then the shim. A legacy full-ruleset `.stignore` is never
  overwritten: it is backed up once and the difference reported; `skcapstone sync stignore
  --migrate` rewrites it as a shim and keeps node-local rules. A worker allowlist
  `.stignore` is left alone. `sync audit` and `skfleet node stignore` now read the include-
  expanded rules, so a shim node is no longer reported as having none.
  The canonical file also keeps four rules from the old template (`**/*.sync-conflict-*`,
  `**/*.pid`, `.cache`, `.venv`); the other old-template rules stay out, as in the live
  reference, so adopting the layout does not change what replicates.
