# ATLAS observation and adapter contract

ATLAS consumes `skoperator.observation/v1` envelopes. Each condition includes
its owning application, evidence timestamp, TTL, provenance, scope, and schema-
owned polarity. Consumers must not maintain a second polarity table. Evidence
that is absent, malformed, expired, or produced by a failed probe is `Unknown`;
it must never be synthesized as healthy.

Built-in and CapAuth-verified manifest adapters share one catalog shape. A
built-in retains precedence on an identifier collision. Action contracts use a
globally unambiguous `<app>.<action>` identifier while preserving the local
action name for existing adapter CLIs and ratifications.

An observation example:

```json
{
  "schema": "skoperator.observation/v1",
  "app": "skgateway",
  "conditions": [{
    "type": "UpstreamServing",
    "status": "Unknown",
    "app": "skgateway",
    "observed_at": "2026-08-20T21:00:00Z",
    "ttl_seconds": 300,
    "provenance": "operator-adapter:skgateway",
    "scope": "local",
    "polarity": "problem_when_false"
  }]
}
```

`Unknown` is actionable for diagnosis and escalation but is not proof that the
service is unhealthy and cannot authorize an app action by itself.
