# sk* ⇄ skcapstone Integration — Orchestration Handoff

**Last updated:** 2026-06-09 (Lumina closeout session — EPIC COMPLETE)
**Epic:** coord `fca7f138` — "EPIC: sk* ⇄ skcapstone optional integration backbone"
**Design:** [`docs/ADR-optional-integration-backbone.md`](./ADR-optional-integration-backbone.md)
**Goal:** every sk* service uses skcapstone's **sk-alert** + **skscheduler** by default *when skcapstone is installed*, and runs fully standalone when it is not. Default-on by presence; `SK_STANDALONE=1` forces native.

> **EPIC CLOSED 2026-06-09:** Backbone + 8 adapters + dual-mode harness (114 tests green) + README docs DONE.
> Only skchat (`ad4f721a`) remains, owned by a separate thread.

---

## 1. Status at a glance

| Layer | State |
|---|---|
| Backbone (skcapstone) | ✅ **DONE** — 4/4 tasks, 208 tests green, pushed to `github/main` |
| Reference adapter (skmemory) | ✅ **DONE** — pushed `skmemory@docs-first-principles` |
| sksecurity adapter | ✅ **DONE** — pushed `sksecurity@main` |
| skgateway adapter (Node) | ✅ **DONE** — pushed `skgateway@main` |
| skcomms / capauth / skvoice / skseed / cloud9 adapters | ✅ **DONE** — all landed |
| skchat adapter | ⬜ OPEN — **owned by a separate thread**, do not touch |
| Dual-mode test harness (`71186ebb`) | ✅ **DONE** — `tests/test_integration_backbone.py` 114 green |
| Per-repo README docs (`4065db2b`) | ✅ **DONE** — all 8 repos have "Integration modes" section |

coord: All sk-integration tasks DONE except skchat (separate thread). EPIC `fca7f138` CLOSED.

---

## 2. The backbone (DONE — this is the stable contract everything builds on)

All in `skcapstone` (`src/skcapstone/`), committed `c5d8d7c`, `9d9f93d`, `d90dfbf` on `main` (pushed to `github`):

- **`sdk.py`** — the ONLY public surface consumers import. Semver-frozen:
  - `is_available() -> bool`
  - `alert(topic, payload, *, level='info', notify=None, ttl_seconds=86400) -> bool`
  - `register_job(spec, home=None) -> str` / `unregister_job(name, home=None) -> bool`
  - `coord_create(title, **kw) -> str`
  - `register_service(name, health_url=None, pid_file=None, home=None) -> str`
- **`scheduler_jobs.py`** — `load_jobs_with_dropins()` merges `jobs.yaml` + `jobs.d/*.yaml`; `register_job()`/`unregister_job()` write atomic per-job fragments. Runtime callers (daemon `scheduled_tasks.py`, `cli/scheduler_cmd.py`, `doctor.py`) all repointed to the merged loader. **Honours `SKCAPSTONE_HOME`** (was a bug, fixed in `9d9f93d`).
- **`cli/alerts.py`** — `skcapstone alerts` subscribes to `*.critical`/`*.error`/`*.warn` and styles consumer topics by severity suffix.
- **`service_health.py`** — `check_all_services()` unions `~/.skcapstone/registry/*.json` (written by `register_service`) with the built-in checks.
- Tests: `tests/test_sdk.py`, `test_jobs_dropins.py`, `test_alerts_consumer_topics.py`, `test_service_registry.py`.

---

## 3. The adapter pattern (copy this for every remaining consumer)

**Canonical reference: `skmemory/skmemory/integration.py`** (commit `be33179`). Each adapter:

1. Add `<pkg>/integration.py` with the optional-import guard:
   ```python
   try:
       from skcapstone import sdk as _sdk
   except Exception:
       _sdk = None
   def is_present() -> bool:
       if os.environ.get("SK_STANDALONE"): return False
       if _sdk is None: return False
       try: return bool(_sdk.is_available())
       except Exception: return False
   ```
2. `alert(event, payload, level)` → `_sdk.alert(f"{SERVICE}.{level}", {"event": event, **payload}, level=level, notify=level in {"warn","error","critical"})` when present, else structured log.
   **CRITICAL CONVENTION:** topic is `<service>.<severity>` (e.g. `skvoice.error`), and the semantic **event name goes in the payload `event` field — NOT the topic suffix.** Otherwise `skcapstone alerts`' `*.error`/`*.critical`/`*.warn` wildcards never match it. (This was a real bug caught building skmemory.)
3. `ensure_schedule()` → `_sdk.register_job({...})` (a `type: shell` job running the service's periodic CLI command) when present, else rely on the service's native systemd timer / thread loop.
4. `register_self(pid_file=None)` → `_sdk.register_service(SERVICE, pid_file=...)`.
5. **Wire into real call sites:** alert into the service's failure path; `ensure_schedule()` + `register_self()` into its startup / post-install lifecycle.
6. Add an optional `[skcapstone]` extra to `pyproject.toml` (`skcapstone = ["skcapstone>=0.6.8"]`). **Never a hard dependency.**
7. Tests `tests/test_integration_adapter.py`: standalone (`SK_STANDALONE=1`), absent (`monkeypatch.setattr(integration, "_sdk", None)`), integrated (sandbox `SKCAPSTONE_HOME` to `tmp_path` + `monkeypatch.setattr(skcapstone, "AGENT_HOME", str(tmp_path))`).

**Node/non-Python services (reference: `skgateway/src/integration.mjs`, commit `cc7bf1a`):** can't import the SDK — integrate **file-based** by writing the same `~/.skcapstone/pubsub/topics/<topic>/msg-*.json` and `~/.skcapstone/registry/<name>.json` formats. Validated round-trip: Node `alert()` → Python `PubSub.poll()` reads it back intact. Presence = shared home exists + `SK_STANDALONE` unset.

---

## 4. What's left (the next session's work-list)

All tagged `sk-integration` on the coord board. Run `skcapstone coord status` / inspect `~/.skcapstone/coordination/tasks/`.

### Consumer adapters (mechanical mirrors of skmemory — for sonnet)
| Task | Repo | Native fallback to preserve | Suggested scheduled job |
|---|---|---|---|
| `eae9b815` | **skcomms** (`skcapstone-repos/skcomms`) | peer `_notify_others()` / log; daemon heartbeat thread + systemd | heartbeat/health; note skcomm is folding INTO skcomms (canonical) |
| `44b11628` | **capauth** | log (no native alerting yet) | key-rotation check (signing-daemon TODO — ensure_schedule may be a stub) |
| `66881a86` | **skvoice** | log; `while True` loop + skvoice systemd | service health / TTS-cache prune |
| `aaafe0d8` | **skseed** | log; pure kernel (periodic task type only) | belief-audit / germination; already duck-types AdaptedPrompt at `llm.py:40` |
| `fb925612` | **cloud9** | log; systemd `cloud9-daemon.timer` + launchd plist | rehydration/FEB-state check |
| `ad4f721a` | **skchat** | **OWNED BY ANOTHER THREAD — leave it.** Note: it already soft-bridges skcapstone MCP at `memory_bridge.py:24`; fold that into the one adapter. |

### Cross-cutting
- `71186ebb` — **dual-mode test harness**: parametrized over all consumers, asserts standalone AND integrated mode. This is the system acceptance gate for the EPIC.
- `4065db2b` — per-repo README "Integration modes" + `~/.skcapstone/` filesystem-contract section.
- `6b9a41a1` — note task (reference-pattern pointer); close once all adapters land.

### Final step
Once all adapters + cross-cutting are done, **complete the EPIC `fca7f138`** and consider squashing the `skcomm` superseded task notes.

---

## 5. Gotchas / things to know

- **coord CLI:** `skcapstone coord claim <id> --agent <name>` and `complete <id> --agent <name>` (the `--agent` flag is required). `create` uses `--by`. Tasks are immutable after creation (no edit/update subcommand) — supersede by completing + creating a replacement.
- **Don't commit the other threads' work.** In `skcapstone` the skcomm→skcomms migration shares the repo; in `skmemory` the mxbai-cutover changes (`backends/pgvector_backend.py`, `cli.py`) are unstaged from another thread. Stage only your adapter files explicitly — never `git add -A`.
- **skcapstone remotes:** `github` is canonical (per `~/clawd/scripts/push-pending.sh`); `origin` and `forgejo` also exist and may be stale (the local `@{u}` tracks `forgejo`, which lags — verify against `github/main`).
- **Test sandboxing:** consumers without a conftest that sets `SKCAPSTONE_HOME` must sandbox it per-test, AND `monkeypatch.setattr(skcapstone, "AGENT_HOME", str(tmp_path))` because skcapstone captures `AGENT_HOME` at import.
- **Pre-existing broken tests (NOT yours):** `sksecurity/tests/test_truth_engine.py` fails to collect (imports a missing `_check_skmemory`); `skgateway/tests/classifier.test.mjs` has 2 pre-existing failures. Run adapter tests by file to avoid these.
- **Leak check after integrated tests:** `ls ~/.skcapstone/config/jobs.d/<svc>_*.yaml ~/.skcapstone/registry/<svc>.json` should be clean — if a fragment leaks to the real home, a test isn't sandboxing `SKCAPSTONE_HOME`.

---

## 6. Commits / branches (all pushed)

| Repo | Branch | Commits |
|---|---|---|
| skcapstone | `main` (→ `github`) | `c5d8d7c` backbone · `9d9f93d` home-fix+convention · `d90dfbf` ADR §3.5 |
| skmemory | `docs-first-principles` | `be33179` reference adapter |
| sksecurity | `main` | `e65979b` threat-sharing adapter |
| skgateway | `main` | `cc7bf1a` Node file-based bridge |
