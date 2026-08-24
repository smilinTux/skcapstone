# SKCP-13 live Now workspace evidence

Card: `c6828b8a`
Date: 2026-08-24
Base revision: `f6f11ff8c71bc7e13776ed62024b8219d95c377b`

## Outcome

The existing SKDashboard Overview is now the live breadth-first `Now`
workspace at `/control-plane/now`. It reuses protected read-only
`GET /api/v1/overview` and groups its 16 bounded adapters into the 12 approved
estate silos. No framework, package, write route, metric calculation, AI
conclusion, recommendation, deployment, or runtime activation was added.

Missing decision, baseline, calculated metric, and typed AI proposal data is
shown as unavailable, Unknown, or abstained. It is never inferred from task or
aggregate state. SKLegal policy-filtered visibility remains visible without
protected Tenant or Matter retrieval.

## Acceptance evidence

1. All 12 rows render only when all 16 declared adapter observations are
   present. Each row shows owner, truth state, high-level source aggregate,
   metric definition version, exact registry source, estate scope,
   latest-source window, per-adapter population and denominator, Unknown
   material change, and a one-click evidence dialog. Disjoint populations are
   never added together.
2. The evidence dialog shows adapter version, observation time, watermark,
   safe errors, visibility, scope, sample, and unresolved uncertainty. It is
   read-only and restores keyboard focus when closed.
3. The AI brief abstains because the read envelope has no typed insight or
   recommendation. Its boundary lists the required evidence, practice,
   confidence, uncertainty, counter-indicator, alternative, impact, risk, and
   precondition fields and exposes no authorization or dispatch control.
4. The URL preserves the supported role and canonicalizes unsupported context
   to `scope=estate`, `window=latest`, `baseline=none`, and `service=all`.
   Deeper scope and time filtering remains gated on SKCP-20.
5. Missing or unauthorized protected evidence fails closed. The UI renders no
   estate rows, removes legacy green health claims, closes protected dialogs,
   purges prior metric and watermark DOM, and states that no silo is assumed
   healthy.
6. Real-browser computed contrast is at least 5.83:1 across new normal text,
   buttons, and truth badges. The qualifier proves sensitivity by restoring
   the former accent and observing a result below 4.5:1.

## Independent FAIL repair

Independent review of staged tree `bb918ec9ae3f5dc2a6ea65f42d401748341f017e`
returned FAIL before any commit or push. The repair preserves each adapter's
population and denominator, binds every displayed metric source to the metric
registry, identifies the Economy metric's separate `skcounter.harness` source,
purges protected DOM on live authorization loss, neutralizes legacy health,
and raises contrast above the WCAG AA normal-text threshold. Each defect has a
sensitive static or real-browser check.

## Tests and exact results

```text
python -m pytest tests/test_control_plane_now_workspace.py tests/test_control_plane_quality.py tests/test_control_plane_full_estate_fixture.py -q
14 passed, 2 warnings in 0.25s

python -m pytest tests/ -q
395 passed, 145 warnings in 30.52s

ruff check src tests
All checks passed!

node scripts/qualify_control_plane_now_cdp.mjs
PASS, Chrome 151, 12 rows, 16 sources, per-population coverage PASS,
registry provenance PASS, keyboard evidence PASS, live auth-revocation purge
PASS, auth fail-closed PASS, minimum contrast 5.833:1, contrast sensitivity
PASS, 390 px and 320 px responsive PASS, reduced motion PASS, 0 non-GET
requests, 0 external HTTP requests, 0 runtime exceptions
```

The real-browser qualifier also captures
`/tmp/skcp-13-now-workspace.png` for local visual inspection. The screenshot is
not a committed product artifact.

## Files changed

- `src/skdashboard/dashboard.py`
- `src/skdashboard/static/overview.html`
- `src/skdashboard/static/js/overview.js`
- `src/skdashboard/static/css/overview.css`
- `tests/test_control_plane_now_workspace.py`
- `scripts/qualify_control_plane_now_cdp.mjs`
- `CHANGELOG.md`
- This evidence file

## Known limitations

- The current read API exposes source aggregates and data quality, not typed
  calculated metric results, baselines, decision queues, AI insights, or AI
  recommendations. The workspace labels each missing layer instead of
  simulating it.
- Scope, service, window, and baseline controls expose only the currently
  supported estate read. SKCP-20 owns deeper filters, saved views, and links.
- Legacy operational tiles remain below the new breadth-first workspace during
  migration.

## Migration and rollback

There is no data migration. Rollback removes the `/control-plane/now` alias and
the additive Now sections, tests, qualifier, and evidence while preserving the
existing Overview, protected read API, adapters, registry, fixture pack, and
quality strip.

No deployment, activation, restart, external action, protected Matter access,
HammerTime Inbox access, Atlas mutation, or shared-checkout update occurred.
