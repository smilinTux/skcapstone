# Fleet model lane routing

Status: live on chiap01, chiap02, chiap03, chiap08 since 2026-09-03.
Regression tests: `tests/test_skfleet_glm_levels.py`.

## Lanes

The rotation dispatches each claimable card to one lane, cheapest first:

| Lane | Default model | Target source | Notes |
|------|---------------|---------------|-------|
| qwen | `SKFLEET_QWEN_MODEL` (default `qwen3.8-27b-huihui-abliterated-q4_k_m`) | `SKFLEET_QWEN_TARGET` | Local free tier. Heavy titles (production, release, migration, schema, architecture, `[HUMAN]`, `[XL]`) are excluded by `qwen_suitable`. |
| glm | `SKFLEET_GLM_MODEL` (default `sk-glm-s`), overridden per card by level routing below | `SKFLEET_GLM_TARGET` | One shared Z.ai connection serves the whole estate. Logical routes prevent provider fallback. |
| codex | `sk-codex` | `SKFLEET_TARGET` | Flat plan quota. |
| escalate | `SKFLEET_ESC_MODEL` (default `gpt-5.6-sol`) | `SKFLEET_ESC_TARGET` | Only capability-escalated cards. |

## GLM level routing

One z.ai connection backs every GLM worker, so the lane spends it deliberately.
The card size marker in the title selects the model level:

| Title marker | Model |
|--------------|-------|
| `[S]` | `sk-glm-s` |
| `[M]` | `sk-glm-m` |
| `[L]` | `sk-glm-l` |
| `[XL]` | `sk-glm-l` |
| no marker | lane default (`sk-glm-s`) |

A bracket only counts as a size marker when it is exactly `S`, `M`, `L`, or
`XL`; a title like `[SKLEGAL][S1-05B][L]` routes as `L`, never as `S`. Each
level is overridable per host with `SKFLEET_GLM_MODEL_S`, `SKFLEET_GLM_MODEL_M`,
`SKFLEET_GLM_MODEL_L`, and `SKFLEET_GLM_MODEL_XL`.

### Connection ceiling

The z.ai account rejects concurrent overshots: one upstream 429 puts the whole
backend into a 30 second cooldown, during which glm requests fall through to
backends that cannot serve them and return 404 to clients. Measured on
2026-08-25 the account served nine concurrent requests before the next one
429'd. The estate therefore farms out at most eight fleet GLM workers,
leaving two connection slots of headroom for probes and non-fleet use:

```
chiap01 SKFLEET_GLM_TARGET=3
chiap02 SKFLEET_GLM_TARGET=3
chiap03 SKFLEET_GLM_TARGET=2
chiap04 SKFLEET_GLM_TARGET=0
chiap08 SKFLEET_GLM_TARGET=0
```

## Skeleton configurations

These are the minimal fragments each layer needs so a new host or a new model
route works on the first dispatch. They contain no secrets.

### skgateway (z.ai backend stanza)

The gateway discovers the GLM ladder from the account, so the backend stanza
pins only transport and the concurrency fence:

```yaml
backends:
  zcode:
    auth_type: zcode_oauth        # credentials live outside the config tree
    discovery: zai
    models: []                    # the ladder is discovered, not pinned
    priority: 2
    timeout_ms: 300000
    concurrency:
      zai:
        max: 10                   # account ceiling; fleet uses 8
        maxQueue: 400
```

### pi (`~/.pi/agent/models.json`)

Pi's local SKGateway catalog is a cache of the gateway's currently advertised
`/v1/models` inventory. Workers and interactive Herdr Pi sessions must only
select routes that the live gateway still advertises. A stale served-model name
left in `models.json` or `settings.json` `defaultModel` after a gateway
revision or inventory change fails closed with HTTP 404 `unknown_model` before
claim.

`skfleet-pi-model-catalog.py --apply` owns that boundary. It reads
`SKFLEET_GATEWAY_URL` (no hardcoded host target), replaces
`providers.skgateway.models` with currently advertised routes, records the
gateway revision and inventory fingerprint under `skfleet_catalog_sync`, and
invalidates removed served-model entries when either changes. If
`defaultModel` is absent from the current inventory, it falls back across
policy-compatible logical size capacity (`sk-s` → `sk-m` → `sk-l` → `sk-xl`)
without naming concrete served models in the product contract. Unrelated
provider fields are preserved. The catalog and settings files must be
current-user mode-0600 regular files; writes are atomic. Run without `--apply`
as the post-install drift check.

#### Five-host installation contract

Catalog sync is ordered and fail closed on every host. It is not valid to
install a launcher that can select a route before that host's Pi catalog has
been reconciled against currently advertised inventory.

The fresh preflight must record the full SHA256, owner, and mode of both the
host-local Pi catalog and installed launcher. The observed catalog mode
baseline is `0664` on chiap01, chiap02, chiap03, and chiap08, and `0600` on
chiap04. The launcher baseline on chiap02 is distinct from the common launcher
baseline on the other four hosts. Deployment evidence must preserve each full
per-host hash rather than treating either launcher baseline as estate-wide.

For each host, the installer must perform these steps in order:

1. Preserve the exact catalog bytes and original mode in host-local rollback
   custody without reading, logging, or copying opaque fields between hosts.
2. If the owned regular catalog is mode `0664`, atomically replace it with the
   exact same bytes at mode `0600`, then verify that its SHA256 is unchanged.
   Any other insecure mode, symlink, wrong owner, byte drift, or verification
   failure stops installation on that host.
3. Invoke `skfleet-pi-model-catalog.py --apply` and verify a subsequent
   read-only invocation reports the catalog current. The reconciler keeps only
   currently advertised gateway routes, invalidates stale served-model names
   after gateway revision or inventory changes, and repairs a stale
   `defaultModel` onto policy-compatible size capacity.
4. Only after reconciliation succeeds, install or activate the alias-selecting
   launcher and verify its exact host-specific expected hash.

Rollback restores that host's exact catalog bytes and original mode, plus its
exact prior launcher bytes. It must not substitute the chiap02 launcher
baseline with the baseline observed on another host. The reconciler remains
strict: it accepts only a current-user, mode-0600 regular file and never
normalizes unsafe input itself.

### opencode (`~/.config/opencode/opencode.jsonc`)

Same routes through the OpenAI-compatible adapter, for interactive seats:

```jsonc
{
  "provider": {
    "skgateway": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "SKGateway chiap01",
      "options": { "baseURL": "http://chiap01:18790/v1" },
      "models": {
        "glm-4.6": { "name": "GLM-4.6", "reasoning": true, "tool_call": true },
        "glm-4.7": { "name": "GLM-4.7", "reasoning": true, "tool_call": true },
        "glm-5.3": { "name": "GLM-5.3", "reasoning": true, "tool_call": true }
      }
    }
  }
}
```

### systemd (`~/.config/systemd/user/skfleet-rotate.service`)

```ini
[Service]
Type=oneshot
Environment=SKFLEET_TARGET=8
Environment=SKFLEET_QWEN_TARGET=1
Environment=SKFLEET_QWEN_MODEL=qwen3.8-27b-huihui-abliterated-q4_k_m
Environment=SKFLEET_GLM_TARGET=3
# Optional per-level overrides; defaults are in the script
# Environment=SKFLEET_GLM_MODEL_L=sk-glm-l
# Environment=SKFLEET_GLM_MODEL_XL=sk-glm-l
Environment=SKFLEET_MAX_LAUNCH=8
ExecStart=%h/.skenv/bin/python3 %h/.skenv/bin/skfleet-rotate.py --go
```

## Related repairs landed with this routing (2026-09-03)

- Review dispatch replay fence: a `review_assignment_launch` receipt consumes
  its deterministic recommendation only while its exact claim revision is the
  live claim, so a worker that dies right after launch can be redispatched.
  Before this, one dead worker fenced its review card forever.
- Coordination digest: card event files may contain bare JSON string rows;
  `collect_board` now skips non-object rows instead of crashing the sort key
  every cycle.

Both behaviors are pinned by `tests/test_skfleet_glm_levels.py`.

## Kimi subscription backends (2026-09-03)

The Kimi Code subscription follows the codex pattern: the `kimi` CLI owns the
OAuth login on the gateway host, and SKGateway reads the credential file
read-only (`auth_type: kimi_oauth`, PR skgateway#108). Access tokens live
900 seconds, so `kimi-auth-keepalive.timer` (7 minute cadence) runs the CLI
on the gateway host to keep the file fresh; the gateway re-reads it on mtime
change and never refreshes or writes it.

The account enforces concurrency per model family, measured by stepped burst
ramp (exactly N requests succeed then the rest 429, five independent bursts):

| Family | Models | Account limit | Gateway cap |
|--------|--------|---------------|-------------|
| coding | kimi-for-coding, kimi-for-coding-highspeed | 30 | 28 |
| k3 | k3, k3-256k | 16 | 14 |

Because the limits are per family, the subscription is declared as TWO
backend stanzas over the same endpoint and credential file, each with its own
capacity domain:

```yaml
  kimi:
    url: https://api.kimi.ai/coding/v1
    auth_type: kimi_oauth
    credentials_path: ~/.kimi-code/credentials/<env>.json
    models: [kimi-for-coding, kimi-for-coding-highspeed]
    concurrency:
      kimi-coding: { max: 28, maxQueue: 400 }
  kimi-k3:
    url: https://api.kimi.ai/coding/v1
    auth_type: kimi_oauth
    credentials_path: ~/.kimi-code/credentials/<env>.json
    models: [k3, k3-256k]
    concurrency:
      kimi-k3: { max: 14, maxQueue: 400 }
```

pi and opencode declare all four model ids under the skgateway provider
(`k3` carries a 1000000-token context window). Requesting a declared model
whose owning backend is in cooldown fails closed with 503
`model_owner_backend_down` instead of spraying to unrelated providers.
