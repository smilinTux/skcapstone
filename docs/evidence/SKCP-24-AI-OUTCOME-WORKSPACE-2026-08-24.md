# SKCP-24 AI outcome workspace evidence

## Scope

Card `77d6bae0` adds a read-only AI outcome and economy workspace at
`/control-plane/ai`. It reuses the frozen protected `/api/v1/overview`
projection. It adds no API route, mutation, credential flow, deployment, or
external action.

The combined integration review used exact `origin/main`
`7299700a0e62e32d11756bde0dd578ec0acd5597`, pre-correction HEAD
`51618eda07bf19a8a8fd22e803cedf7bea4f4949`, and tree
`337ad8622e5e476dc2e0f2266b0dd809fbc14fc1`.

The implementation keeps these observations separate:

- `skcounter.harness` for harness-reported usage
- `skgateway.observed` for gateway-observed usage
- `skjoule.wallet` for SKJoule accounting state

No cross-lane total is calculated. Tokens, USD, SKJoules, latency, quality,
and value retain distinct labels and truth states.

## Acceptance evidence

1. Harness and gateway observations render in separate lane cards and are not
   summed. SKJoule renders in a third separate card.
2. Estimated cost displays distinct pricing revision and confidence fields.
   Both remain explicitly unavailable because the bounded overview does not
   project those inputs.
3. Accepted outcome, recommendation acceptance, verified effect, evaluation
   quality, citation coverage, rework, override, abstention, denial handling,
   budget, and cost per accepted outcome remain Unknown pending typed owner
   evidence.
4. The page reads only bounded aggregate overview observations. It does not
   request or render model content, identity material, detailed source records,
   or owner paths.
5. Model, client, provider, node, route, queue, cache, tool error, quality, and
   cost rows show the exact estate scope and their harness and gateway source
   provenance, including the metric registry version and hash. Cost is
   projected when present. Unsupported detail stays Unknown.

## Tests

Focused tests:

```text
python -m pytest tests/test_control_plane_ai_workspace.py \
  tests/test_control_plane_now_workspace.py \
  tests/test_control_plane_project_workspace.py \
  tests/test_control_plane_schedule_explorer.py \
  tests/test_control_plane_architecture_workspace.py \
  tests/test_dashboard_link_accessibility.py \
  tests/test_session_adapter.py -q
35 passed in 4.65s
```

Full repository tests:

```text
python -m pytest tests/ -q
491 passed, 6 warnings in 32.64s

ruff check src/ tests/
All checks passed!

git diff --check
PASS
```

The six warnings are existing `jsonschema.RefResolver` deprecation warnings.

## Real Chrome CDP

Google Chrome `151.0.7922.108` loaded the synthetic bounded overview through
the protected read route and proved:

- three separate lane cards
- eleven outcome and evaluation rows, including explicit Unknown budget
- ten operational drilldown rows
- three provenance rows
- metric registry version `1.0.0` and exact hash provenance
- 1200 harness tokens and estimated 18 USD
- 1260 gateway tokens with unavailable cost
- 420 separate SKJoules
- keyboard traversal, Enter-open, Escape-close, and trigger focus return
- accessible heading and drilldown button names
- minimum computed contrast `6.60:1` in light mode and `8.63:1` in dark mode
- reduced-motion emulation and no horizontal overflow at 390 or 320 CSS pixels
- immediate protected-DOM purge on delayed load, HTTP 401, HTTP 403, and an
  invalid popstate, with stale response repaint blocked
- 15 GET requests, zero non-GET requests, zero external requests, and zero
  runtime exceptions

Evidence artifacts:

```text
/tmp/skcp24-ai-repin-v170.json
sha256:d68997dc4d6e2ec55898489b2675afa3fd5cc86fafc0506e6ae8fd36170842db

/tmp/skcp24-ai-outcomes-rereview.png
sha256:1706b313297880d285360b0ce5ac0d038928a840a3b5cefb972b2367efe47ed5
```

## Known limitations

The frozen overview currently projects lane totals, cost state, collector
coverage, and SKJoule wallet totals. It does not project accepted outcomes,
verified effects, evaluation results, pricing revision, cost confidence,
budget, latency, cache, detailed model or route dimensions, or cost per
accepted outcome. The workspace exposes those gaps instead of inferring them.

## Rollback

No data changed. Rollback removes the AI page route, its three static assets,
the four navigation links, the focused test, this evidence record, and the
changelog entry. The frozen API and all owner sources remain unchanged.
