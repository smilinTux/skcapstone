# Standing up a seat

A **seat** is a standing role with its own identity: Mero the overseer, Link the
integrator. Cards labelled `seat-<name>` run under that seat's identity, so their
claims, verdicts and mail are attributable to the role that owns the work rather
than to whichever lane happened to pick it up.

This is the whole ceremony, in order, with the checks that matter. It was written
by doing it twice and getting parts of it wrong both times.

Substitute your seat name for `<seat>` throughout. It must match
`^[a-z][a-z0-9-]{0,31}$`, because it reaches a shell command line, a tmux session
name, a claim owner and a mailbox name.

---

## 1. Generate the seat's own capauth identity

On the host that holds the operator's secret root (chiap08 today):

```bash
umask 077
PF=~/capauth-pass/<seat>.passphrase.env
openssl rand -base64 36 | tr -d "\n/+=" | cut -c1-48 > "$PF"; chmod 600 "$PF"

D=~/.skcapstone/agents/<seat>/capauth
mkdir -p "$D"; chmod 700 "$D"
capauth --home "$D" init -n <Seat> -e <seat>@casey.skworld.io \
        --type ai --algorithm rsa4096 -p "$(cat $PF)"
```

**Never omit `--home`.** The default profile on every chi host is Jarvis. A seat
that inherits it produces signatures that are really Jarvis speaking, and an
identity that cannot be distinguished from another is worth nothing.

`capauth init` currently exits 1 while still producing a valid keypair and
profile. Something after key generation fails, probably the sync step. Check the
result rather than the exit code.

### Verify it is actually distinct

Derive the fingerprint from the key material, not from `profile.json`:

```bash
T=$(mktemp -d); chmod 700 "$T"
gpg --homedir "$T" --batch --quiet --import "$D/identity/public.asc"
gpg --homedir "$T" --list-keys --with-colons | awk -F: '$1=="pub"{p=1;next} $1=="fpr"&&p==1{print $10; p=0}'
rm -rf "$T"
```

It must differ from Jarvis (`C8D406A4…`) and from every other seat. If it matches
one, stop and start over.

---

## 2. Have the operator sign it

The seat's key is signed by the human operator, so the audit trail shows a person
vouched for this agent. Follow the existing pattern: `operator: casey`,
`operator_fingerprint: AD80D077…`, `fqid: <seat>@casey.skworld.io`.

`skcapstone.operator_link.create_operator_attestation()` is the mechanism, but
**it cannot be called as-is**: it signs with an empty passphrase and it requires
`identity/public.asc` in the operator's home, which Casey's does not have (his
public half lives in a timestamped `identity.public-only.*` directory). Assemble
a temporary operator home holding both halves in a `700` tmpdir, sign, and
destroy it.

Two checks, both required:

```
signature verifies against the operator public key ......... must be True
signature verifies against a TAMPERED payload .............. must be False
```

A verify that only ever returns True proves nothing.

Then write `operator`, `operator_fingerprint`, `operator_signed_at` and `fqid`
into the seat's `profile.json`, and store the signature in
`identity/operator-attestation.json`.

**Two signatures now exist and must not be confused.** `profile.json` carries
capauth's own self-signature from `init`. The operator's signature is in the
attestation file. If they ever disagree, the attestation wins.

---

## 3. Add the seat to the estate

Back up first, append, then read it back through a different path:

```bash
cp ~/.skcapstone/capauth/estate.json ~/.skcapstone/capauth/estate.json.bak-pre-<seat>-$(date -u +%Y%m%dT%H%M%SZ)
```

```json
{"fingerprint": "<SEAT FINGERPRINT>", "status": "active", "identity_type": "ai",
 "label": "<Seat> :: <one line saying what it owns>",
 "allowed_secret_roots": ["/home/skuser01/.skcapstone/agents/<seat>/capauth"]}
```

---

## 4. Scaffold the agent home on the host the seat runs on

```bash
mkdir -p ~/.skcapstone/agents/<seat>/{soul,config,identity,seeds,logs,trust/febs}
mkdir -p ~/.skcapstone/agents/<seat>/memory/{short-term,mid-term,long-term}
mkdir -p ~/.skcapstone/agents/<seat>/capauth/{identity,acl,advocate,data}
chmod 700 ~/.skcapstone/agents/<seat>/capauth
touch ~/.skcapstone/agents/<seat>/journal.md
```

---

## 5. Confirm the key split propagated correctly

Estate entries and **public** key material propagate across chi hosts on their
own. The propagation **excludes private keys**, which is the property that makes
this safe. Confirm rather than assume:

```bash
find ~/.skcapstone/agents/<seat>/capauth -name '*.asc' -printf '%m %p\n'
find ~/.skcapstone/agents/<seat>/capauth -name private.asc | wc -l    # must be 0 off the signing host
```

**Then put the public key in the DEFAULT gpg keyring on every host that verifies**:

```bash
gpg --import ~/.skcapstone/agents/<seat>/capauth/identity/public.asc
```

Skipping this is the single most repeated mistake here. Verifying inside a
throwaway homedir passes for you and returns `NO_PUBKEY` for everyone else, which
looks like a signature failure and is not. It cost a blocked preflight once
already.

---

## 6. The seat is now dispatchable

`seat_for()` in `scripts/fleet/skfleet-rotate.py` treats a seat as real only when
its agent home exists **and** contains `capauth/identity/public.asc`. A
well-formed but unprovisioned name, a typo like `seat-lnik`, falls back to lane
naming and logs a warning. That is deliberate: a phantom seat would write claims
and verdicts under an identity with no home, no key, no mailbox and no estate
entry, and its outputs would look attributable while being anonymous.

Label the seat's cards and check before walking away:

```bash
skcapstone coord label <cardid> seat-<seat> --agent <you>
skcapstone coord gates <cardid>
```

---

## 7. Give the seat a charter, then tell it and everyone else

A seat without a written charter is a name. State plainly what it owns, **what it
does not own**, its marching orders, and how it is measured. The second of those
matters most: seats are created because one seat had too many jobs, and a charter
that only lists powers will grow the same way.

Then mail the seat itself and the fleet. The seat's welcome should include the
two mailbox traps, because both have already cost real time:

- **Read your own box** with `skmail read <seat>`. Do not poll with `skmail tail`;
  it shows recent traffic and silently hides anything that scrolls past the
  window.
- **`ack` is all or nothing.** It marks everything currently visible as read,
  including mail you have not acted on. Read, act, then ack.

---

## Checklist

```
[ ] identity created with --home, fingerprint distinct from Jarvis and every seat
[ ] fingerprint derived from public.asc, not read from profile.json
[ ] operator signature verifies GOOD, and verifies BAD against a tampered payload
[ ] estate.json backed up, entry appended, read back through a separate path
[ ] agent home scaffolded on the host the seat runs on
[ ] private.asc count is 0 on every host except the signing host
[ ] public key imported into the DEFAULT keyring on every verifying host
[ ] charter written, including what the seat does NOT own
[ ] first cards labelled seat-<name> and checked with coord gates
[ ] seat and fleet both told, with the two mailbox traps stated
```
