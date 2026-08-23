# .100 secret and key inventory (read-only audit)

Epic `3bbf39ea`, card `d2663796` (parent `804dc9d5`). Collected 2026-08-14
over `ssh cbrd21@192.168.0.100`. **Nothing on .100 was modified.** Only `ls`,
`find`, `gpg --list-secret-keys`, `systemctl cat`, `systemctl show` and
`grep` of key NAMES were run.

**Paths and key names only. No secret values appear in this file.**

## Why this audit exists

.100 is meant to be a `worker-gpu`: serve inference, hold zero sovereign
state. It got a de-facto full install instead, so operator-grade material is
sitting on a VM whose hypervisor (norpv1300) is also in scope. Before card
`f709c721` slims the box, we need to know exactly what is on it, because
some of what is here cannot be rotated, only killed.

## Verdict summary

| verdict | count | meaning |
|---|---|---|
| **rotate** | 8 | credential is or may be exposed; issue a new one and invalidate the old |
| **revoke** | 3 | bearer credential with no rotation path; killing the session is the only remedy |
| **delete** | 6 | must not exist on a worker at all; remove after the Syncthing unshare |
| **keep** | 7 | legitimately belongs to a worker-class node |
| **keep (public)** | 2 | public material, no confidentiality requirement |

## The table

| path | kind | scope | reachable-by | verdict | rationale |
|---|---|---|---|---|---|
| `~/.ssh/id_ed25519` | ssh private key | operator | `cbrd21` user, any process running as it | **rotate** | A worker needs no outbound ssh identity. It is the operator's key shape, present since 2026-05-09. Rotate, then do not reissue to this node. |
| `~/.ssh/id_ed25519.pub` | ssh public key | operator | same | keep (public) | Public half. Harmless, tracks the rotation above. |
| `~/.ssh/authorized_keys` | ssh trust anchor | node | sshd | **keep** | This is how the fleet reaches .100 read-only. Belongs on a worker. Re-verify contents after the operator key rotation. |
| `~/.ssh/known_hosts`, `known_hosts.old` | ssh host trust | node | `cbrd21` | keep | Host fingerprints, not credentials. |
| `~/.gnupg/pubring.kbx` | gpg public keyring | third-party | gpg | keep (public) | **32 bytes: an empty keyring.** Confirmed rather than assumed. |
| `~/.gnupg/trustdb.gpg` | gpg trust database | third-party | gpg | keep | 1200 bytes, mtime 2026-08-14 18:52. Trust metadata only. Note: the card recorded "no trustdb"; one now exists, created today. |
| (`gpg --list-secret-keys`) | **negative result** | n/a | n/a | n/a | **Returns nothing. There is genuinely no secret keyring on .100.** This was verified, not inferred from the empty pubring. |
| (full-content scan of all 29 `.asc` files) | **negative result** | n/a | n/a | n/a | **Zero PGP PRIVATE KEY BLOCKs anywhere under `~/.skcapstone`**, and zero `private.asc` files. Every `.asc` on the box is a `public.asc` or a `*.pub.asc`. Re-verified by grepping whole files rather than first lines. **Why:** `~/.skcapstone/.stignore` carries `**/private.*`, so .158 never announces the 11 agent private keys it holds. See `control-bus-folder.md` and card `20a1d4d3`. |
| `~/.skcapstone/capauth/service/keys.db` | capauth key database | **operator** | capauth service, any process as `cbrd21` | **rotate** | 12288 bytes, world-readable (`-rw-r--r--`). This is the capauth key store on a node that should hold a least-privilege worker credential and nothing else. Mode is wrong independently of the rotation. |
| `~/.skcapstone/capauth/service/bunker_sessions.json` | bunker session state | operator | capauth service | **revoke** | 30 bytes (likely empty session set), mode 0600. Session material has no rotation path; invalidate server-side. |
| `~/.skcapstone/capauth/identity/identity.json` | capauth identity | operator | capauth | **delete** | The node carries an operator identity. Replaced by a `worker` identity class in card `fc6500cb`, issued in `5ee6510f`. |
| `~/.skcapstone/capauth/identity/custody.json` | custody record | operator | capauth | **delete** | Same: operator-scope custody metadata does not belong on a worker. |
| `~/.skcapstone/capauth/identity/profile.json` | identity profile | operator | capauth | delete | Non-secret, but it is the operator identity's metadata. Goes with it. |
| `~/.skcapstone/capauth/identity/public.asc` | PGP public key | operator | anyone | keep (public) | Public. Retained for verification until the worker identity is issued. |
| `~/.skcapstone/capauth/identity/root-revocation.asc` | **root revocation cert** | **operator** | `cbrd21` (mode 0600) | **delete** | 288 bytes. A revocation certificate is a loaded gun: anyone holding it can permanently kill the root identity. It must live in the sealed vault and the bunker, never on a GPU worker. High priority. |
| `~/.skcapstone/revocations/lumina-02BC0EB3-revocation.asc` | agent revocation cert | operator | `cbrd21` (mode 0600) | **delete** | 989 bytes. Same reasoning: destructive capability with no upside on this node. |
| `~/.skcapstone/capauth/quarantine-20260814T072744Z/keys.db.before` | quarantined key db | **operator** | `cbrd21` (dir 0700) | **rotate** | The stray-keypair quarantine from 2026-08-14. Pre-quarantine key material still on disk; it was quarantined, not destroyed. Confirm the keys it holds are in the rotation set. |
| `~/.skcapstone/capauth/identity-preflight-backup-20260814T071857Z/` | identity backup | operator | `cbrd21` (dir 0700) | delete | `identity.json`, `profile.json`, `public.asc`. A backup of the operator identity that should not be on this node in the first place. |
| `~/.skcapstone/capauth/security/tokens/*.json` | capability tokens | operator/agent | capauth | **rotate** | **1460 token files.** Volume alone makes per-token triage impractical; treat the whole store as exposed and let it re-mint under the worker identity. |
| `~/.skcapstone/capauth/tokens/localhost/tokens.json` | local capability tokens | node | capauth | rotate | 1 file. Re-mint with worker scope. |
| `~/.skcapstone/capauth/advocate/{lumina,jarvis}.json` | advocate policy | agent | capauth | keep | Policy documents, not credentials. Move with the identity work; no rotation needed. |
| `~/.skcapstone/capauth/acl/` | acl store | node | capauth | keep | Empty directory. |
| `~/.skcapstone/agents/*/capauth/identity/public.asc` (13 agents) | PGP public keys | agent | anyone | keep (public) | Public halves only; verified no PRIVATE blocks. |
| `~/.skcapstone/agents/*/trust/*.pub.asc` (9 agents) | PGP public keys | agent | anyone | keep (public) | Same. |
| `~/.skcapstone/agents/lumina/config/secrets.env` | env secrets | **agent** | any process as `cbrd21` | **rotate** | Names present: `SKMEMORY_SKVECTOR_URL`, `SKMEMORY_SKVECTOR_KEY`, `SKMEMORY_SKGRAPH_URL`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `SKAGENT`, `SKMEMORY_AGENT`, `SKCAPSTONE_AGENT`, `NVIDIA_API_KEY`. Rotate `SKMEMORY_SKVECTOR_KEY`, `NVIDIA_API_KEY`, and the Telegram API pair. |
| `~/.skcapstone/agents/lumina/secrets/nextcloud.env` | env secrets | agent | any process as `cbrd21` | **rotate** | Names present: `NEXTCLOUD_URL`, `NEXTCLOUD_USERNAME`, `NEXTCLOUD_PASSWORD`. A live account password in plaintext on a worker. |
| `~/.skcapstone/agents/lumina/telegram.session` | **Telethon session** | agent | any process as `cbrd21` | **revoke** | 106496 bytes, mode 0644. A bearer credential that **cannot be rotated, only killed**: terminate the session from an authenticated Telegram client. World-readable is the wrong mode for it. |
| `~/telegram.session` | **Telethon session** | agent | any process as `cbrd21` | **revoke** | 77824 bytes, mode 0644, dated 2026-03-18. A second, older session outside `~/.skcapstone` entirely, so the Syncthing unshare will not touch it. Same revoke-only property. |
| `~/.claude/.credentials.json` | **Claude OAuth token** | **operator** | any process as `cbrd21` | **rotate** | Mode 0600. Contains a `claudeAiOauth` block. A paid-API bearer credential on a GPU worker that has no reason to run an agent harness. |
| `~/.ollama/id_ed25519` | ollama registry key | service | ollama | **keep** | Ollama's own model-registry identity. This is worker-class material and legitimately belongs here. |
| `~/.ollama/id_ed25519.pub` | ollama public key | service | ollama | keep (public) | Public half. |
| `~/.skcapstone/identity/identity.json` (+ 2 `.bak`) | operator identity | operator | SK tooling | **delete** | The shared operator identity record. Replaced by the worker identity class. The two backups go with it. |
| `~/.skcapstone/agents/lumina/wallet/joules.json`, `transactions.jsonl` | economic ledger | agent | SK tooling | delete | Not credentials, but sovereign state a worker must not hold (590K of transaction log). Note the two `.sync-conflict-*` files: evidence of the multi-writer problem the folder split fixes. |
| `~/.local/state/syncthing/key.pem` | syncthing device key | node | syncthing | **keep** | This IS the node's Syncthing device identity. It must survive so the folder can be **unshared** in an orderly way (card `3118769c`). Do not delete before the unshare. |
| `~/.local/state/syncthing/https-key.pem` | syncthing GUI TLS key | node | syncthing | keep | GUI TLS only, bound to 127.0.0.1:8384. |
| `~/.local/state/syncthing/config.xml` `<apikey>` | syncthing GUI API key | node | syncthing GUI | **rotate** | Grants full control of the Syncthing instance, including folder configuration. Mode 0600 and GUI bound to loopback, which is why this is rotate and not urgent. |
| `~/.local/state/syncthing/cert.pem`, `https-cert.pem` | certificates | node | syncthing | keep (public) | Public certificates. |
| `~/.skcapstone/vault/` | sealed vault | operator | skvault | keep | **Empty directory.** No vault material on .100. |
| `~/.skcapstone/secure/` | secure store | operator | SK tooling | keep | **Empty directory.** |
| `~/.huggingface/` | HF credentials | third-party | HF libs | keep | **Empty directory.** No HF token on this box. |
| `~/.docker/config.json` | registry credentials | third-party | docker | n/a | **Does not exist.** `~/.docker` is absent entirely despite docker being enabled. |
| `~/.config/environment.d/` | env dropins | node | systemd user manager | keep | **Empty**, as the card expected. Confirmed. |

## Systematic sweeps, and what they found

### Every `EnvironmentFile=` on both scopes

Swept programmatically over every enabled unit rather than the card's named
list, so the coverage matches the actual unit set:

- **User scope: zero units reference an `EnvironmentFile`.** The SK services
  carry their configuration inline or inside their `ExecStart` wrapper
  scripts. No `Environment=` line on any enabled user unit names a
  `KEY`/`TOKEN`/`SECRET`/`PASS` variable either.
- **System scope: 6 units**, all distro baseline and none SK-related:
  `networkd-dispatcher` (`/etc/default/%p`), `nvidia-cdi-refresh`
  (`/etc/nvidia-container-toolkit/nvidia-cdi-refresh.env`), `rpcbind`
  (`/etc/rpcbind.conf`, `/etc/default/rpcbind`), `snapd.apparmor`,
  `snapd.autoimport` and `snapd` (`/etc/environment`,
  `/var/lib/snapd/environment/snapd.conf`), `tailscaled`
  (`/etc/default/tailscaled`).

So the agent secrets found above are reachable through the process
environment of anything running as `cbrd21`, not through a unit-declared
env file. That is a wider surface, not a narrower one.

### Effective vs on-disk `ExecStart`

`skai-beellama.service` has two `ExecStart` lines: the vendor unit serves
`qwen3.6-27b-abliterated` on :8082, then a drop-in resets `ExecStart=` and
substitutes `/mnt/comfyui/beellama.cpp/run-skai.sh`, which is what actually
serves `ornith-1.0-9b`. Read the **effective** unit, not the vendor file,
for anything that matters here.

### No cron surface

`which crontab` returns nothing. .100 has no crontab binary installed, so
there is no cron credential surface to audit or remove. Confirmed, not
assumed.

## Priority order for card `6c77b26d` (rotation)

1. `~/.claude/.credentials.json` (paid-API bearer, no business being here)
2. `~/.skcapstone/agents/lumina/secrets/nextcloud.env` (live account password)
3. `~/.skcapstone/agents/lumina/config/secrets.env` (`NVIDIA_API_KEY`,
   `SKMEMORY_SKVECTOR_KEY`, `TELEGRAM_API_ID`/`TELEGRAM_API_HASH`)
4. `~/.ssh/id_ed25519`
5. `capauth/service/keys.db` and the 1460-file token store
6. Syncthing GUI api key

Revoke-only, no rotation path, do these by hand from an authenticated
client: both `telegram.session` files, and `bunker_sessions.json`.

🔴 Deletions wait for the Syncthing unshare (card `3118769c`).
`~/.skcapstone` on .100 is a live shared folder, so deleting anything under
it propagates the delete to .158, .41 and noroc2027. `~/telegram.session`
and `~/.claude/.credentials.json` are **outside** the shared folder and can
be handled independently.

## Inference smoke test: before and after

Card AC requires proof that this read-only audit changed nothing.
`scripts/fleet/dot100-inference-smoke.sh` (card `193089bf`) run either side
of the collection.

| probe | before | after |
|---|---|---|
| embed `:11434/api/embed` `mxbai-embed-large` | PASS dim=1024 | PASS dim=1024 |
| embed `:11438/v1/embeddings` (mxbai Vulkan arc) | PASS dim=1024 | PASS dim=1024 |
| chat `:8082` `ornith-1.0-9b` max_tokens=2048 | PASS | PASS |
| chat `:8085` `qwen3.5:4b` | PASS | PASS |
| comfyui `:8188` | PASS http=200 | PASS http=200 |
| f5-tts `:18796` | PASS | PASS |
| whisper-stt `:18794` | PASS | PASS |
| **failures** | **0** | **0** |

`ActiveEnterTimestamp`, unchanged across the whole audit:

| unit | before | after |
|---|---|---|
| `system/ollama.service` | Fri 2026-08-14 05:38:38 UTC | Fri 2026-08-14 05:38:38 UTC |
| `system/mxbai-arc.service` | Fri 2026-08-14 05:38:32 UTC | Fri 2026-08-14 05:38:32 UTC |
| `user/skai-beellama.service` | Fri 2026-08-14 09:03:31 UTC | Fri 2026-08-14 09:03:31 UTC |
| `user/comfyui.service` | Fri 2026-08-14 18:29:08 UTC | Fri 2026-08-14 18:29:08 UTC |
| `user/f5-tts.service` | Fri 2026-08-14 05:38:32 UTC | Fri 2026-08-14 05:38:32 UTC |
| `user/whisper-stt.service` | Fri 2026-08-14 05:38:17 UTC | Fri 2026-08-14 05:38:17 UTC |
| `user/qwen3-arc.service` | Fri 2026-08-14 05:38:17 UTC | Fri 2026-08-14 05:38:17 UTC |
| `user/sovereign-orchestrator.service` | Fri 2026-08-14 05:38:17 UTC | Fri 2026-08-14 05:38:17 UTC |
