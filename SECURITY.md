# Security Policy — SKCapstone

SKCapstone is **sovereign by design**: an agent's identity, memory, trust, and
conversation state live under `~/.skcapstone/` on hardware the operator owns, and the
control/status daemon binds to **loopback only**. This document states the threat model,
how to report a vulnerability, the secret-handling rules, and the crypto posture
(honestly).

skcapstone holds **no key material of its own** — it delegates all cryptographic identity
to [capauth](https://github.com/smilinTux/capauth). It is a **non-crypto** repo under the
smilinTux [SK Repo Doc Standard](https://github.com/smilinTux/sk-standards); see the
crypto-posture section below.

---

## Reporting a vulnerability

- **Private channel first.** Open a private security advisory on GitHub
  (`smilinTux/skcapstone` → Security → *Report a vulnerability*), or contact the
  smilinTux maintainers directly. Do **not** open a public issue for an unpatched
  vulnerability.
- Include: affected version (`skcapstone --version` / `pyproject.toml`), the subsystem
  involved (daemon HTTP API / consciousness loop / model router / a pillar / an MCP
  tool), and a reproduction.
- We aim to acknowledge within a few days and to ship a fix or mitigation before any
  public disclosure. Per [VERSION_LIFECYCLE](https://github.com/smilinTux/sk-standards),
  fixes target the **Active v2** line (latest `0.13.x`).

---

## Threat model (summary)

| Asset | Threat | Mitigation |
|---|---|---|
| Daemon HTTP control/status API | Remote access, LAN exposure, SSRF | Bound to `127.0.0.1:7777` only (hard-coded in `daemon.py`); never a public interface. Remote access via operator tailnet / SSH tunnel only. Per-sender rate limiting on the request path; `/api/v1/logs` requires CapAuth. |
| Agent home `~/.skcapstone/` (memories, conversations, soul, trust) | Disk/host compromise; exfiltration | Local-only storage on operator hardware; owned by the operator; encrypted seed sync over the operator's own Syncthing mesh. Memory-at-rest sealing is delegated to skmemory's vaulted backend. |
| PGP private key / agent identity | Leak, impersonation | **Owned by capauth**, not skcapstone. Private key never leaves the node; held by gpg-agent. skcapstone only orchestrates signing/verification via capauth. |
| Inbound `*.skc.json` envelopes | Spoofed sender, replay, flooding | Sender identity verified via capauth signature (transport layer); envelope-id dedup guard in the consciousness loop; per-sender sliding-window rate limiter drops over-limit intake without crashing the loop. |
| LLM provider API keys | Leak into logs / repo / transcripts | Sourced from environment / systemd `EnvironmentFile` only; never inlined or committed; `.env.example` documents names, not values. |
| Autonomous LLM routing | Prompt injection, data exfil via a compromised cloud backend | Privacy-sensitive / localhost-pinned tasks are forced to the LOCAL (Ollama) tier and never leave the node; cloud backends are opt-in (present only when their key is set). |
| Dependency supply chain | Malicious/compromised dependency | Version-bounded deps in `pyproject.toml`; minimal core install; every sibling `sk*` integration is an opt-in extra. |
| Self-healing auto-fix | Unintended destructive remediation | `SelfHealingDoctor` fixes are scoped to recreating dirs / rebuilding derived indexes / re-probing backends; unrecoverable states escalate to the operator over SKChat rather than force-fixing. |

---

## Secret handling rules (hard rules)

- **Never** inline a live secret in the repo, docs, tests, or commit history.
- LLM provider API keys (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY`,
  `MOONSHOT_API_KEY`, `NVIDIA_API_KEY`) and `SKCOMMS_TURN_SECRET` are read from the
  **environment** (shell profile / systemd `EnvironmentFile`) — presence enables the
  backend; absence disables it. `.env.example` lists variable **names** only.
- PGP private keys live only under the operator's capauth/gpg-agent control and never
  leave the node. skcapstone reads no private key file directly.
- Do not echo secrets, PGP material, or vaulted memory content into `logs/daemon.log`
  or the HTTP API responses.

---

## Cryptography posture (honest claims)

skcapstone performs **no** cryptographic operations of its own — it neither generates,
exchanges, signs, wraps, nor stores key material. All identity crypto (PGP keypairs,
DID documents, challenge-response auth, the peer trust store) is owned by
[capauth](https://github.com/smilinTux/capauth); memory-at-rest sealing is owned by
[skmemory](https://github.com/smilinTux/skmemory). Maturity tier for this repo:
**T0 — N/A (no key material)**.

- skcapstone makes **no** post-quantum claim. It does **not** use the words
  "quantum-proof", "quantum-safe", or "unbreakable", and does not describe AES-256 as
  quantum-broken.
- Any post-quantum posture is a property of the **capauth / sk_pgp / sk_pqc** cutover
  (hybrid `HKDF(X25519 ‖ ML-KEM-768)`, ML-KEM per **FIPS 203**), not of this repo.
- The optional daemon TLS uses a **self-signed** certificate for loopback transport
  confidentiality only; it is not a trust anchor and does not authenticate peers.

This conforms to the smilinTux
[CRYPTOGRAPHY_STANDARD](https://github.com/smilinTux/sk-standards) honest-claim rules:
every claim above is surface-scoped and backed by code (`daemon.py`, `consciousness_loop.py`,
the `pillars/` initializers) or by `skcapstone doctor` / `skcapstone status`.

---

## Supported versions

Security fixes target the latest released `0.13.x` (Active v2). Older lines are
best-effort; upgrade to the latest minor for fixes.
