# ATLAS human authorization

ATLAS records a person as that person. It never aliases Chef to the literal
string `human`, and an agent identity cannot assert a human role. A qualifying
CAB approval contains the signer name, the `owner` or `approver` role, the
CapAuth fingerprint, and the ID of a verified single-use authorization.

An authorization is bound to one action, target, ITIL change, scope
fingerprint, expiry, and random nonce. Rebinding or replay fails closed.

## Chef approval ceremony

1. Ensure the Chef human CapAuth public/private keypair is installed under
   `~/.skcapstone/capauth/identity/`. Restore a missing private key only through
   the CapAuth custody/recovery ceremony; never copy an agent key into its
   place.
2. Obtain the exact scope fingerprint from the three accepted CMDB shadow
   artifacts.
3. Create the short-lived signed authorization:

   ```text
   skcapstone itil cab authorize chg-a543c87b \
     --decision approved \
     --target cmdb-network-reconcile \
     --scope <exact-scope-fingerprint> \
     --output ~/.skcapstone/authorizations/chg-a543c87b.json
   ```

4. Submit it before expiry:

   ```text
   skcapstone itil cab vote chg-a543c87b \
     --decision approved \
     --authorization ~/.skcapstone/authorizations/chg-a543c87b.json \
     --target cmdb-network-reconcile \
     --scope <exact-scope-fingerprint>
   ```

5. Read the folded change back and confirm the voter is `chef`, the role is
   `owner`, and the status is approved. ATLAS still rechecks the fleet freeze
   immediately before starting the oneshot. Approval does not bypass freeze.

On this workstation the Chef profile and public key exist, but the human
private key is not installed. The safe next step is custody recovery; recording
a live Chef vote before that would be identity forgery.
