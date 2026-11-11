#!/usr/bin/env bash
# Owner-reviewed launcher for skcapstone-cmdb-reconcile-network.service.
#
# Install, after review, as ~/.config/skcapstone/cmdb-network-apply (mode 0700).
# Its presence is the unit's ConditionPathExists opt-in: without it the apply
# unit and its timer stay inert. Targets and skvault references live in
# ~/.config/skcapstone/cmdb-network-targets, read by skcapstone-cmdb-network.
set -euo pipefail
exec "$HOME/.skenv/bin/skcapstone-cmdb-network" apply
