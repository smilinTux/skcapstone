- Card `0c678c8e`: include Pi-emitted `name` and `contextWindow` in the catalog
  inventory fingerprint so identical logical IDs with metadata drift rewrite
  `models.json` instead of reporting `changed=[]`.

- Card `75de8848`: close Pi catalog review FAIL findings from `e1f4321e` /
  PR 699 — no size downgrade, healthy logical routes only, health-aware
  fingerprint, required `SKFLEET_GATEWAY_URL`, and gateway revision passed into
  reconciliation.

- Card `075a8493`: Pi's SKGateway catalog reconciler now selects only currently
  advertised gateway routes, invalidates stale served-model names when the
  gateway revision or inventory fingerprint changes, and falls a stale
  `defaultModel` back across policy-compatible logical size capacity without
  hardcoding host targets or concrete served model names.
