# Fleet + gateway settings registry

**One authoritative declaration per fact.** Every number below is declared in exactly
one place. This file restates the repo-side ones so CI can compare the two and fail
when either side moves alone, and records the estate-side ones as dated measurements
with the command that re-measures them.

If you are adding a setting, read [§ Where a new setting goes](#where-a-new-setting-goes)
at the bottom. It is nine lines.

---

## 1. Repo facts — CI-asserted, never hand-drifted

The value lives in code. The `value` column here is compared against the code by a
tier-3 assertion in `SOP.md`, by extracting the number from *both* sides. Editing
either one alone fails `docs / docs-check`.

| Fact | Authoritative declaration | Value |
|---|---|---|
| Builder dispatch ceiling for an untuned node | `BUILDER_CAPACITY` in `src/skcapstone/fleet/builder_dispatch.py` | **4** |
| Per-node override for that ceiling | **does not exist on `main`.** Lands as the node spec label `builder-capacity` in PR #805 | *(none today: every node takes the default above)* |
| Codex lane model default | `SKFLEET_CODEX_LANE_MODEL` fallback in `scripts/fleet/skfleet-rotate.py` | **sk-codex-mid** |
| Qwen lane session target default | `SKFLEET_QWEN_TARGET` fallback, same file | **6** |
| Kimi lane session target default | `SKFLEET_KIMI_TARGET` fallback, same file | **0** |
| Escalation lane model default | `SKFLEET_ESC_MODEL` fallback, same file | **gpt-5.6-sol** |
| Launches per rotation cycle | `SKFLEET_MAX_LAUNCH` fallback, same file | **11** |
| Package daemon port | `DEFAULT_PORT` in `src/skcapstone/__init__.py` | **9383** |
| Consciousness daemon port | `DEFAULT_PORT` in `src/skcapstone/daemon.py` | **7777** |

`SKFLEET_GATEWAY_URL`, `SKFLEET_TARGET` and `SKFLEET_GLM_TARGET` have **no default on
purpose**. The dispatcher exits rather than guessing. Do not give them one.

## 2. Estate facts — measured, not in this repo, not CI-reachable

CI cannot read a host it has no account on. These are therefore recorded as **dated
measurements**, each with the command that re-measures it. A number here is evidence of
what was true on that date, never a standing claim. Re-measure before you rely on one.

Each value appears **only here**. Tier 3 asserts that no other doc in the tree restates
them, which is the part that actually stops drift: a second copy is the thing that rots.

### The two gateways are different machines with different ports

Conflating them has cost real time twice. There is no shared pool.

| | chi fleet gateway | nor gateway |
|---|---|---|
| origin | `http://chiap01:18790` | `http://localhost:18780` |
| unit | `skgateway-codex.service` | `skgateway.service` |
| config | `chiap01:~/skgateway-codex/config/skgateway-codex.yaml` | `noroc2027:~/.skcapstone/gateway/skgateway.yaml` |
| serves | chiap01, chiap03, chiap08 rotate lanes | noroc2027 local inference, skchat, consciousness |

Measured 2026-09-19, `grep -n 'port:' <config>` and
`grep -rh SKFLEET_GATEWAY_URL ~/.config/systemd/user/` on each host. The chi hosts prove
which one they use: chiap01 sets `http://localhost:18790`, chiap03 and chiap08 set
`http://chiap01:18790`. **nvidia, ornith and openrouter are not backends on the chi
gateway at all.**

### chi gateway pool ceilings

Re-measure: `curl -s http://chiap01:18790/queue` (authoritative at runtime), or
`ssh chiap01 'grep -n -A2 "max:" ~/skgateway-codex/config/skgateway-codex.yaml'`.

| backend / domain | max | maxQueue |
|---|---|---|
| codex | 32 | 200 |
| zai | 10 | 400 |
| kimi-for-coding | 5 | 8 |
| kimi-k3 | 4 | 8 |
| chiap08-qwen38 | 2 | 8 |

These are the **gateway** caps. Kimi's *account-family* limits (a different fact,
measured by burst ramp) live in `docs/fleet/model-lane-routing.md`; do not read one for
the other.

Measured 2026-09-19. Total 53. `chiap01-qwen38` was removed 2026-09-19 (commented in
place for restore). A pool change needs a **full restart**, not SIGHUP: `getPool` is a
memoized singleton, so SIGHUP updates the config object and never rebuilds the ceilings.

⚠️ **The codex block's own leading rationale comment names a superseded ceiling**, left
behind from an intermediate step; its own trailing line records the step that actually
landed. The `max:` **value** is correct and authoritative. Do not reason from the
comment. Fixing it is a live-config edit and is deliberately out of scope here.

### chi per-host lane session targets

Re-measure: `ssh <host> 'systemctl --user show skfleet-rotate -p Environment'`.

| host | `SKFLEET_TARGET` (codex) | glm | kimi | qwen |
|---|---|---|---|---|
| chiap01 | 8 | 0 | 3 | 1 |
| chiap03 | 7 | 0 | 3 | 0 |
| chiap08 | 3 | 0 | 0 | 0 |
| **estate total** | **18** | **0** | **6** | **1** |

Measured 2026-09-19. These are estate configuration, not repo defaults. Only these three
hosts carry a `skfleet-rotate` unit; the **five**-host tuple in the code
(`_DEFAULT_ROTATION_HOSTS`) is the roster the dispatcher may scan, not the set that runs
a lane. The two numbers are different facts and neither is wrong.

### per-node builder capacity labels

Re-measure: `skfleet get node -o json | grep builder-capacity`, or
`grep -l builder-capacity ~/.skcapstone/fleet/objects/node/*.json`.

**No node carries a `builder-capacity` label as of 2026-09-19, and no code reads one:**
`grep -rn builder-capacity src/ scripts/` returns nothing on `main`. The label is the
mechanism PR #805 introduces. Every node takes the §1 default until it merges.

## 3. Derived artifacts — never hand-edit

| Artifact | Derived from | Refreshed by |
|---|---|---|
| `~/.local/bin/skfleet-rotate.py` on each rotate host | `scripts/fleet/skfleet-rotate.py` | the deploy path, **not** `git pull` |
| `~/.pi/agent/models.json` on a worker | the lane model env the dispatcher passes | the pi harness setup |
| `CHANGELOG.md` release sections | `changelog.d/*.md` fragments | `scripts/changelog_fragments.py` |

⚠️ **The deployed rotate script drifts between hosts and CI cannot see it.** Measured
2026-09-19: chiap01 and chiap03 carry a 370031-byte copy, chiap08 a 355727-byte copy.
Compare with `ssh <host> 'md5sum ~/.local/bin/skfleet-rotate.py'` across all rotate hosts
before trusting any claim about dispatcher behaviour on a specific box.

---

## Where a new setting goes

1. **A default that ships with the code** → a named constant in `src/` or `scripts/`,
   then a row in §1, then a paired tier-3 assertion in `SOP.md`. Nothing else restates it.
2. **A per-node value** → a node spec label via `skfleet label`. Not a constant, not a
   systemd drop-in, not a new file.
3. **A per-host runtime value** → a systemd `Environment=` in the unit or a drop-in, and
   a dated row in §2 with its re-measure command.
4. **A gateway pool ceiling** → the gateway YAML on the owning host, with a dated
   rationale comment naming who measured what, and a dated row in §2.

**A number in prose with no assertion behind it is a defect.** Either pair it with the
code (§1) or stamp it as a dated measurement with a re-measure command (§2). Never both,
and never a third copy.
