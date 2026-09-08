# Multi-host coordination layout

Card 556491d9 candidate configuration uses one Syncthing folder per host's
coordination root, `~/.skcapstone/coordination`. Mutable agent projections are
written below `coordination/agents/<host-id>/`, using a sanitized host id. No
folder may cover `~/.skcapstone`, `~/.skcapstone/coordination/agents`, or a
nested coordination path in addition to this root.

## Five-host diff and rollback

The exact intended path change is identical on all five hosts:

```text
- <home>/.skcapstone                 (broad overlapping folder)
+ <home>/.skcapstone/coordination    (single coordination root)
```

Host set: `chiap01`, `chiap02`, `chiap03`, `chiap04`, `chiap08`. This is a
review artifact only; no runtime rollout was performed by this card. Before
approval, export each host's Syncthing XML, save it beside the deployment
record, and make the inverse diff above the rollback. Restore the XML only
with exact deployment approval.

Conflict files remain in place and are announced as
`conflict_reconciliation_required`; no replacement, deletion, or mtime
last-writer-wins operation is permitted.
