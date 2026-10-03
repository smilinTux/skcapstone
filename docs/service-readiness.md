# Explicit application readiness

The existing service-health registry and scheduled health task support opt-in
readiness. Legacy entries retain their reachability semantics. Register through
`skcapstone.sdk.register_service(name, health_url, readiness=True,
expected_json={"maintenance": False}, user_agent="SKCapstone-Readiness/1.0")`.
The last two arguments are optional. The persisted fields are `health_readiness_url`, `health_readiness`,
`health_expected_json`, and `health_user_agent`.

Use a documented vendor readiness endpoint, not a login page. A check requires
2xx without redirects and retains default TLS certificate/hostname verification.
JSON assertions compare exact top-level scalar values and types; missing fields,
malformed/non-object JSON and bodies over 64KiB fail closed. There is no expression
language, nested traversal, authentication provisioning or arbitrary command.

HTTP 3xx/4xx is `unknown` with `http_policy_or_endpoint`: it may indicate access
policy, authentication, rate limiting or an unavailable endpoint, not backend
failure. HTTP 5xx and failed JSON assertions are `down`/`not_ready`. Transport
failures are `down`/`transport`; malformed options are `unknown`/`invalid_config`.
Unknown results neither create backend-down incidents nor resolve existing ones.
An `up` result certifies only the configured endpoint contract: a maintenance
flag alone does not establish database readiness. Check databases separately.

The scheduler's existing cadence and incident authority are unchanged. This
feature does not create another collector or enable a recurring task by itself.
`tests/test_service_readiness.py` uses a local HTTP fixture for healthy, missing,
malformed, maintenance, policy-denial, redirect and oversized-response controls.

Readiness registrations leave legacy `health_url` and `pid_file` empty. Older
daemons therefore report unknown instead of silently weakening the readiness
contract to a reachability or PID check during mixed-version rollout.
