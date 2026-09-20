Fix governed review cards dispatching with the bare bucket alias (`sk-s`)
instead of the operator's configured reviewer model.

`skfleet-rotate.py` resolves `model=_lane_model(_LANE,fresh_claimability["core"])
or _bucket` for every card before deciding whether it is a governed review.
For a governed review card only, the very next lines reset it back:
`if _review_seat is not None: model=_bucket`. The comment beside that reset
argued `eligible_review_routes`/`choose_review_route`, called a few lines
later, had already made a better-informed model selection than the lane's
generic resolution, so overriding back to the bucket preserved it. That
reasoning does not hold: those two functions choose a `capacity_domain` for
admission and occupancy bookkeeping, never the `model` string sent to the
gateway. The reset discarded the operator's configured reviewer model and
sent every governed review card to the shared `sk-s` pool alias instead,
same defect as the producer-dispatch bug fixed on 2026-09-18, just
reintroduced one call site later inside the review branch itself.

Measured 2026-08-31: a drop-in setting `SKFLEET_MODEL_S=sk-codex-mid` for
the Seraph seat was confirmed loaded into the running service
(`systemctl --user show` at the 14:25:10 CDT restart), and governed review
workers launched afterward still sent `"model":"sk-s"` on every turn. `sk-s`
round-robins across backends including `chiap08-qwen38`, the only erroring
backend on the gateway that day (80 errors of 3,943 requests, every other
backend zero). A review turn landing there returned an empty completion,
the reviewer recorded no verdict, and the wrapper exited 75
`no_card_mutation`; cards accumulated 17 claims and 18 releases this way.
In three sessions that did succeed, the turn that actually produced the
correct verdict command was served by `gpt-5.6-luna`, not the pool alias,
so success was pool luck rather than reviewer competence.

The fix removes the reset: a governed review card now keeps the same
`model` the non-governed path already resolves, and the capacity_domain
chosen by `choose_review_route` (fed from the review-specific
`eligible_review_routes`) is unaffected and still lands in
`_route_identity["capacity_domains"]` exactly as before. The misleading
comment is rewritten to record the corrected understanding and the
measurement above. `tests/fleet/test_lane_model_routing.py` previously
contained a test that asserted the buggy reset as intended behavior; it is
replaced with tests covering the resolved model reaching a governed review
card, the capacity domain surviving unchanged, the producer path being
unaffected, and an unconfigured card still resolving to a sane, non-empty
default.


`tests/test_skfleet_seraph_selector_e2e.py::test_real_selector_runs_distinct_heads_concurrently_and_blocks_duplicates`
caught a tempting-but-wrong alternative along the way: reading the dispatched
model back off `_selected_route` (`model=str(_selected_route["model_or_bucket"])`)
instead of simply leaving `model` alone. That was tried, and the live
subprocess test failed for a real reason: `eligible_review_routes` accepts
any route sized AT LEAST the card's required size, so two same-size [S]
review cards routinely resolve to two DIFFERENT concrete routes on two
different capacity_domains. Reading `model` back off `_selected_route` would
make two concurrent same-size review cards request two different models
depending only on which route each happened to reserve, silently defeating
an operator's per-size configuration for exactly the cards that raced each
other for capacity. It is also the exact pattern the 2026-09-18
producer-dispatch fix (95c04b06) deliberately removed and pinned against in
`test_skfleet_logical_routes.py` and `test_skfleet_pool_v2_authority.py`, for
the same reason `_selected_route` only picks a `capacity_domain` for local
oversubscription bookkeeping; `resolve_and_preflight`, called on `model`
separately, is the actual authority on whether a requested model is
currently advertised and healthy. That alternative was reverted in favour of
the simpler fix described above.

The e2e test itself needed one unrelated line removed:
`SKFLEET_CODEX_MODEL_S=sk-codex-mid` in its subprocess env, a model its mock
gateway never advertises at any size. It was inert while the review branch
still hardcoded the bare bucket regardless of any override, and only turned
into a real (and correct) `ROUTE_PREFLIGHT_BLOCKED`/no-launch failure once
the branch started honouring it, since nothing in this specific mock's
catalog can serve `sk-codex-mid`. The test's own second scenario
(`test_generic_niobe_launches_seraph_review_through_codex`) never set that
variable, confirming it was disconnected boilerplate rather than a
deliberate scenario; removed with a comment explaining why, no assertions
changed.
