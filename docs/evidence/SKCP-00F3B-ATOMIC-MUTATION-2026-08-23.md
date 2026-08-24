# SKCP-00F3B atomic mutation evidence

Card: `1f9ee2c9`

SKCoord now uses a bounded per-card advisory lock at
`coordination/locks/<sha256(card-id)>.lock`. It rejects path-like identifiers,
waits at most five seconds, and serializes dependency amendments and exact
claim releases across processes.

Eight concurrent identical additions produced one `add_dependency` event and
one changed result. Eight concurrent identical releases produced one logical
`unassign` plus backlog move and one changed result. Release failure injection
proved that a CardStore failure restores the legacy owner projection, while a
legacy save failure writes no CardStore release transition.

Verification:

```text
20 passed in 0.86s
ruff: All checks passed
```

The local `.skenv` editable SKCoord and SKCapstone packages were refreshed with
`pip install -e`. Installed `coord kanban --json` exposes `d0edbff1` on all
three gated cards. Installed read-only state confirms `d0edbff1` remains
incomplete, `d12b8951` remains open, and F4 `90a02b0e` remains incomplete. No
live claim probe was run. Rollback is
`pip install skcoord==0.1.32 skcapstone==0.15.49`, followed by the editable
activation command when requalification is required.

No candidate artifacts, production services, commits, pushes, or deployments
were changed.
