# PR 324 independent fleet rereview

Card: `f6a93d20`
Parent: `ec108b74`
Review verdict: **FAIL**

## Pinned candidate

- PR: https://github.com/smilinTux/skcapstone/pull/324
- State observed: open
- Head: `737197eec64a1ab8e2323454d223d50355266310`
- Head tree: `2f55b3c46af7543fa15818c94b9c00461a7d8f14`
- Base observed: `f38e20799f06d4b76bdc867b0ad9744d8e35f027`
- Head ref: `feat/ec108b74-replace-editable-installs`
- GitHub API diff SHA256: `5260f3677e07bdaa68f1d23baeffa738ab20ab6502866f0e4d26facdf1a16529`
- Exact PR paths: `docs/evidence/chatgpt-client/safe-restart-procedure.md`, `docs/evidence/chatgpt-client/terminal-closure-regression-root-cause.md`, `docs/fleet/editable-install-inventory-pre.json`, `docs/fleet/editable-install-inventory.md`, `docs/runbooks/chatgpt-codex-sk-client.md`, `scripts/pip-editable-guard.sh`, `src/skcapstone/fleet/editable_guard.py`, `src/skcapstone/sdk.py`, `src/skcapstone/service_health.py`, `tests/test_service_health_incidents.py`, and `tests/test_service_registry.py`.

The candidate PR was not changed. `pr-324.diff`, the API response, and the API files response preserve its exact observed bytes and metadata.

## Producer evidence reconciliation

The producer verdict SHA256 is `359f5602594b73018eb03fb62ea85e21a3a708ed58d75dbf527ff22c1d9e2221`. Its inventory SHA256 is `db1d0032d1a2ac5f2673927c811ecf71c4d7f533edf1f68c2cb79a54adc2279b`. Its report SHA256 is `2804a81565325ec8314db7dc3d97371a7649f625a348a3d5fa366a6e1e7aaa67`.

The producer counted only shared `~/.skenv` package inventories and classified one package as service imported. That conclusion is false. The same shared interpreter runs SKComms on all five hosts, not only chiap04. Separate active service interpreters add SKHarness, SKVoice, and 21 Liberty packages. The confirmed minimum is therefore 5 + 1 + 1 + 21 = **28 service-imported editable packages**.

## Active service runtime inventory

| Host | Unit or launch owner | Interpreter | Service-imported editables |
| --- | --- | --- | ---: |
| chiap01 | `skcomms.service` | `~/.skenv/bin/python` | 1 SKComms |
| chiap02 | `skcomms.service` | `~/.skenv/bin/python` | 1 SKComms |
| chiap03 | `skcomms.service` | `~/.skenv/bin/python` | 1 SKComms |
| chiap04 | `skcomms.service` | `~/.skenv/bin/python` | 1 SKComms |
| chiap08 | long-lived GNOME Terminal scope | `~/.skenv/bin/python` | 1 SKComms |
| chiap01 | `skcode-hostd.service` | `~/.venvs/skops/bin/python` | 1 SKHarness |
| chiap01 | `skvoice.service` | `~/skvoice-env/bin/python` | 1 SKVoice |
| chiap08 | `sklegal-liberty-api.service`, `sklegal-liberty-web.service` | `~/work/sklegal-liberty-live/.venv/bin/python` | 21 Liberty packages |

The Liberty set is capauth plus 20 `sklegal-*` distributions. The JSON verdict names all 28 entries. `live-interpreter-inventory.json` records every interpreter examined and its full editable list. `service-unit-inventory.json` records every active user unit examined, unit FragmentPath, ExecStart, MainPID, process command, cgroup, and import origins. SKComms import resolution on every host points into `~/work/skcomms`. SKHarness resolves into `~/.local/opt/skharness-5bcecb3b/src`. Liberty resolves into `~/work/sklegal-liberty-live/services/api/src`.

Shared `~/.skenv` also contains non-service or duplicate editable entries. Those do not reduce the service-imported minimum. Isolated approved dashboard, vLLM, Claude API, HammerTime embedding, and system Python interpreters were examined and had zero editables in the captured inventory where pip was available.

## Guard review

The guard fails the required mechanism test.

1. Direct invocation of `~/.skenv/bin/pip install -e ...` bypasses `~/.local/bin/pip` completely.
2. Direct invocation of `~/.skenv/bin/python -m pip install -e ...` also bypasses the wrapper.
3. `SKIP_EDITABLE_GUARD=1` is an explicit documented bypass with no authorization check.
4. PR 324 does not install the wrapper or integrate the Python helper with pip, service launchers, or fleet launchers.
5. The wrapper delegates through `/usr/bin/env pip`, which can resolve back to the wrapper when installed first on PATH as documented.
6. The Python helper knows only `~/.skenv`, omitting the active SKHarness, SKVoice, and Liberty service venvs.
7. Its intended private venv distinction is reasonable in isolation, but no pip invocation enforces it.

## Non-Python mutable runtime exclusions

GNOME, PipeWire, portal, GVFS, tracker, gpg-agent, RustDesk, and similar processes are not Python SK package runtimes. The vLLM runtime is Python but has zero editables and loads model artifacts rather than these editable SK packages. These were excluded from the count, not silently treated as clean service dependencies.

## Conclusion

**FAIL**. PR 324 understates the live service exposure by at least 27 packages, does not cover the active service interpreters, and provides a guard that is both bypassable by direct shared pip invocation and explicitly bypassable by environment variable. No source, PR 324, service, process, install, credential, deployment, cleanup, merge, or live configuration was mutated during this review.
