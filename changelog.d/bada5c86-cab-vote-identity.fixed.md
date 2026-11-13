An approval no longer authorizes ATLAS unless it is an authenticated human grant.
The dispatcher now acts on a CAB approval only when it carries a human approver
role, signer fingerprint and authorization id, and the voter is not the change's
drafter, creator, managed-by owner, the operator seat (`operator`, `atlas`) or a
placeholder such as `human`. This holds whatever skcoord release is installed.
`skoperator decide --approve` now needs `--authorization`, `--target` and
`--scope` (a grant from `skcapstone itil cab authorize`) and records the grant's
verified issuer instead of the running agent's identity. The `itil_cab_vote` MCP
tool refuses an approval when no CapAuth identity resolves, instead of recording
the free-text label (card bada5c86).
