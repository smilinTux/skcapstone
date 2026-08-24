# SKCP-05F8-D3 session adapter evidence

Card `c8f9202d` adds an opt-in server-side session boundary to the dedicated
read-only SKDashboard runtime. It does not activate or deploy that boundary.

## Authority path

The browser receives only the opaque `__Host-skdashboard_session` cookie. The
cookie is Secure, HttpOnly, SameSite Strict, host-only, and bounded to eight
hours. CapAuth access and refresh credentials are encrypted in a mode-0600
SQLite store. A protected request resolves the server-side credential and then
calls the existing SKDashboard CapAuth PEP. The adapter cannot add a capability
or route around that PEP.

## Login and refresh

Login uses an exact HTTPS issuer, exact callback URI, confidential client
authentication, one-use state, nonce, and PKCE S256. The callback verifies the
signed ID token issuer, audience, lifetime, and nonce against the issuer JWKS.
Refresh uses CapAuth's one-use refresh-family endpoint. A compare-and-swap
reservation prevents two dashboard requests from using the same refresh token.
Unknown, corrupt, expired, conflicting, or unavailable state denies access.

## Storage and rollback

The session key and OIDC client secret are separate mode-0600 inputs. SQLite
state and consistent backups are mode 0600 under mode-0700 directories. Backup
recovery is qualified against the same custody key. Omitting all session CLI
options is the rollback: the four auth routes are absent and direct short-lived
bearer behavior remains unchanged.

The only added POST surface is `/auth/logout`. It requires the exact named
origin and a session-bound CSRF token. No dashboard mutation, queue,
capability-handout, assistant, CMDB-write, or operator-action route is added.
