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
CapAuth identity resolves, and refuse to replace a signed vote with an unsigned
one. A vote counts only for the change being gated and the voter its file names
(`cab_approval.votes_for`), so a vote file copied under another change's or
voter's name authorizes nothing. Approval age is measured from the grant's signed
`issued_at`, not the vote file's `decided_at`, and the CMDB apply gate and the
trustee guard now apply it too (a day, the dispatcher's default). A signed
decision cannot be rolled back: `record_signed_vote` refuses a grant unless its
signed `issued_at` is strictly later than the voter's signed vote already held,
and every gate counts, per voter, only the signed vote with the latest
`issued_at` (`cab_approval.current_votes`). Voter names are stripped and
compared case folded, so one human has one vote slot. `docs/ATLAS_EXECUTION_SAFETY.md` gains the arming runbook
(Chef writes and reads back the pin, open changes are re-voted) and states the
trust limit of the pin on a node where everything runs as one sudo-capable user.
