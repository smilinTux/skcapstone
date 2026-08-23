# Runbook: issue a fleet machine a `node`-class capauth identity

**Epic:** `3bbf39ea`. **Card:** `5ee6510f` (parent `804dc9d5`).
**Depends on:** `fc6500cb`, which built the ceiling itself
(`capauth/src/capauth/identity_class.py`, capauth PR #51).
**Companion:** [adr-node-role-model.md](adr-node-role-model.md) (why `.100` is a
worker at all), [dot100-secret-audit.md](dot100-secret-audit.md) (what the old
full install left behind, which this identity deliberately does not reuse).

Executed against `.100` (`node-ollama`) on 2026-08-16. The evidence at the
bottom is the actual output, kept because the reason strings are a contract:
operators grep them, and capauth's own tests assert them literally.

---

## What this actually buys

`capauth.authz.decide` is a grant checker. A node with no `token:issue` grant is
safe only for as long as nobody issues it one. The identity class turns that
state into a property: `decide` resolves the subject's class at **step 0**, ahead
of the token read, so a node-class subject holding a valid, signed
`Capability.ALL` token is still denied. Section 3 of the evidence proves that
against a real signed wildcard rather than asserting it.

Three facts worth carrying into the next node:

1. **The class is stored, not asserted.** It lives in
   `<base_dir>/identity/classes.json`, a flat `{subject: class}` map. `decide`
   takes no class from the request, because a class the caller can pass is a
   ceiling an attacker can raise.
2. **Unclassified subjects are untouched.** `resolve_identity_class` returns
   `None` for anything with no assignment and `decide` then behaves exactly as
   it did before the layer existed. That is what makes classifying one subject
   at a time safe. It is measured below, not assumed.
3. **An unreadable `classes.json` denies everything.** `IdentityClassError` is a
   deny, on purpose: the alternative rewards corrupting one file. This is the
   real blast radius of the file, and the reason it is worth knowing that it
   replicates.

---

## What propagates, and what does not

`~/.skcapstone` is the Syncthing folder `skcapstone-sync` (`sendreceive`, shared
with three other devices). Three things this procedure writes live under it and
therefore **reach every node in the mesh**:

| Path | What it is | Replicates? |
|---|---|---|
| `identity/classes.json` | subject -> class map | **yes** |
| `peers/<node>.json` | the approved `DeviceRecord` | **yes** |
| `security/tokens/<id>.json` | the scoped capability token | **yes** |

That is the correct direction. A ceiling on a subject should be the same ceiling
at whichever PDP the subject shows up at, and the device record and token are
what any PDP needs to reach an ALLOW at all. `capauth/security/tokens` (note the
`capauth/` prefix) is a **different**, per-host, `.stignore`d directory; the PDP
does not read it. Do not confuse the two.

What does **not** leave the node:

* **The node's private key.** It is generated on the node, into `~/.gnupg`,
  which is outside the replicated folder entirely. Only the public half travels,
  and it travels by hand.
* **The operator's secret key.** It never goes near the node. The node's keyring
  holds the operator's PUBLIC key only, which is exactly what it needs to verify
  a token the operator signed, and nothing it could sign with.

Consequence worth stating plainly: this procedure writes **no bytes directly to
the node**. Syncthing delivers a few KB. Nothing is installed there and no
service is restarted.

---

## Procedure

### 1. Generate the node's own keypair, on the node

The node signs nothing in normal operation, but it should still own a real
identity rather than borrow one. Ed25519, no passphrase (the box is unattended),
private half never leaves:

```bash
ssh cbrd21@192.168.0.100
cat > /tmp/node-key-params <<'EOF'
%echo generating node identity
Key-Type: eddsa
Key-Curve: Ed25519
Key-Usage: sign
Subkey-Type: ecdh
Subkey-Curve: Curve25519
Subkey-Usage: encrypt
Name-Real: node-ollama (SKWorld Fleet Node)
Name-Email: node-ollama@chef.skworld.io
Expire-Date: 0
%no-protection
%commit
EOF
gpg --batch --gen-key /tmp/node-key-params && rm -f /tmp/node-key-params
gpg --armor --export node-ollama@chef.skworld.io
```

Confirm the node holds **no secret keys other than its own**, and does hold the
operator's public key (it needs that to verify tokens):

```bash
gpg --list-secret-keys      # its own key, and nothing else
gpg --list-keys             # + the operator root
```

The subject name is the fleet's own node name (`skfleet` calls `.100`
`node-ollama`, after its hostname) qualified into the fqid grammar:
`node-ollama@chef.skworld.io`. Use `capauth.subject.canonical_subject` rather
than eyeballing it; `decide` is an exact string matcher and a near-miss reads as
`unknown subject: no enrolled device`, which looks like a config error and is
actually a naming defect.

### 2. Enroll, classify, and mint, from the operator box

```bash
python scripts/fleet/issue-node-identity.py \
    --subject node-ollama@chef.skworld.io \
    --node-pubkey node-ollama.pub.asc \
    --operator-key BD7EEECA23D90A594400751CFDB582D9CB7272A6
```

Run it where the operator SECRET key is, never on the node. `--dry-run` verifies
the attestation and writes nothing.

**The trap.** An `attested` enrollment's `attestation` is not a detached
signature. `PGPyBackend.verify` parses it with `PGPMessage.from_blob` and
compares the **embedded** payload against the challenge bytes, so it needs a
signed *message* (`gpg --sign`), with `--compress-algo 0` and an empty filename
so the literal packet is a plain copy of those bytes. A `--detach-sign` blob
passes `gpg --verify` and passes bare `pgpy`, and is still rejected here. The
script handles this; a hand-rolled repeat will not.

### 3. Verify at the node's own PDP

Wait for replication, then decide on the node itself. Do not take the operator
box's word for it: the node has a different keyring, and `signature_verifies`
depends on it.

---

## Evidence, `.100`, 2026-08-16

Identity as issued: subject `node-ollama@chef.skworld.io`, device
`eb40a8dc-e7d1-481d-9a3a-57f0cf8a085f`, key fingerprint
`85EC93FBD06E622B9F39F886EE0C62C705FD89E8`, mode `attested`, attested by the
operator root `BD7EEECA23D90A594400751CFDB582D9CB7272A6`. Token
`950ee371c67f1866...`, capabilities `[skgateway.infer, skchat.status,
skchat.inbox]`, no `Capability.ALL`, signature verifies, 90 day TTL.

### 1. Decisions at `.100`'s own PDP (capauth 0.2.15, live store)

```
resolved class: node
  skgateway.infer  allow=True  audit=True   reason: granted: subject enrolled attested (>= attested) with an active token granting skgateway.infer
  skchat.status    allow=True  audit=True   reason: granted: subject enrolled attested (>= tofu) with an active token granting skchat.status
  token:issue      allow=False audit=True   reason: identity class 'node' forbids capability 'token:issue'
  identity:sign    allow=False audit=True   reason: identity class 'node' forbids capability 'identity:sign'
  change.deploy    allow=False audit=True   reason: identity class 'node' does not permit capability 'change.deploy'
  *                allow=False audit=True   reason: identity class 'node' forbids capability '*'
```

The ALLOW rows are the control that matters: the deny is about the **capability**,
not about the subject being broken or unknown. Every row, allow and deny, carries
the AUDIT obligation.

The two deny shapes are different operator problems and stay distinct.
`forbids` means someone tried to give a machine an operator power.
`does not permit` means a capability outside the node allowlist, which is where
anything newly added to the rule table lands by default.

### 2. No live seat lost access

The one thing that could go wrong here is collateral. Measured by re-deciding
against a shadow home identical to the live store except that `classes.json` is
absent, which is the fleet exactly as it was before this card:

```
comparing 133 enrolled subjects x 12 capabilities
subjects whose outcome CHANGED: ['node-ollama@chef.skworld.io']
total changed decisions: 9 of 1596
```

All nine are the node, and all nine were **already denials**; the class only
replaced the reason with a stronger one:

```
  node-ollama@chef.skworld.io change.deploy
    before: allow=False insufficient enrollment mode: device is 'attested', change.deploy requires at least 'verified'
    after : allow=False identity class 'node' does not permit capability 'change.deploy'
  node-ollama@chef.skworld.io token:issue
    before: allow=False unknown capability: 'token:issue'
    after : allow=False identity class 'node' forbids capability 'token:issue'
```

Note what `before` shows about the pre-class posture: `token:issue` was denied
only because it is not in `DEFAULT_RULES`, and `change.deploy` only because the
device is `attested` rather than `verified`. Both are *incidental*. Add the rule,
or ever enroll that box `verified`, and the protection evaporates. The class is
what makes it structural.

### 3. A genuinely signed `Capability.ALL` token is still denied

Run in an isolated temporary home, so no wildcard token ever enters the
replicated store. The token is real: minted through `issue_token`, signed by the
actual operator key via actual gpg, `signature_verifies() == True`.

```
  capabilities   : ['*']
  issuer         : BD7EEECA23D90A594400751CFDB582D9CB7272A6
  signature verifies (real gpg, real Chef key): True

with the node class assigned:
  token:issue        allow=False  reason: identity class 'node' forbids capability 'token:issue'
  identity:sign      allow=False  reason: identity class 'node' forbids capability 'identity:sign'
  *                  allow=False  reason: identity class 'node' forbids capability '*'
  change.deploy      allow=False  reason: identity class 'node' does not permit capability 'change.deploy'
  change.propose     allow=False  reason: identity class 'node' does not permit capability 'change.propose'

SAME store, SAME signed '*' token, class assignment removed:
  change.propose     allow=True   reason: granted: subject enrolled attested (>= attested) with an active Capability.ALL token
```

That last line is the point of the whole card. The wildcard token is not inert
and not malformed: remove the ceiling and it grants. The ceiling is what stops
it, and it stops it before the store is ever read.

---

## Reverting

Revoke, do not delete. `capauth.pairing.revoke(device_id, reason)` is a state
transition and a revoked device satisfies no minimum mode, which drops the node
to `unknown subject`. Revoke the token with `capauth.tokens.revoke_token`.

Removing the subject's row from `identity/classes.json` removes the ceiling and
nothing else; it does not remove the grant. Do that only to restore the exact
pre-card behavior, and understand that section 3 above is what you are giving
up. Deleting the file entirely is not a revert: an unreadable or malformed
`classes.json` is a **deny for every subject**, fleet-wide, by replication.
