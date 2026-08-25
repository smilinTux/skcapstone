# SKCP-50A main landmark remediation - PASS

Card: `e404f0b0`

Verdict: **PASS.** All exact acceptance criteria pass locally at unchanged revision `9b2cbff7204d3e76a69f4bf21e9bf1de8ec04ef5` plus the uncommitted working-tree patch.

## Repair

The seven route templates already share `.wrap` layout and the navigation/primary-content sibling pattern. Each now has one native `<main>` around only its primary workspace content; navigation and body-level dialogs remain outside it. No CSS or JavaScript changed, so native `<main>` retains the previous block layout without visual styling changes.

Routes and templates:

- `/control-plane/now` - `src/skdashboard/static/overview.html`
- `/control-plane/portfolio` - `src/skdashboard/static/projects.html`
- `/control-plane/reliability` - `src/skdashboard/static/reliability.html`
- `/control-plane/architecture` - `src/skdashboard/static/architecture.html`
- `/control-plane/ai` - `src/skdashboard/static/ai.html`
- `/control-plane/governance` - `src/skdashboard/static/governance.html`
- `/control-plane/reports` - `src/skdashboard/static/reports.html`

The render seam remains `src/skdashboard/dashboard.py::_page`, with those routes declared at lines 1869-1876. Focused regression coverage is `tests/test_control_plane_landmarks.py`; the reused Chrome CDP reproducer is `scripts/qualify_control_plane_accessibility_cdp.mjs`.

## Source qualification

The exact `83a8c40b` evidence was read from its isolated worktree. Its failing matrix hash was independently verified as:

`sha256:17352eec29a23df682009093650794f4763436e855fb11e07e4894310a2e5013`

That matrix recorded `mainLandmarks=0` and `axMain=0` on 7/7 routes. Its source report is `docs/evidence/SKCP-50-BROWSER-ACCESSIBILITY-QUALIFICATION-2026-08-24.md` in `/home/skuser01/worktrees/skdashboard-83a8c40b-browser-qual`.

## Fresh evidence

- `docs/evidence/artifacts/SKCP-50A-2026-08-25/accessibility-landmark-matrix.json`
  - PASS; 7/7 routes have exactly `mainLandmarks=1` and `axMain=1`.
  - Every route retains one DOM/AX navigation landmark and visible first-Tab focus.
  - 0 non-GET requests, 0 external HTTP requests, 0 runtime exceptions.
  - SHA-256 `55503df1ae88313a79b13b198bc757479098456bed77326a71f8c0de10b1452a`.
- `docs/evidence/artifacts/SKCP-50A-2026-08-25/now-desktop.png`
  - Local public-synthetic/unprivileged screenshot; no protected response or credential.
  - SHA-256 `54acb8cb6bc47668307040e29bfa7e08880fa70bb82d24d352f896a61c21c2e0`.
- `docs/evidence/artifacts/SKCP-50A-2026-08-25/SHA256SUMS`
- `docs/evidence/artifacts/SKCP-50A-2026-08-25/timing.tsv`

A pixel comparison against the source qualification screenshot found only 47 antialiased pixels in the existing animated live-status dot (`bbox=(23,946,30,953)`) out of 1,440,000 pixels. No layout geometry or content changed; no CSS changed.

## Checks

Corrected environment: `PYTHONPATH=$HOME/work/capauth/src:$HOME/work/skcoord/src:$HOME/work/skcapstone/src:$PWD/src` for bootstrap, narrowed to `$HOME/work/capauth/src:$PWD/src` for the hermetic app browser run.

- Focused DOM + real Chrome regression: `2 passed`.
- Targeted existing browser/workspace suite: `34 passed, 2 pre-existing jsonschema deprecation warnings`.
- Existing direct Chrome lanes: Reliability, Architecture, Governance, Reports, and authorization preview all PASS.
- Existing 11-surface navigation contrast lane: `6 passed`.
- `python -m ruff check .`: PASS.
- `python -m ruff format --check tests/test_control_plane_landmarks.py`: PASS.
- `git diff --check`: PASS.

Logs are retained under `docs/evidence/artifacts/SKCP-50A-2026-08-25/`.

## Execution identity and safety

- Model route: `PI_PROVIDER=skgateway`, `PI_MODEL=sk-codex`, `PI_REASONING_LEVEL=high`.
- Worktree/cwd: `/home/skuser01/worktrees/skdashboard-e404f0b0-landmarks`.
- Branch: `pi/e404f0b0-landmarks`.
- tmux: `sk-parallel-farm-20260824`, window `0:skdashboard`, pane `%14`.
- Startup log: `/home/skuser01/.skcapstone/runtime/sk-parallel-farm-20260824-pane-14.log`.
- Corrected bootstrap log copy: `docs/evidence/artifacts/SKCP-50A-2026-08-25/bootstrap.log`.
- No applicable `AGENTS.md` exists in this repository or its ancestor path.

No deployment, service restart, protected-data access, dependency addition, commit, merge, push, other-worktree mutation, or cleanup occurred. Local Uvicorn and headless Chrome children were terminated by each qualifier.

## Limitations and rollback

This closes only the exact seven-page main-landmark blocker. It does not claim or rerun the full SKCP-50 mobile/tablet/desktop/zoom/reduced-motion/common-task-time qualification. The Now/Portfolio/AI historical qualifier contamination limitation remains outside this card; this card's landmark reproducer is hermetic under the corrected CapAuth path.

Rollback before any commit: restore the seven listed HTML files and delete `tests/test_control_plane_landmarks.py`, `scripts/qualify_control_plane_accessibility_cdp.mjs`, this report, and `docs/evidence/artifacts/SKCP-50A-2026-08-25/`. No migration or runtime rollback is required.
