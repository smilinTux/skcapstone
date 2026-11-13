A CAB approval no longer authorizes an actuation unless it carries a verifying
signed human grant from a pinned approver key (card bada5c86). The vote stores the
signed CapAuth envelope it was cast under, and the ATLAS dispatcher, the governed
CMDB apply gate and the trustee rotate guard all re-verify it against
`~/.config/skcapstone/cab-approvers.json` (no pin file means no human approval;
`CAPAUTH_HOME` plays no part). Votes by the change's drafter, creator or managed-by
owner, the operator seat (`operator`, `atlas`) or a placeholder such as `human`
never qualify. Auto approval is derived from the change's own shape (standard, or
auto-normal), never from the fold's status, so none of this depends on the
installed skcoord release. `skoperator decide --approve` needs `--authorization`,
`--target` and `--scope`; the target and scope must name the decision's option,
and both are checked before the grant's nonce is consumed. `itil cab vote` and the
`itil_cab_vote` MCP tool refuse any vote (approve, reject or abstain) when no
CapAuth identity resolves, and refuse to replace a signed vote with an unsigned one.
