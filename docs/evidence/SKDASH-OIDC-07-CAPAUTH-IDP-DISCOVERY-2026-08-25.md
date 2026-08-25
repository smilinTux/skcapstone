# SKDashboard CapAuth IdP discovery repair

Card `8dd9837d` repairs the confidential authorization-code client discovery
boundary. SKDashboard now reads:

```text
<configured issuer>/oidc/.well-known/openid-configuration
```

CapAuth's root `/.well-known/openid-configuration` document describes its
legacy PGP challenge API. It is not the authorization-code IdP document and
therefore must not be used to validate an OIDC token response.

The regression test serves conflicting root and `/oidc` documents. A valid
exchange succeeds only through the `/oidc` document, while issuer drift and
nonce mismatch remain distinct fail-closed results. Existing JWT signature,
audience, PKCE, scope, TLS, and safe diagnostic checks are unchanged.
