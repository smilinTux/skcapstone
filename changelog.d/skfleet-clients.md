`skcapstone fleet clients` (skfleet-clients) keeps pi, opencode, codex and claude at the
same pinned versions on every chi host, launched from `~/.local/bin` shims, with the
gateway provider blocks of their configs rendered from skgateway's live `/v1/models`. Data
lives in skgateway-config-chi `clients/`. One job on chiap08 (`skfleet-clients.timer`, 5
min) rolls changes canary first, halting at the first failed gate with ROLLED/HALTED
skmail; `skfleet-clients-upstream.timer` (daily) smokes newer releases on the canary and
opens pin-bump PRs. The `pi` wrapper skips its launch-time gateway sync on hosts the job
manages. Design: docs/fleet/CLIENTS.md.
