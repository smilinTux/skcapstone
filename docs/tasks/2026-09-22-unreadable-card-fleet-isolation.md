# 38c4f1ee: isolate unreadable CardStore cards in fleet eligibility

Base: SKCapstone `v0.15.174` commit `5c50b01ca31b1954d1e53e753af62dd55ded12fd`.

The September 22 fleet rotation crashed in `leaf_eligibility_counts` when strict `CardStore.list_cards()` reached the malformed `38c4a706` writer chain. The installed CardStore already offers `list_cards(degrade_unreadable=True)`, which returns an explicit unreadable projection for that card while leaving `fold()` and the default strict `list_cards()` unchanged.

Change only fleet eligibility counting. Use the existing per-card projection, count an unreadable selected card as malformed, and exclude it from leaves and review work. A dependency on an unreadable projection cannot be complete. Healthy unrelated cards, including review leaves, remain countable. Do not make unreadable records claimable, weaken strict governance reads, or repair CardStore event files here.

Add a focused regression with one broken chain, an unreadable card dependent, and healthy independent leaf and review cards. Verify exact counts, strict `fold()` failure, and the explicit unreadable alert. Run the existing status filter tests. Source-only; no install or rotation until independent source review.

Add a short failure-path paragraph to `AGENTS.md` beside its existing coordination write boundary. A CLI or MCP failure must not trigger direct event-file writes, and a broken chain must preserve exact bytes and require reviewed mediated recovery. This adds no human approval gate.
