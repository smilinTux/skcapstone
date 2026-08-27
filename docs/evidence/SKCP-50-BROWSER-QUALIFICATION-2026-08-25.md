# SKCP-50 browser qualification

Card: `83a8c40b`

Verdict: **BLOCKED**

## Scope exercised

A fresh local Chrome 146 CDP run exercised Now, Portfolio, Reliability, Architecture, AI outcomes, Governance, and Reports. It checked native and accessibility tree main and navigation landmarks, first Tab focus visibility and name, runtime exceptions, external requests, and non-GET requests. Separate existing qualifiers were attempted for Now, Portfolio, Reliability, Architecture, AI outcomes, Governance, Reports, authorization preview, and data quality.

## Passing evidence

The seven-route landmark matrix passed in real headless Chrome:

- 7 of 7 routes had exactly one DOM main landmark and one accessibility tree main landmark.
- 7 of 7 routes had navigation landmarks in both DOM and accessibility tree.
- 7 of 7 routes exposed visible first Tab focus named `Now`.
- 0 non-GET requests, 0 external HTTP requests, and 0 runtime exceptions were observed.
- AI outcomes passed its existing keyboard, focus return, accessible name, contrast, reduced motion, responsive 390 px and 320 px, purge, stale response, and no-write checks.
- Authorization preview passed its fail-closed URL, declared state, explicit trigger, no-write, no-external-request, and runtime exception checks.

## Blocking evidence

This run cannot truthfully satisfy the full acceptance criteria:

1. The Now and data-quality fixture requests returned HTTP 403, so their common-task, truth-state, contrast, responsive, and evidence-drill assertions did not execute to completion.
2. Portfolio, Reliability, Architecture, Governance, and Reports could not create their bearer or report fixtures. Reliability identified `ModuleNotFoundError: No module named 'pytest'` in the active Python environment. The other fixture startup failures are retained without inferring a cause.
3. A complete approved mobile, tablet, desktop, 200 percent zoom, and reduced-motion screenshot matrix was not produced or reviewed.
4. Complete manual screen-reader evidence and WCAG 2.2 AA automated evidence were not produced.
5. Common-task elapsed-time and interaction-count evidence for every qualified task was not produced. Therefore exception-to-evidence within two interactions and KPI-to-contributors within one interaction are not established across the required surface set.
6. The generated Now desktop image proves only the landmark run. It is not a reviewed visual-regression matrix.

No verdict is inferred from card lifecycle state or evidence links. The BLOCKED verdict follows from the explicit failed and missing qualification evidence above.

## Artifacts

Artifacts are under `docs/evidence/artifacts/SKCP-50-2026-08-25/`:

- `accessibility-landmark-matrix.json`
- `now-desktop.png`
- `accessibility.log`
- `qualifier-summary.tsv`
- one log for each attempted surface qualifier
- `qualification-result.json`
- `SHA256SUMS`

## Safety and limitations

The work used only local Uvicorn and headless Chrome child processes. The qualifier terminated its children. No commit, push, merge, deployment, restart, live gateway or configuration mutation, credential disclosure, cleanup, WAKE-02 enablement, live execution, automerge, human signoff, or repository visibility change occurred.
