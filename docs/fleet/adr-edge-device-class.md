# ADR: an auth-only edge device is a capauth device class, not a node role

**Status:** Accepted.
**Epic:** `3bbf39ea`. **Card:** `b89f76ca`, parent `31c1ae2c`. Sibling ADR:
[adr-node-role-model.md](adr-node-role-model.md) (card `e884151b`).
Reconciliation work: card `a2bc8461`.

## Decision

A phone, a security key, a tablet or a laptop that only authenticates is
**not** a node role. It gets no fifth profile in
`deploy/fleet-objects/profile/`, no `sknoded`, no membership in any fleet-store
Syncthing folder, and no spec is ever delivered to it.

The line is simple enough to state as a test. A **node** is a machine the
fleet installs software onto and then converges toward a declared state: it
carries `spec.role`, it receives objects and placements, and it reports status
back. An **edge device** authenticates and attests, and it never converges.
Nothing is installed on it by us, nothing is declared about it by us, and
losing it costs a credential rather than a workload.

Giving such a device a role would import the whole node contract to describe a
key. Every field on a Profile (`packages`, `units`, `unitsIgnore`,
`syncFolders`, `stateTier`) would be empty or meaningless on a phone, and the
one property that actually matters, how strongly the device's key was bound to
a human, has no place to live on a Profile at all. It already has a home:
capauth's enrollment mode. Role is a property of a machine. Device class is a
property of a credential. They are different objects and they belong in
different registries.

## Context: two device registries exist today

Both were read for this ADR, and both paths were confirmed present.

### 1. The capauth pairing store (the cryptographic one)

`/home/cbrd21/clawd/skcapstone-repos/capauth/src/capauth/pairing/store.py`
defines `PairingStore`, whose `base_dir` defaults to `~/.skcapstone` and which
keeps approved devices as a versioned `pairing` sidecar on the existing v1
peer records under `<base_dir>/peers/<name>.json`, with pending enrollments
under `<base_dir>/pairing/enrollments/<id>.json`.

The record type is `DeviceRecord` in
`/home/cbrd21/clawd/skcapstone-repos/capauth/src/capauth/pairing/records.py`,
which carries an `EnrollmentMode` and a `revoked` flag. The mode ordering is
explicit and matches the prompt this ADR was written from:

```python
#: Severity ordering: higher wins. verified > attested > tofu.
MODE_SEVERITY: dict[str, int] = {
    EnrollmentMode.TOFU.value: 1,
    EnrollmentMode.ATTESTED.value: 2,
    EnrollmentMode.VERIFIED.value: 3,
}
```

`verified` is a capauth challenge-response or self-signed FQID assertion,
`attested` is an operator signature over the device key, and `tofu` is
pin-on-first-use. `mode_satisfies(record_mode, minimum)` (`records.py:84`) is
the comparison, and `capauth.authz.decide` consumes it: `authz.py` imports
both `list_devices` and `mode_satisfies`, resolves the subject's devices
through `capauth.pairing.list_devices` (which builds a `PairingStore` under
the hood), takes the strongest mode, and gates on it.

This is the registry that already knows how a key was bound to a human, which
is exactly the fact an edge-device class needs.

### 2. The skchat operator device registry (the operational one)

`/home/cbrd21/clawd/skcapstone-repos/skchat/src/skchat/device_registry.py`
keeps one row per `device_fp` in a JSON file, default
`~/.skchat/state/operator_device_registry.json` (present on this box,
overridable with `SKCHAT_DEVICE_REGISTRY`). `approval_for(device_fp)` is the
gate, `set_approved()` is the writer, and the operator drives it with
`skchat devices approve <fp>` (`skchat/cli.py:5897`, which calls
`set_approved(device_fp, True)`). Session minting reaches it through
`skchat.guest.is_device_approved`, which "delegates the whole decision,
including which way to fail when the registry cannot answer" to
`approval_for`.

Its job is different from capauth's. It is the correlation key for Linked
Devices: label, approval state, and the prekey `key_ids` that unlink has to
remove. It is not a cryptographic trust store, and it does not pretend to be
one.

## The real risk is not duplication, it is disagreement on failure

Two registries holding overlapping facts is untidy. Two registries that fail
in **opposite directions** on the same input is a security property, and this
is the argument for unifying them.

**capauth fails closed.** `decide()`'s docstring says so in as many words:

> Deterministic from cryptographic facts only (enrollment mode + granted
> capability tokens + a verifying signature on the granting token). ...
> **Fails closed on every uncertainty, including key material that cannot be
> reached to verify a signature.**

and the body is written as a ladder of denials, each labelled:

```python
    # 1. Unknown capability -> fail closed.
    # 2. Unknown subject (no enrolled, non-revoked device) -> fail closed.
    # 3. Insufficient enrollment mode -> fail closed.
```

Unknown subject means no enrolled device record was found. Missing state
denies.

**skchat deliberately fails open on the same shape of missing state.**
`approval_for` distinguishes "no row" from "cannot read the file" and then
sends the second case the other way (`device_registry.py:111-125`):

```python
    path = registry_path()
    if not path.exists():
        return True
    try:
        data = json.loads(path.read_text() or "{}")
    except (ValueError, OSError):
        logger.warning("device registry unreadable; approval gate open: %s", path)
        return True
    if not isinstance(data, dict):
        logger.warning("device registry is not an object; approval gate open: %s", path)
        return True
    row = data.get(device_fp)
    if row is None:
        return False
    return is_approved(row)
```

Its own docstring states the split as policy: "**readable, but no row for this
fingerprint** -> NOT approved", and "**missing or unreadable registry** ->
approved", because "bricking every device on the node over one corrupt JSON
file is a far worse outcome than briefly not enforcing a gate that only bites
a caller who already holds the token." A row that exists but has no `approved`
key also reads as approved (`is_approved`, `return bool(row.get("approved",
True))`), which grandfathers the operator's pre-Phase-3 devices.

Read the two together and the asymmetry is exact. A device fingerprint that no
registry has ever heard of is **denied** by `capauth.authz.decide` and, when
the JSON file is absent or corrupt, **permitted** by
`skchat.device_registry.approval_for`. The same missing file denies in one
system and permits in the other.

Neither choice is wrong in isolation. capauth is a policy decision point whose
denial costs one action, and skchat's gate is an anti-brick measure on a store
that is per-node, best-effort by design, and whose failure mode would
otherwise be locking the operator out of every device at once with no approved
device left to approve from. The problem is that the two are now consulted
about **the same device**, so the effective posture of the system is whichever
component happens to be asked, and no reviewer can state the fleet's answer to
"is this device trusted" without first asking which code path ran. A security
property that depends on the call site is not a security property.

## One registry of record: capauth

**capauth's pairing store is the registry of record for device trust.**
skchat's registry stays as the operational join table it already is (label,
`key_ids`, unlink bookkeeping) and stops being an independent source of truth
about whether a device may act.

The direction follows from what each store can prove. capauth holds the
enrollment mode, the evidence behind it (a challenge proof, an operator
signature, or an admission that it is only a pin), and the revocation flag,
and it is already the input to the PDP that other services are being pointed
at. skchat's registry holds a boolean written by whoever ran the CLI. You can
derive a boolean from a mode, and you cannot derive a mode from a boolean, so
migrating the other way would destroy the only fact worth keeping. The
fail-open behaviour also has to move with the decision rather than being
argued about twice: once approval resolves through capauth, "I cannot read the
store" stops being a per-service policy choice and becomes one documented
posture for the whole plane.

Migration belongs to card `a2bc8461`, and its shape is already decided there:
add a capauth-backed lookup behind a flag
(`SKCHAT_DEVICE_REGISTRY_BACKEND=capauth`) that resolves a fingerprint through
`capauth.pairing.list_devices` / `find_device` and honours
`DeviceRecord.revoked` and the enrollment mode, then ship it **shadow-first**,
logging agreement and disagreement between the two answers without changing
which one is returned. That is the same shadow-then-enforce staging the
`SKCHAT_AUTHZ_PDP` rollout used, and it is the right shape here for a specific
reason: the disagreement described above is currently unmeasured. Flipping to
fail-closed without first counting how often the two registries differ would
be a lockout with a good rationale, which is still a lockout.

The JSON path keeps its fail-open default until the shadow window says what
enforcing would actually have done.

## What the edge-device class needs beyond this

This ADR settles the taxonomy question and the registry-of-record question. It
does not define the class itself. That is parent card `31c1ae2c`: hardware
backed key enrollment (FIDO2, TPM or Secure Enclave), posture fields, and PDP
policy hooks so edge attestation can gate high-consequence actions such as the
change-management deploy executor, secret reads and autopilot live execution.
The dependency runs one way, which is why this ADR comes first: those hooks
have to attach to a single registry, and naming which one is the decision
recorded here.

## Consequences

Good: the fleet's four roles stay four, and a phone never acquires a spec.
Device trust gets one home, and the fail-open versus fail-closed asymmetry
becomes one documented posture instead of an accident of routing.

Costs, honestly: skchat gains a dependency on capauth for a decision it can
currently make alone, which means a capauth outage now has an opinion about
whether a phone can hold a session, and that opinion has to be designed rather
than inherited. The shadow window also delays the actual fix, and a shadow
window nobody reads is just a longer version of doing nothing. The measurement
is the deliverable, not the flag.

## See also

- [adr-node-role-model.md](adr-node-role-model.md): the two axes, the four
  roles, and the accepted single-control-seat SPOF.
- [profiles.md](profiles.md): the Profile kind, including
  `capauthIdentityClass`, which classifies a node's credential and is not the
  same thing as an edge device's class.
- Card `a2bc8461`: one device registry, the shadow-first reconciliation.
- Card `31c1ae2c`: the capauth edge-device class itself.
