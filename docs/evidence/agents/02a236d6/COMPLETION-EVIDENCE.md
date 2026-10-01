# Provider breadth qualification

Card: `02a236d6`. Producer: `codex-provider-breadth-02a236d6`.

The added route is `qwen`, using explicit Pi provider `skgw-qwen` and model
`qwen3.8-27b-huihui-abliterated-q4_k_m`. Actual gateway responses identify
provider `local` and backend `chiap08-qwen38`. These transport names are never
accepted as independently qualified model families. Existing DeepSeek, Zai,
and Codex routes remain available. No provider was called directly.

## Source and behavior

- `fiber_routes.py` owns the qualified routes, actual gateway
  capacity-domain names, canonical independence check, and additive Qwen
  catalog preparation. It contains no concurrency limits or credentials.
- `fiber_worker.py` and `fiber_admission.py` use the same qualified families.
- `fiber_control.py` and the host side of `fiber_gateway_bridge.py` require
  the qualified Qwen backend and exact requested/served model. Completion
  receipts retain semantic family, actual provider/backend, and revision.
- `fiber_probe.py` recognizes the canonical Qwen route, including underscores
  in its model ID. `fiber_selection.py` selects it only for explicitly pinned
  `qwen-suitable` public source cards. Existing protected/local-only source
  exclusions are preserved.
- `fiber_lifecycle.py` accepts canonical qualified producers. The native
  handoff regression verifies Qwen implementation receives one DeepSeek
  review of the exact candidate, with no duplicate generation on replay.
- The sandbox mounts the bridge script alone. Its host-only route-helper
  import is deliberately local to the host handler. A standalone import
  regression reproduced the initial missing-module failure and verifies
  the corrected boundary without mounting additional host files.
- The node probe is streamed to remote Python over stdin. It retains its
  standalone public family map rather than importing host modules. A separate
  isolated-import regression reproduced and corrected this dependency edge.

This additive commit follows the bridge chain through `cc8e92823ef2b3fce51af32e00c839a78d0c4dce`
and the scheduling change `870a5e089e8b529e77a25c6114d11d241f339fc0`
(cherry-picked locally as `c9ec28c7010757ff49aaac286ff56e57d926ba90`).
The integration owner should cherry-pick only this card's final commit after
those prerequisites, preserving subsequent scheduling/test-plane changes.

## Live qualification

All successful checks used a public arithmetic tool schema, validated its
arguments, executed addition locally, supplied the result, and required the
final answer `42`. Credentials stayed inside the existing agent catalog
boundary. Successful qualification retained the gateway Git revision both
before and after the two calls and required them to match.

| Family | Requested model | Outcome | Reported total tokens | Elapsed |
| --- | --- | --- | --- | --- |
| Qwen | canonical model above | Two calls PASS | 724 | 1.722 seconds |
| DeepSeek | `deepseek-flash` | Two calls PASS | 835 | 2.174 seconds |
| Zai | `sk-zai-m`, served `glm-5.3` | Two calls PASS | 318 | 8.070 seconds |
| Codex | `gpt-5.6-sol` | Two calls PASS | 204 | 6.388 seconds |
| Kimi | `kimi-for-coding` and `k3` | Upstream authentication 401 | Unavailable | Unqualified |

These are provider-reported qualification counts, not billing or throughput
savings. The first DeepSeek probe forced a tool choice incompatible with its
thinking mode and received 400. Its successful repeat used the actual Pi-style
automatic tool choice. Earlier failed probes remain retained.

Operator evidence under `~/.skcapstone/evidence/work/02a236d6/`:

- `qualification-qwen-1790823198880970858.json`
- `qualification-deepseek-1790823289001871620.json`
- `qualification-zai-1790823205215465728.json`
- `qualification-codex-1790823203588242577.json`
- `qualification-kimi-1790823288184718295.json`
- `final-suite-standalone-fixed.log`

## Kimi disable

The user confirmed there is no active Kimi subscription and requested its
disable switch. The exact pending login process was stopped; an authoritative
remote process check found no remaining Kimi global-login process. No token
was obtained and existing credentials were preserved.

All eight node catalogs were inspected. Only chiap04 and ziowk01 retained Kimi
entries. The four exact model IDs `kimi-for-coding`,
`kimi-for-coding-highspeed`, `k3`, and `k3-256k` were removed from their
existing `skgateway` provider. Every credential field and every other provider
remained equal. New catalog files and private preimages have mode 0600.
The gateway operator owns disabling the two actual upstream backend entries;
this card does not claim that separate gateway mutation as its own work.

Each changed node retains sanitized evidence at
`~/.local/state/skfleet-fiber/evidence/02a236d6/KIMI-CATALOG-DISABLED.json`
and its private original catalog at
`~/.pi/agent/models.before-02a236d6-kimi-disable.json`.

## Validation and deployment boundary

The suite covers real two-hop HTTP attribution and backpressure, standalone
sandbox-script import, canonical review independence, exact source handoff,
catalog preservation, explicit selection, and actual gateway capacity-domain
use. Final exact-source suite:

```text
/home/skuser01/.skenv/bin/python -m pytest -q ops/fiber-f1be0929/tests tests/test_fiber_lifecycle_finish.py tests/test_fiber_lifecycle_recovery.py tests/test_fiber_lifecycle_connection.py
371 passed, 3 skipped in 40.98 seconds
```

The three existing conditional skips require a qualified local sandbox or
remote resource check. Additionally, this exact source's streamed node probe
ran successfully against all eight fleet nodes, including gateway-only
chiap01, and every returned host identity and gateway probe matched.

Qwen runtime and catalog deployment require independent review of the exact
composed candidate. Install `fiber_routes.py` beside the host modules on every
affected worker and authority. Prepare each host catalog with
`fiber_routes.qwen_catalog` using that host's existing gateway credential
binding; preserve a private preimage and atomically replace only unchanged
input bytes. Never copy credential material between hosts or into model
workspaces. The private sandbox bridge needs no new mounted module.

Rollback the new source modules to their exact previous installed hashes and
restore unchanged private catalog preimages. Kimi remains intentionally
disabled unless the user later authorizes enabling a subscribed route.

Large-context coding quality, sustained fleet throughput, and the full
automatic production cycle are not established by the arithmetic probe.
The parent rollout owns those qualifications. No protected Matter data,
external legal action, repository push, or merge occurred.
