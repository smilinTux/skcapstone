# Security Policy - skdashboard

`skdashboard` is the SKWorld operator dashboard: a Starlette web UI and JSON API bound
to **`127.0.0.1:7778`** that reads and mutates the skcoord coordination store (cards,
ITIL records, CMDB) and can queue AI runs against fleet items.

Read the posture below before relying on it or reporting an issue. The short version:
**the loopback bind is the access control today, and the authorization gate ships
open.** Every write route now passes through that gate, but the gate itself is still
staged off by default, and the actor identity it records is still self-asserted.

## Posture

> **Pre-1.0, not independently security-audited.** No third-party audit, fuzzing, or
> formal review has been performed. Review it yourself before exposing it to anything
> wider than a loopback interface.

**skdashboard is not a cryptographic component.** It generates no keys, signs nothing,
verifies no signatures, and stores no key material. Its only security-relevant original
code is `src/skdashboard/queue_authz.py`, a **policy enforcement point (PEP)** that
compares a shared secret with `hmac.compare_digest` and otherwise delegates the decision
to `capauth.authz.decide` (the PDP). Identity, key custody, and the policy itself belong
to [capauth](https://github.com/smilinTux/capauth).

Maturity tier: **`T0 - N/A (no key material)`**. This repo makes **no** post-quantum
claim, and none of the "quantum-proof" family of terms applies to anything here. If you
need the fleet's cryptographic posture, read
[skcomms](https://github.com/smilinTux/skcomms) and capauth, not this file.

## What is actually enforced today

This section is the honest one. Do not assume a POST route is protected.

### The bind is the control

`uvicorn.Config(app, host="127.0.0.1", port=port, ...)` in `_UvicornServer.__init__`. The
`--port` flag moves the port; **nothing in this package moves the interface**. There is
no TLS, no public `:443` route, and no Funnel exposure. Any remote access is an SSH
tunnel or tailnet decision made outside this repo, and it removes the only control that
is unconditionally in force.

### The authz gate is staged, and stages open

`_capability_gate` (`dashboard.py`, the single gate body that `_queue_gate` and
`_change_gate` now both wrap) short-circuits:

```
if neither SKAI_AUTHZ nor SKAI_QUEUE_TOKEN is set:
    return {"ok": True, "reason": "loopback-open", "via": "none"}
```

That is the deployed configuration. Once **either** variable is set, the decision routes
through `queue_authz` and is **fail-closed by construction**: `token` mode denies when no
secret is configured, `pdp` mode denies on any PDP error or import failure, `both`
requires every enabled check to pass, and an unrecognized `SKAI_AUTHZ` value falls back
to `token` rather than to open.

**Operational warning.** Flipping `SKAI_AUTHZ` on a live seat is not a no-op. The browser
UI sends no `X-SK-Capability` header, so every gated button starts denying the moment the
gate goes live. Mint and deliver capabilities first. As of card `9d37d53d` that blast
radius includes the board itself (card note/move/assign), the CMDB reseed button, and the
model-enablement panel, which were previously ungated and would have kept working.

### Gated vs ungated routes

Every registered `POST` route passes through the gate. `tests/test_write_route_gates.py`
sweeps the real route table and fails if a `POST` endpoint carries no gate marker, so
this table cannot silently drift back.

| Routes | Gate |
|---|---|
| `POST /api/card/{id}/queue-ai`, `POST /api/queue/{surface}/{id}` | `_queue_gate` (capability `agentrun.execute` for `mode=execute`, otherwise `agentrun.queue`) |
| `POST /api/change/{id}/{validate,schedule,arm,verify}`, `GET /api/change/{id}/pir-draft` | `_change_gate` (explicit `change.*` capability) |
| `POST /api/assistant` mutations | `_ai_capability_ok`, the coarse boolean form of `_queue_gate` |
| `POST /api/card/{id}/{action}` (note, move, assign, and the rest of the board mutations) | `_capability_gate`, **interim** capability `agentrun.queue` |
| `POST /api/cmdb/seed` | `_capability_gate`, **interim** capability `agentrun.queue` |
| `POST /api/models/advertise` | `_capability_gate`, capability `skgateway.admin` |
| every other `GET` | **none.** All reads are open on the bound interface. |

**"Interim" means the capability is borrowed, not purpose-built.** capauth seeds no
`skboard.*` or `cmdb.*` rule, so those two routes reuse the already-seeded, already-granted
write-class row `agentrun.queue` rather than ship an ungranted capability that would deny
every caller the moment the gate goes live. This mirrors skchat's `dataplane_auth.py`,
which maps the same `POST /api/card/{id}/{action}` route to an interim capability with a
note that it migrates to `skboard.write` (SKWorld Authorization Model L1.8). The
consequence to be aware of: **a subject holding `agentrun.queue` can also mutate the board
and reseed the CMDB.** Narrowing that needs a capauth rule, not a change here.

### `X-SK-Actor` is asserted, not authenticated

**This is the sharper of the two problems, and it is still open.** Both `_change_actor`
and the card-mutation path take the actor from the `X-SK-Actor` request header, which
this package does **not** verify. It is trusted because it is expected to be set by an
authenticating layer in front of the dashboard, and no such layer is deployed today.
Actor strings that land on ITIL and card records are therefore **attribution, not proof
of identity**. `_change_actor` at least refuses a client-supplied JSON body fallback (the
card-mutation path does accept one, defaulting to `"dashboard"`).

Gating the write routes does not close this. Any caller that can reach the loopback port
can still name itself anything in `X-SK-Actor`, and while the gate is loopback-open it
does not have to present a capability either. Authenticating the human at the door is the
Unified Consent Plane epic's job (capauth-minted capabilities carried in
`x-sk-capability`), not this repo's, and a partial capability check invented here would be
worse than the honest gap. Treat every actor string in this system's records accordingly
until that epic lands.

### Other notes

- `/.well-known/skworld-module.json` is served unauthenticated by design. It is public
  discovery metadata built from the request origin and contains no secrets.
- `/api/models*` proxies `SKGATEWAY_URL` (default `http://localhost:18780`) with a
  3-second timeout. Pointing that variable at an untrusted origin makes the dashboard an
  SSRF-ish egress for whoever can set it.
- `dashboard_economy.py` and `dashboard_assistant.py` lazy-import optional siblings and
  degrade to an empty payload plus an `errors` note, so a missing dependency is not a
  denial-of-service on the page.
- `src/skdashboard/static/vendor/Sortable.min.js` is vendored third-party code and is not
  reviewed on every release. Treat it as a supply-chain surface.

## Threat model

### In scope

- Any route reachable **without** the loopback bind that mutates the coordination store,
  ITIL records, or the CMDB.
- A bypass of `queue_authz` when `SKAI_AUTHZ` or `SKAI_QUEUE_TOKEN` **is** set: a
  capability accepted that the PDP denied, a PDP error treated as an allow, or a
  `both`-mode request satisfied by only one leg.
- Non-constant-time or otherwise leaky comparison of `SKAI_QUEUE_TOKEN`.
- Path traversal or arbitrary read through the `/static` mount, the `_page()` helper, or
  any `{card_id}` / `{surface}` / `{rid}` path parameter reaching the filesystem.
- Injection through `/api/assistant` that turns a read/report request into an unintended
  `ACTION` mutation.
- Any change that widens the bind off `127.0.0.1`, or that makes a gated route ungated.
- Stored XSS in a card, ITIL, or CMDB field rendered by the packaged pages.

### Out of scope

- **Anything that presumes the service is already publicly exposed.** It binds loopback;
  exposing it is an operator decision this repo does not make and cannot defend.
- **The loopback-open default itself.** It is documented, deliberate, and staged. A
  report that "no auth is required on localhost" tells us what this file already says.
- **Identity, key custody, capability minting, and policy content.** All capauth.
- **The coordination store's own integrity model.** That is skcoord and skcapstone.
- **Vulnerabilities in skgateway, Starlette, uvicorn, or the vendored Sortable.js.**
  Report those upstream; we will consume the fix.
- **Local privilege escalation on a host where the attacker is already the same user.**
  The dashboard runs as a systemd **user** unit with that user's full privileges.

### Trust roots and dependencies

| Surface | Owner | Basis |
|---|---|---|
| Authorization decision | capauth (`capauth.authz.decide`) | `SKWORLD_AUTHORIZATION_STANDARD` |
| Shared-secret comparison | this repo (`queue_authz.py`) | `hmac.compare_digest`, stdlib |
| Identity / key material | capauth | not present in this repo |
| Coordination data integrity | skcoord / skcapstone | event-sourced card store |
| Transport | none (loopback, plaintext HTTP) | operator network boundary |

## Supported versions

| Version | Supported |
|---|---|
| latest published `0.1.x` | yes, current |
| anything older | no, best effort only |

The version is derived by `setuptools_scm` from the git tag, so there is no SemVer
number written in the tree to quote here. Determine the installed version with
`python -c "import importlib.metadata as m; print(m.version('skdashboard'))"`. Until
1.0, only the latest published `0.x` line receives security fixes, per
[VERSION_LIFECYCLE](https://github.com/smilinTux/sk-standards/blob/main/standards/VERSION_LIFECYCLE.md).

## Reporting a vulnerability

**Do not open a public GitHub issue for a security vulnerability.**

- **Primary channel:** GitHub **private vulnerability reporting**. Use "Report a
  vulnerability" on the Security tab of
  [`smilinTux/skdashboard`](https://github.com/smilinTux/skdashboard/security).
- **Secondary (out of band):** contact the maintainers (smilinTux / SKWorld) through the
  address on the GitHub organization profile. Encrypt sensitive reports to the
  maintainer's PGP key published there.

Please include: the installed `skdashboard` version, the Python version, the values of
`SKAI_AUTHZ` and whether `SKAI_QUEUE_TOKEN` was set, the exact route and method, and a
minimal reproduction.

**Acknowledgement SLA: within 72 hours.** We aim to ship a fix or a documented mitigation
within 90 days and to coordinate a disclosure date with you.

**Safe harbour.** Good-faith research conducted under coordinated disclosure will not be
pursued. Do not access, modify, or exfiltrate data that is not yours, do not degrade
service for others, and give us a reasonable window before publishing. Credit is given
unless you ask otherwise.

### What we especially want to hear about

- A `queue_authz` decision that allows where the PDP denied, or that treats an error as
  an allow.
- A privileged route that reaches the coordination store, the CMDB, or skgateway's admin
  surface without passing through `_capability_gate` (or one of its named wrappers
  `_queue_gate` / `_change_gate`) when a gate is configured.
- Any path parameter that escapes its intended directory.
- An `/api/assistant` prompt that induces an unintended `ACTION` mutation.
- A change that binds the server to anything other than `127.0.0.1`.
- A crypto or security overclaim in this repo's own documentation. Overclaiming is a
  defect here, and this file is meant to be checkable against the code.

---

**License:** GPL-3.0-or-later. **Standards:** ISO/IEC 29147 and 30111 (disclosure),
CVSS v4.0, and the sk-standards
[SECURITY_DISCLOSURE_STANDARD](https://github.com/smilinTux/sk-standards/blob/main/standards/SECURITY_DISCLOSURE_STANDARD.md)
and
[SKWORLD_AUTHORIZATION_STANDARD](https://github.com/smilinTux/sk-standards/blob/main/standards/SKWORLD_AUTHORIZATION_STANDARD.md).
