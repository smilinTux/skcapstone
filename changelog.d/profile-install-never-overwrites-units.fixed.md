The install-profile core backend no longer rewrites a unit file that already
exists. With a lagging inventory it saw `sknoded.service` as missing and copied
the packaged base unit over the `systemd/production` overlay, stripping
`SKFLEET_PRODUCTION_POLICY` on production nodes. Since #1337 ran it on every
rollout, the drift gate halted the chi auto-rollout 95 runs in a row and
chiap01's sknoded ran without its production policy. Absent units are still
installed, and enable/start still run.
