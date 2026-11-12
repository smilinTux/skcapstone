# Native Node dependency artifact provisioning

Use the recovered operator procedure: clean locked npm install without lifecycle
scripts, immutable files, production artifact digest, atomic content-addressed
installation and environment readback. Add a small operator module with prepare
and install phases so native binding work remains explicit. Preserve the opaque
legacy marker format, but record real operator provenance separately.

Test exact manifests, bootstrap, permissions, unsafe links, failed install and
idempotent publication. Build the single distinct skgateway lock; explicitly
compile its better-sqlite3 binding with the governed Node headers in a private
sandbox. Install the two bootstrap variants on chiap01-04 and chiap08, verifying
digests, environment resolution and SQLite smoke execution on each. Open a PR
for the reusable command without merging or changing deployed source.
