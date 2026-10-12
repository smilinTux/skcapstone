# Cursor lane design, 2026-10-12

Approved scope: a disabled Cursor lane, initially restricted to chiap08 and its
existing cakjr.estate login. No production policy edit, lane activation, merge,
or paid-model probe is part of this implementation.

The installed cursor-agent supports print mode, stream-json, explicit model,
workspace and `--sandbox enabled`. Its read-only model list contains
`composer-2.5` and `cursor-grok-4.6-high`.

## Routing and billing boundary

Add an optional `cursor` lane with provider `cursor`, fixed host `chiap08`, and
`enabled: false` in the repository example. Existing policies without the lane
retain their behavior. S and M select composer-2.5; L selects
cursor-grok-4.6-high; XL refuses. Use a code-owned exact allowlist of the observed
Cursor-plan IDs, checked during route selection, request validation and child
launch. Never allow auto/default, composer-fast aliases, arbitrary parameters,
Claude, GPT, Gemini, unprefixed Grok or fallback to another model. Cursor routes
must never enter SKGateway catalog mutation or a billed gateway probe.

## Worker and isolation

Reuse native production resource admission, exact claim ownership, managed
worker wrapper, review_child_command heartbeat, live snapshot and terminal
worker evidence. The child is a host-side Cursor adapter running the CLI inside
a fail-closed bwrap boundary plus `--sandbox enabled`. Mount only the source
workspace writable, required runtime/system files read-only, private tmpfs,
and the selected account authentication read-only. Do not mount CardStore,
other workspaces, the user bus, SSH credentials or ordinary home configuration.
Use a fresh Cursor configuration so inherited model selections, MCP servers,
plugins and hooks cannot change this launch. Do not use force/yolo flags.

Pi's current guard is a tool-call extension, not an outer OS sandbox. Cursor
cannot load that extension or its SKGateway retry helper. Instead, CardStore is
absent from the sandbox and the agent cannot claim, release or directly edit
board files. The host retains those lifecycle operations. A source-only Cursor
handoff is a bounded workspace result describing its report and requested typed
verdict; the trusted adapter verifies exact ownership, candidate Git identity
and report confinement, then uses the existing coord CLI and validators.
A successful stream result alone never creates PASS. Independent review and
native acceptance remain mandatory. Initial Cursor supply is producer work;
existing review lanes retain review work until a separate review adapter exists.

Authentication and shared provider networking are explicit exceptions to a
literal no-read-outside-workspace rule. Persistent filesystem writes are confined
to the workspace; runtime state is disposable tmpfs. The account credential is
read-only, not an additional workspace. The CLI's selected model is code-pinned;
this does not claim independent visibility into Cursor's server-side billing.

## Progress and evidence

Parse bounded stream-json events in the host adapter. Record only recognized
assistant/tool activity as agent progress, separately from the wrapper's
shell-liveness heartbeat. Reject malformed streams, explicit errors and any
reported model outside the pinned selection. Preserve exit status and bounded
raw output in the wrapper's normal evidence path. A timer, JSON output or clean
exit is not a product verdict.

## Verification

Tests must cover disabled and absent policies; chiap08-only placement; S/M/L
routing and XL refusal; every disallowed model category; no fallback; exact
argv; bwrap containment; stale claims and escaping evidence; stream errors and
progress semantics; wrapper and admission wiring. Run the relevant existing
builder, policy, routing, wrapper and lifecycle tests plus Ruff/Black. Live
model inference is unnecessary for these tests and will not be used to validate
billing assumptions.
