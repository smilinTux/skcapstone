# SKDASH-OIDC-08 PEP subject evidence

## Defect

CapAuth OIDC access tokens identify the authenticated key by its raw fingerprint.
CapAuth policy enrollments and grants use the canonical `device:<fingerprint>`
subject. SKDashboard verified the token and scope but passed the raw fingerprint
to `decide()`, so current policy denied an otherwise valid Casey session.

## Repair

The dashboard PEP now accepts only a 40 or 64 character hexadecimal OIDC
fingerprint and converts it with CapAuth's canonical subject helper before the
policy decision. Token encoding, lifetime, scope, audience signature, enrollment,
grant, resource, and origin checks remain unchanged and fail closed.

## Proof

The regression decision allows only the expected canonical device subject. It
fails against the prior raw-fingerprint behavior. Separate malformed, short,
non-hexadecimal, and already-prefixed subjects are denied before the PDP call.
