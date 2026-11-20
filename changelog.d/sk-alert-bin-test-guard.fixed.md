Every sk-alert lookup (`fleet.alerts`, the scheduler, GFS backup, headless
notifications, the ATLAS operator seat and the auto-rollout halt escalation)
honours `$SK_ALERT_BIN` first, and the test suite points it at `/bin/true`. Test
runs on noroc2027 and the chi workers no longer send real "REFUSED actuation"
and "auto-rollout HALTED" alerts. The operator seat now also finds the venv
`sk-alert` when it is not on PATH.
