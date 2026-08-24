# SKCP-05F8-A2 HTTPS transport qualification

Date: 2026-08-24
Card: `86fdbdf8`
State: implementation only, disabled

## Qualified boundary

- The dedicated read-only launcher requires a certificate and private-key path.
- Browser access permits only `https://10.0.0.139:7778` and
  `https://100.81.238.58:7778`.
- HTTP requests to an allowed named host receive an exact HTTPS redirect.
- An unnamed or public host is denied before redirect or application routing.
- Every HTTPS response carries `Strict-Transport-Security: max-age=31536000`.
- Every emitted cookie is forced to Secure transport and stripped of a Domain
  attribute, making it host-only.
- No session route is mounted by the dedicated read-only application.

## Sensitivity and rollback

The focused tests prove both named origins, query-preserving redirect, public
host denial, HSTS, Secure cookie addition, Domain removal, absent session
routes, exact launcher TLS inputs, and zero persistent transport state. Each
assertion is anchored to a deliberately exercised response rather than an
absent field.

There is no schema or data migration. Rollback is stopping the disabled
launcher and selecting the preceding package revision. Because the transport
layer writes no runtime state, rollback does not require data repair. A later
deployment card must independently prove listener removal and service
inactivity after rollback.

No service unit, listener, certificate, private key, token, deployment, or
CMDB record was created or changed by this card.
