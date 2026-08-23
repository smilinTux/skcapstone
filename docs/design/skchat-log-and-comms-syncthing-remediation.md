# Remediation: skchat runaway log + comms/inbox pileup pinning syncthing

Status: planned (v2, 2026-07-10, post deep-dive)
Owner: opus
Origin: investigation of node `.41` (cbrd21-laptop): high WAN download + load 17.
Tracking: coord epic `36450c88` + child tasks (see bottom).

## Symptoms on .41
- Load ~17; `syncthing` pinned 137% CPU 29h.
- `~/.skchat/daemon.log` grew to 3.8 GB/day (opus daemon); jarvis daemon log also growing.
- ~792k files in the `skcapstone-sync` folder; ~270k are un-GC'd `comms/inbox` envelopes
  (lumina 133k, chef 59k, opus 41k, jarvis 37k).
- (The ~15 GB/day WAN download itself is the separate `hammerTime` rsync from remote
  tailnet node `chiap01`; NOT this remediation.)

## Root causes (confirmed at source across 3 repos)

- **RC1 unbounded log** — `skchat/src/skchat/daemon.py:191-196` uses
  `logging.basicConfig(filename=…, level=INFO)` (plain FileHandler, no rotation) on the
  **root** logger, so all `skcomms.*` transport logs also land in `daemon.log`.
- **RC2 transport storm** — `skcomms/src/skcomms/router.py:882` logs
  `Transport '%s' send failed` at WARNING every cycle. A per-transport cooldown exists
  (`_record_failure` :724, `FAILURE_THRESHOLD=3`, `COOLDOWN=60s`) but `router.py:894`
  **skips cooldown for `perm:` errors and for `*` broadcasts**, so those retry+log every 5s.
  No "log once per state change" dedup anywhere.
- **RC3 `*` heartbeat bug** — `skchat daemon.py:994-1002` broadcasts presence to the literal
  recipient `"*"` every ~60s. Point-to-point rails (`https-s2s`, `tailscale`) are offered as
  candidates and turn `"*"` into a peer-store lookup that raises
  `Peer name '*' is empty after sanitization` (`skcomms discovery.py:239-241`), logged at
  `http_s2s.py:473` + the paired `perm:` WARNING at `router.py:882`. Two WARNINGs/heartbeat.
- **RC4 dead rails logged forever** — enabled-but-unreachable rails (nostr bad key, tailscale
  no-IP, https-s2s 422, webrtc signaling `ws://127.0.0.1:9390` down) fail every cycle.
  `skcomms core.py:339-349` registers a rail if merely `enabled`+constructible; no startup
  health-gate. WebRTC degraded also re-logged every ~30s from `skchat daemon.py:1267-1268`.
- **RC5 inbox never GC'd** — inbox envelopes are write-once/read-maybe/delete-never. The
  skcapstone consumer `consciousness_loop.py:2141 _on_inbox_file` reads + submits but never
  unlinks/archives. `FileTransport.receive()` (`skcomms file.py:403-432`) *would* archive/
  delete, but nothing runs it as a loop. `skcomms housekeeping.py:47-48` deliberately skips
  inbox. `skcapstone housekeeping.py` prunes only outbox/acks/seeds/legacy — never inbox.
- **RC6 syncthing over-syncs** — the `skcapstone-sync` folder = the whole `~/.skcapstone`
  tree (`syncthing_setup.py:251`), so syncthing scans ~792k files incl. all comms + a
  479 MB `chroma.bak` dump + pidfiles, and races the consumer (`pull: no such file`).

## RESOLVED design decision
Syncthing is **not** a required carrier for comms delivery (federation S2S is self-contained
HTTP: `skcomms api.py:1272 post_inbox` → writes local recipient inbox). Inbox = destination.
Therefore: delete-on-consume + a conservative TTL backstop (hours, well above poll interval)
is safe. Excluding derived/runtime state (incl. comms archive) from syncthing is safe;
keep `comms/inbox`+`outbox` synced only if a given agent's sender uses the SyncthingTransport/
mailbox rails, and rely on GC (F5/F6) to keep counts low rather than blanket-excluding comms.

## Fix plan by repo workstream (chosen so parallel agents never touch the same file)

### Workstream A — skchat repo (branch `fix/log-remediation`)
- **A1 (F1):** `daemon.py:191-196` → `RotatingFileHandler` (env `SKCHAT_LOG_MAX_BYTES`
  default 50 MB, `SKCHAT_LOG_BACKUP_COUNT` default 5) + `SKCHAT_LOG_LEVEL` (default INFO).
  Import `logging.handlers`. Move routine per-cycle INFO lines (Received/No-new/Reaper/
  Outbox/`transport.py:766`) to DEBUG. New tests in `tests/test_daemon.py`.
- **A2 (F3-skchat):** `daemon.py:1263-1268` — only WARN on WebRTC signaling-health
  *transitions* (track last value on self), not every 30s. Optionally gate `_webrtc_active`
  on startup reachability.
- **A3 (F4-skchat):** `transport.py:988` `_poll_file_inbox` logs non-JSON at WARNING; make a
  leading-`<` (XML/CoT `<event>`) payload a DEBUG skip, matching the main path at
  `transport.py:668` (already DEBUG). Test in `tests/test_transport.py`.

### Workstream B — skcomms repo (branch `fix/transport-noise-and-inbox-gc`)
- **B1 (F2):** `router.py:882-886` — per-`(transport,error-signature)` state map; WARN only on
  transition into failing state + once on recovery (`_record_success` :755), else DEBUG.
  Mirror receive-side WARNING at `router.py:506`.
- **B2 (F2):** per-transport backoff for `perm:`/`*` failures — new
  `_perm_backoff[(name,recipient)]` consulted in `_select_transports` (:585-594), updated at
  `router.py:894`, so structurally-undeliverable rails stop being re-attempted every cycle.
- **B3 (F3):** `_select_transports` (:585-594) — when `recipient == "*"`, keep only
  broadcast/relay-capable rails (drop https-s2s/tailscale). Also early-return `None` for
  `recipient in ("*","")` in `http_s2s.py:_resolve_inbox_url`/`_inbox_url_from_store`
  (:375/:439) before the peer-store call; downgrade `http_s2s.py:473` to DEBUG for that case.
- **B4 (F3):** `webrtc.py:499-505` — log signaling connect failure once per state change
  (first failure + recovery), DEBUG in between; the health-gate (B5) is the real fix if the
  broker is simply absent.
- **B5 (F3):** `core.py:339-349 from_config` — after `_load_transport`, best-effort
  `transport.health_check()`; if enabled-but-unreachable, register quarantined (skipped by
  `_select_transports` until a periodic re-probe passes) instead of retry-every-cycle.
- **B6 (F4):** at the skcomms inbound decode that runs `ChatMessage.model_validate_json` on
  envelopes, treat a leading-`<` payload as a non-chat beacon → DEBUG skip, not WARNING.
- **B7 (F5-lib):** add `FileTransport.prune_inbox(ttl_hours)` (+ `SyncthingTransport`) reusing
  `_prune_dir_by_ttl` (`file.py:155-188`); add `inbox_ttl_hours` to `HousekeepingConfig`
  (`config.py:51`); call it in `run_housekeeping_pass` (`housekeeping.py:141-158`). This is
  the shared primitive skcapstone F6 depends on.

### Workstream C — skcapstone repo (branch `fix/inbox-gc-and-sync-scope`)
- **C1 (F5-consumer, root fix):** `consciousness_loop.py:2141 _on_inbox_file` — after
  successful parse + `self._executor.submit(...)` (:2221), `path.unlink()` (or move to
  `archive/`); route malformed/oversized (:2150,:2171) to `deadletter/`. Presence-on-disk
  becomes the durable "unconsumed" marker.
- **C2 (F6):** `housekeeping.py` — add `DEFAULT_INBOX_MAX_AGE_HOURS=168`, `_inbox_dirs()`
  (mirror `_legacy_outbox_dirs` :173 but `/comms/inbox`), `prune_inbox(...)`, and a
  `prune_derived_junk()` for `**/chroma.bak*` + `**/*.pid`. Register in `run_housekeeping`
  `targets` (:328-335), dry-run block (:344-359), prune block (:362-365), and the CLI key
  tuple `cli/housekeeping.py:49`. TTL backstop only (delete-on-consume from C1 is the primary
  guarantee); do not ship aggressive age-only deletion below the poll interval.
- **C3 (F7):** add real `src/skcapstone/defaults/.stignore` template; install idempotently
  from `ensure_skeleton()` (`__init__.py`) via `_install_default_stignore` (mirror
  `_install_default_jobs_dropins` :196, never overwrite an existing user file); replace the
  4-rule `STIGNORE_CONTENTS` in `syncthing_setup.py:26-42` to read the same template.
  Exclude all derived/runtime (chroma*, chroma.bak*, *.db*, sessions, logs, *.pid,
  conversations, backups, deployments, pubsub, file-transfer, telegram.session, skwhisper
  state, memory/archive); keep source-of-truth synced (memory/{short,mid,long}-term JSON,
  soul, seeds, journal.md, trust/febs, coordination). Comms per the resolved decision.
- **C4 (F6-schedule):** `skhousekeep.timer` does NOT exist. Add
  `skcapstone-housekeep.{service,timer}` under `src/skcapstone/data/systemd/` (+ mirror
  `systemd/`), cloning the `skcapstone-memory-compress.{service,timer}` pair
  (Type=oneshot, `ExecStart=skcapstone housekeeping`). The weekly scheduler drop-in
  `config/jobs.d/housekeeping.yaml` (Sun 04:00) + in-daemon hourly loop already exist; the
  timer is the decoupled safety net for non-daemon nodes.

## Cross-repo contract (so B and C agree)
"Consumed" = the local consumer removed the file (C1). TTL sweeps (B7 lib primitive + C2
skcapstone) are the backstop for agents with no live consumer. TTL must exceed consume
latency (default 168h). skcapstone C2 may call the skcomms B7 primitive where available,
else prune `agents/<agent>/comms/inbox/*.skc.json` by mtime directly.

## Rollout / deploy (gated — do NOT auto-deploy to .41)
1. Each workstream on its own branch, TDD, full repo test suite green.
2. Review branches; run all three suites together.
3. Deploy to .41: pull branches into the editable installs, restart
   `skchat` daemons + `skcapstone` daemon, then a bounded `housekeeping` + a syncthing
   rescan. Verify: daemon.log stays capped, comms/inbox count drops, syncthing CPU falls,
   load normalizes. Checkpoint with Chef before this step.

## Immediate mitigations already applied on .41 (2026-07-10)
- Truncated both skchat daemon logs (3.8 GB→0) + user `logrotate` stopgap (200 MB, */20 cron).
- Added safe `.stignore` entries: `**/chroma.bak*`, `daemon.pid`, `**/*.pid`.
- Left comms/inbox untouched pending the code fix.
